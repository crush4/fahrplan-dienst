"""HTTP-Schnittstelle des Fahrplan-Dienstes.

  GET /status         Ohne Schlüssel. {"dienst", "version", "quelle", "abfragen_letzte_stunde", "max_je_stunde"}
  GET /verbindungen   Kopfzeile X-Zugang. Werte:
                        von=<lat>,<lon>   nach=<lat>,<lon>
                        zeit=<ISO 8601>   (optional; ohne: jetzt)
                        ankunft=1         (optional; zeit ist dann die späteste Ankunft)
                        anzahl=<1..6>     (optional; Standard 4)
                      Antwort {"quelle", "quellen_url", "verbindungen": [...]} (Form siehe transitous.py).
                      Das Gleis von Zügen und S-Bahnen kommt, wo Transitous es nicht kennt, aus den Bahnhofstafeln
                      (tafel.py); die Haltestelle trägt dann "gleis_quelle".
                      Jedes Teilstück trägt seinen Streckenverlauf als "weg": [[Breite, Länge], …], höchstens 120.
                      Endet die letzte Fahrt besser an einem früheren Halt, steigt die Verbindung dort aus und
                      trägt "angepasst": "frueher_aussteigen" (abschaltbar mit FRUEHER_AUSSTEIGEN=0).

Fehler kommen als {"fehler": "<Text>"} mit 400 (Werte), 401 (Schlüssel), 429 (eigene Grenze je Stunde),
502/503/504 (Transitous). Gleiche Abfragen innerhalb einer Minute kommen aus dem Zwischenspeicher.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import VERSION, tafel, transitous
from .konfig import Konfig

LOG = logging.getLogger("fahrplan_dienst")
ZUGANG_KOPF = "X-Zugang"
QUELLEN_URL = "https://transitous.org/sources/"
MERKEN_S = 60
MAX_GEMERKT = 200
STANDARD_ANZAHL = 4
_ZEIT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")


class Abgelehnt(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class Dienst:
    """Prüft Werte, zählt Abfragen und merkt sich Antworten. `hole` und `uhr` sind für Tests austauschbar."""

    def __init__(self, konfig: Konfig, hole: Callable[[str, str], dict[str, Any]] = transitous.hole,
                 uhr: Callable[[], float] = time.monotonic, tafeln: "tafel.Tafeln | None" = None):
        self.konfig = konfig
        # Gleise aus den Bahnhofstafeln; None, wenn abgeschaltet.
        self.tafeln = tafeln if tafeln is not None else (
            tafel.Tafeln(konfig.tafel, konfig.kontakt) if konfig.tafel else None)
        self._hole = hole
        self._uhr = uhr
        self._sperre = threading.Lock()
        self._abfragen: deque[float] = deque()
        self._gemerkt: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def zugang_ok(self, kopfwert: str | None) -> bool:
        schluessel = self.konfig.zugang_schluessel
        if schluessel is None:
            return True
        return kopfwert is not None and hmac.compare_digest(kopfwert.encode("utf-8"), schluessel.encode("utf-8"))

    def abfragen_letzte_stunde(self) -> int:
        with self._sperre:
            self._raeume(self._uhr())
            return len(self._abfragen)

    def _raeume(self, jetzt: float) -> None:
        while self._abfragen and jetzt - self._abfragen[0] >= 3600:
            self._abfragen.popleft()

    def verbindungen(self, werte: dict[str, list[str]]) -> list[dict[str, Any]]:
        von = _punkt(werte, "von")
        nach = _punkt(werte, "nach")
        zeit = _eins(werte, "zeit")
        if zeit is not None and not _ZEIT.match(zeit):
            raise Abgelehnt(400, "zeit braucht die Form 2026-01-31T08:15:00Z")
        ankunft = (_eins(werte, "ankunft") or "") in ("1", "true", "ja")
        try:
            anzahl = int(_eins(werte, "anzahl") or STANDARD_ANZAHL)
        except ValueError:
            raise Abgelehnt(400, "anzahl ist keine Zahl") from None
        if not 1 <= anzahl <= 6:
            raise Abgelehnt(400, "anzahl liegt außerhalb von 1 bis 6")

        url = transitous.adresse(self.konfig.quelle, von, nach, zeit, ankunft, anzahl)
        jetzt = self._uhr()
        with self._sperre:
            gemerkt = self._gemerkt.get(url)
            if gemerkt is not None and jetzt - gemerkt[0] < MERKEN_S:
                return gemerkt[1]
            self._raeume(jetzt)
            if len(self._abfragen) >= self.konfig.max_je_stunde:
                raise Abgelehnt(429, "Zu viele Abfragen in dieser Stunde. Bitte später noch einmal.")
            self._abfragen.append(jetzt)
        try:
            # Transitous liefert oft mehr als gewünscht; mehr als gefragt geht nicht weiter.
            ziel = nach if self.konfig.frueher_aussteigen else None
            ergebnis = transitous.forme_um(self._hole(url, self.konfig.kontakt), ziel)[:anzahl]
        except transitous.QuelleFehler as fehler:
            raise Abgelehnt(fehler.status, str(fehler)) from None
        if self.tafeln is not None:
            try:
                self.tafeln.ergaenze(ergebnis)
            except Exception:  # noqa: BLE001 - das Gleis ist eine Zugabe und darf die Verbindung nie verhindern
                LOG.warning("Gleise nicht ergänzt", exc_info=True)
        with self._sperre:
            if len(self._gemerkt) >= MAX_GEMERKT:
                self._gemerkt.clear()
            self._gemerkt[url] = (jetzt, ergebnis)
        return ergebnis


def _eins(werte: dict[str, list[str]], name: str) -> str | None:
    liste = werte.get(name)
    return liste[0].strip() if liste and liste[0].strip() else None


def _punkt(werte: dict[str, list[str]], name: str) -> tuple[float, float]:
    text = _eins(werte, name)
    if text is None:
        raise Abgelehnt(400, f"{name} fehlt (Breite,Länge)")
    teile = text.split(",")
    try:
        lat, lon = float(teile[0]), float(teile[1])
    except (ValueError, IndexError):
        raise Abgelehnt(400, f"{name} braucht die Form 50.1071,8.6638") from None
    # Die Vergleiche schlagen auch bei NaN fehl.
    if len(teile) != 2 or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise Abgelehnt(400, f"{name} liegt außerhalb der Erde")
    return lat, lon


def baue_server(dienst: Dienst, host: str, port: int) -> ThreadingHTTPServer:
    class Bearbeiter(BaseHTTPRequestHandler):
        server_version = "fahrplan-dienst"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Kein Zugriffsprotokoll: Start und Ziel einer Abfrage gehören nicht ins Log.
            return

        def _sende(self, status: int, inhalt: dict[str, Any]) -> None:
            daten = json.dumps(inhalt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self) -> None:  # noqa: N802
            teile = urlsplit(self.path)
            if teile.path == "/status":
                self._sende(200, {
                    "dienst": "fahrplan-dienst",
                    "version": VERSION,
                    "quelle": "Transitous",
                    "abfragen_letzte_stunde": dienst.abfragen_letzte_stunde(),
                    "max_je_stunde": dienst.konfig.max_je_stunde,
                    "gleis_quelle": "Bahnhofstafeln (DBF)" if dienst.tafeln is not None else None,
                    "tafeln_letzte_stunde": dienst.tafeln.abfragen_letzte_stunde() if dienst.tafeln else 0,
                    "frueher_aussteigen": dienst.konfig.frueher_aussteigen,
                })
                return
            if teile.path != "/verbindungen":
                self._sende(404, {"fehler": "Unbekannter Pfad"})
                return
            if not dienst.zugang_ok(self.headers.get(ZUGANG_KOPF)):
                self._sende(401, {"fehler": "Zugangsschlüssel fehlt oder ist falsch"})
                return
            try:
                verbindungen = dienst.verbindungen(parse_qs(teile.query, max_num_fields=10))
            except Abgelehnt as fehler:
                if fehler.status >= 500:
                    LOG.warning("Abfrage gescheitert: %s", fehler)
                self._sende(fehler.status, {"fehler": str(fehler)})
                return
            except ValueError:
                self._sende(400, {"fehler": "Zu viele Werte in der Abfrage"})
                return
            self._sende(200, {"quelle": "Transitous", "quellen_url": QUELLEN_URL, "verbindungen": verbindungen})

    return ThreadingHTTPServer((host, port), Bearbeiter)
