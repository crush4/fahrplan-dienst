"""Gleise aus den Bahnhofstafeln der Bahn, über DBF (db-fakedisplay, https://dbf.finalrewind.org).

Transitous kennt das Gleis im Regional- und S-Bahn-Verkehr oft nicht. Die Bahnhofstafeln kennen es für alle Züge und
S-Bahnen an Bahnhöfen der Bahn, samt Gleiswechsel. Für jedes Teilstück mit Zug oder S-Bahn ohne Gleis schlägt der
Dienst deshalb die Tafel des Abfahrtsbahnhofs nach.

Sparsam: Ein Bahnhof wird höchstens einmal je Minute gefragt, die Zuordnung Koordinate → Bahnhof nur einmal, und je
Stunde gibt es eine feste Obergrenze. Die Tafeln reichen nur wenige Stunden voraus; für spätere Fahrten bleibt das
Gleis leer. Jeder Fehler hier heißt nur „kein Gleis“, nie „keine Verbindung“.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .transitous import nutzer_kennung

STANDARD_URL = "https://dbf.finalrewind.org"
ZEITLIMIT_S = 10
MAX_ANTWORT = 2 * 1024 * 1024
ARTEN = ("FERNZUG", "REGIONALZUG", "S_BAHN")
# Ein Bahnhof der Tafel muss so nah an der Haltestelle der Verbindung liegen (Kilometer).
MAX_ABSTAND_KM = 0.4
MERKEN_S = 60
# So weit reicht die Tafel verlässlich voraus, und so weit zurück lohnt die Frage noch (Minuten).
VORAUS_MIN = 180
ZURUECK_MIN = 10
MAX_JE_STUNDE = 120
MAX_JE_ABFRAGE = 6
MAX_BAHNHOEFE = 3
QUELLE = "Bahnhofstafel"

Holer = Callable[[str, str, bytes | None], Any]


def hole(url: str, kontakt: str, daten: bytes | None = None) -> Any:
    """Eine Abfrage bei DBF; mit `daten` als POST. Wirft OSError oder ValueError, wenn nichts Brauchbares kommt."""
    anfrage = urllib.request.Request(url, data=daten, headers={"User-Agent": nutzer_kennung(kontakt),
                                                               "Accept": "application/json"})
    with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT_S) as antwort:
        roh = antwort.read(MAX_ANTWORT + 1)
    if len(roh) > MAX_ANTWORT:
        raise ValueError("Antwort zu groß")
    return json.loads(roh.decode("utf-8"))


def ortszeit(iso: str) -> datetime | None:
    """Eine ISO-Zeit als deutsche Ortszeit (MEZ oder MESZ), ohne Zeitzonen-Daten des Systems."""
    try:
        zeit = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if zeit.tzinfo is None:
        return None
    utc = zeit.astimezone(timezone.utc)
    return utc + timedelta(hours=2 if _sommerzeit(utc) else 1)


def _sommerzeit(utc: datetime) -> bool:
    """Sommerzeit gilt vom letzten Sonntag im März bis zum letzten Sonntag im Oktober, je 01:00 UTC."""
    def letzter_sonntag(monat: int) -> datetime:
        tag = datetime(utc.year, monat, 31, 1, tzinfo=timezone.utc)
        return tag - timedelta(days=(tag.weekday() + 1) % 7)

    return letzter_sonntag(3) <= utc < letzter_sonntag(10)


def _kurz(text: str) -> str:
    return "".join(text.split()).upper()


def passt(eintrag: dict[str, Any], linie: str, hhmm: str) -> bool:
    """Ist dieser Eintrag der Tafel der Zug des Teilstücks? Gleiche Planabfahrt und gleiche Linie oder Zugnummer."""
    if eintrag.get("scheduledDeparture") != hhmm:
        return False
    gesucht = _kurz(linie)
    zug = eintrag.get("train")
    if not gesucht or not isinstance(zug, str) or not zug.strip():
        return False
    teile = zug.split()
    # „S S6“ → S6, „HLB RE98“ → RE98, „ICE 1573“ → ICE1573.
    kandidaten = {_kurz(zug), _kurz(teile[-1])}
    nummer = str(eintrag.get("trainNumber") or "")
    if nummer:
        kandidaten.add(_kurz(teile[0] + nummer))
    if gesucht in kandidaten:
        return True
    return len(nummer) >= 3 and gesucht.endswith(nummer)


class Tafeln:
    """Schlägt Gleise nach und merkt sich Bahnhöfe und Tafeln. `hole` und `uhr` sind für Tests austauschbar."""

    def __init__(self, basis: str, kontakt: str, hole: Holer = hole,
                 uhr: Callable[[], float] | None = None, jetzt: Callable[[], datetime] | None = None):
        self._basis = basis.rstrip("/")
        self._kontakt = kontakt
        self._hole = hole
        self._uhr = uhr or time.monotonic
        self._jetzt = jetzt or (lambda: datetime.now(timezone.utc))
        self._sperre = threading.Lock()
        self._abfragen: deque[float] = deque()
        # (lat, lon) auf vier Stellen → EVA-Nummern der Bahnhöfe dort, nächster zuerst; leer, wenn dort keiner liegt.
        # Große Bahnhöfe haben mehrere Tafeln (Halle und Tiefbahnhof), deshalb bis zu drei.
        self._bahnhoefe: dict[tuple[float, float], tuple[int, ...]] = {}
        self._tafeln: dict[int, tuple[float, list[dict[str, Any]]]] = {}

    def abfragen_letzte_stunde(self) -> int:
        with self._sperre:
            self._raeume()
            return len(self._abfragen)

    def _raeume(self) -> None:
        jetzt = self._uhr()
        while self._abfragen and jetzt - self._abfragen[0] >= 3600:
            self._abfragen.popleft()

    def _frage(self, url: str, daten: bytes | None = None) -> Any:
        """Eine gezählte Abfrage; None bei jeder Art von Fehler oder wenn die Grenze je Stunde erreicht ist."""
        with self._sperre:
            self._raeume()
            if len(self._abfragen) >= MAX_JE_STUNDE:
                return None
            self._abfragen.append(self._uhr())
        try:
            return self._hole(url, self._kontakt, daten)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _bahnhof(self, lat: float, lon: float) -> tuple[int, ...]:
        ort = (round(lat, 4), round(lon, 4))
        with self._sperre:
            if ort in self._bahnhoefe:
                return self._bahnhoefe[ort]
        daten = urllib.parse.urlencode({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"}).encode("ascii")
        antwort = self._frage(f"{self._basis}/_geolocation", daten)
        if not isinstance(antwort, dict):
            return ()  # nicht merken: beim nächsten Mal noch einmal versuchen
        evas: list[int] = []
        for kandidat in antwort.get("candidates") or []:
            # Nur Einträge mit Kürzel („ds100“) sind Bahnhöfe mit Tafel der Bahn.
            if (isinstance(kandidat, dict) and kandidat.get("ds100") and isinstance(kandidat.get("eva"), int)
                    and isinstance(kandidat.get("distance"), (int, float))
                    and kandidat["distance"] <= MAX_ABSTAND_KM):
                if kandidat["eva"] not in evas:
                    evas.append(kandidat["eva"])
                if len(evas) == MAX_BAHNHOEFE:
                    break
        with self._sperre:
            if len(self._bahnhoefe) >= 5000:
                self._bahnhoefe.clear()
            self._bahnhoefe[ort] = tuple(evas)
        return tuple(evas)

    def _tafel(self, eva: int) -> list[dict[str, Any]]:
        with self._sperre:
            gemerkt = self._tafeln.get(eva)
            if gemerkt is not None and self._uhr() - gemerkt[0] < MERKEN_S:
                return gemerkt[1]
        antwort = self._frage(f"{self._basis}/{eva}.json?version=3&limit=80")
        abfahrten = antwort.get("departures") if isinstance(antwort, dict) else None
        eintraege = [e for e in abfahrten if isinstance(e, dict)] if isinstance(abfahrten, list) else []
        with self._sperre:
            if len(self._tafeln) >= 500:
                self._tafeln.clear()
            # Auch eine leere Tafel wird eine Minute gemerkt, damit ein Ausfall nicht jede Abfrage bremst.
            self._tafeln[eva] = (self._uhr(), eintraege)
        return eintraege

    def ergaenze(self, verbindungen: list[dict[str, Any]]) -> None:
        """Trägt in die Teilstücke mit Zug oder S-Bahn das Gleis der Abfahrt ein, wo die Tafel es kennt."""
        jetzt = self._jetzt()
        gefragt: set[tuple[float, float]] = set()
        for verbindung in verbindungen:
            for teil in verbindung.get("teile", []):
                von = teil.get("von", {})
                if teil.get("art") not in ARTEN or von.get("gleis") or "lat" not in von:
                    continue
                plan = ortszeit(von.get("plan", ""))
                if plan is None:
                    continue
                minuten = (_utc(von["plan"]) - jetzt).total_seconds() / 60
                if not -ZURUECK_MIN <= minuten <= VORAUS_MIN:
                    continue
                ort = (round(von["lat"], 4), round(von["lon"], 4))
                if ort not in gefragt:
                    if len(gefragt) >= MAX_JE_ABFRAGE:
                        continue
                    gefragt.add(ort)
                for eva in self._bahnhof(von["lat"], von["lon"]):
                    if self._trage_ein(von, self._tafel(eva), teil.get("linie", ""), plan.strftime("%H:%M")):
                        break

    @staticmethod
    def _trage_ein(von: dict[str, Any], eintraege: list[dict[str, Any]], linie: str, hhmm: str) -> bool:
        """Sucht den Zug auf einer Tafel; True, wenn er dort steht (auch ohne Gleis)."""
        for eintrag in eintraege:
            if passt(eintrag, linie, hhmm):
                gleis = _gleis(eintrag.get("platform")) or _gleis(eintrag.get("scheduledPlatform"))
                if gleis:
                    von["gleis"] = gleis
                    von["gleis_quelle"] = QUELLE
                    geplant = _gleis(eintrag.get("scheduledPlatform"))
                    if geplant and geplant != gleis:
                        von["gleis_plan"] = geplant
                return True
        return False


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _gleis(wert: Any) -> str | None:
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        wert = str(int(wert)) if float(wert).is_integer() else str(wert)
    if not isinstance(wert, str):
        return None
    wert = wert.strip()
    return wert[:20] if wert else None
