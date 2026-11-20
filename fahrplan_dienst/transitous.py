"""Abfrage bei Transitous (MOTIS-2-API) und Umformen in die kurze Antwort des Dienstes.

Die Antwort nennt je Verbindung Abfahrt, Ankunft, Dauer und Umstiege, und je Teilstück Linie, Richtung, Haltestellen,
Gleis (wenn die Quelle eines kennt), Zeiten nach Plan und in Echtzeit. Zeiten bleiben, wie Transitous sie liefert:
ISO 8601 in UTC. Die App rechnet sie in die Ortszeit um.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from . import VERSION

ZEITLIMIT_S = 20
MAX_ANTWORT = 4 * 1024 * 1024
FUSS = "FUSS"

# Verkehrsmittel von MOTIS → kurze deutsche Art. Unbekanntes wird zu „SONST“.
_ARTEN = {
    "WALK": FUSS,
    "HIGHSPEED_RAIL": "FERNZUG",
    "LONG_DISTANCE": "FERNZUG",
    "NIGHT_RAIL": "FERNZUG",
    "REGIONAL_FAST_RAIL": "REGIONALZUG",
    "REGIONAL_RAIL": "REGIONALZUG",
    "SUBURBAN": "S_BAHN",
    "METRO": "S_BAHN",
    "SUBWAY": "U_BAHN",
    "TRAM": "TRAM",
    "BUS": "BUS",
    "COACH": "FERNBUS",
    "FERRY": "FAEHRE",
}


class QuelleFehler(Exception):
    """Transitous antwortet nicht, lehnt ab oder liefert Unlesbares. `status` ist der Code für die eigene Antwort."""

    def __init__(self, text: str, status: int = 502):
        super().__init__(text)
        self.status = status


def nutzer_kennung(kontakt: str) -> str:
    """Kopfzeile User-Agent, wie Transitous sie verlangt: Name, Version und Kontakt."""
    return f"fahrplan-dienst/{VERSION} ({kontakt})"


def adresse(quelle: str, von: tuple[float, float], nach: tuple[float, float], zeit: str | None, ankunft: bool,
            anzahl: int) -> str:
    werte = {
        "fromPlace": f"{von[0]:.6f},{von[1]:.6f}",
        "toPlace": f"{nach[0]:.6f},{nach[1]:.6f}",
        "numItineraries": str(anzahl),
    }
    if zeit:
        werte["time"] = zeit
    if ankunft:
        werte["arriveBy"] = "true"
    return f"{quelle}/api/v1/plan?{urllib.parse.urlencode(werte)}"


def hole(url: str, kontakt: str) -> dict[str, Any]:
    """Eine Abfrage. Wirft QuelleFehler, wenn nichts Brauchbares kommt."""
    anfrage = urllib.request.Request(url, headers={"User-Agent": nutzer_kennung(kontakt), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT_S) as antwort:
            daten = antwort.read(MAX_ANTWORT + 1)
    except urllib.error.HTTPError as fehler:
        if fehler.code == 429:
            raise QuelleFehler("Transitous bremst gerade (zu viele Abfragen)", 503) from None
        raise QuelleFehler(f"Transitous antwortet mit {fehler.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QuelleFehler("Transitous ist nicht erreichbar", 504) from None
    if len(daten) > MAX_ANTWORT:
        raise QuelleFehler("Antwort von Transitous ist zu groß")
    try:
        roh = json.loads(daten.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise QuelleFehler("Antwort von Transitous ist nicht lesbar") from None
    if not isinstance(roh, dict):
        raise QuelleFehler("Antwort von Transitous hat ein unbekanntes Format")
    return roh


# ---------------------------------------------------------------- Umformen


def forme_um(roh: dict[str, Any]) -> list[dict[str, Any]]:
    """Alle Verbindungen der Antwort in der kurzen Form. Kaputte Einträge fallen weg."""
    verbindungen = []
    for eintrag in roh.get("itineraries") or []:
        v = _verbindung(eintrag)
        if v is not None:
            verbindungen.append(v)
    return verbindungen


def _verbindung(eintrag: Any) -> dict[str, Any] | None:
    if not isinstance(eintrag, dict):
        return None
    teile = [t for t in (_teil(l) for l in eintrag.get("legs") or []) if t is not None]
    ab, an = _text(eintrag.get("startTime")), _text(eintrag.get("endTime"))
    if not teile or not ab or not an:
        return None
    dauer = eintrag.get("duration")
    return {
        "ab": ab,
        "an": an,
        "dauer_min": round(dauer / 60) if isinstance(dauer, (int, float)) else _minuten(ab, an),
        "umstiege": max(0, sum(1 for t in teile if t["art"] != FUSS) - 1),
        "teile": teile,
    }


def _teil(leg: Any) -> dict[str, Any] | None:
    if not isinstance(leg, dict):
        return None
    von, nach = leg.get("from"), leg.get("to")
    if not isinstance(von, dict) or not isinstance(nach, dict):
        return None
    ab, an = _text(leg.get("startTime")), _text(leg.get("endTime"))
    if not ab or not an:
        return None
    ab_plan = _text(leg.get("scheduledStartTime")) or ab
    an_plan = _text(leg.get("scheduledEndTime")) or an
    art = _ARTEN.get(_text(leg.get("mode")) or "", "SONST")
    teil: dict[str, Any] = {
        "art": art,
        "von": _halt(von, ab_plan, ab),
        "nach": _halt(nach, an_plan, an),
        "dauer_min": _minuten(ab, an),
    }
    if art == FUSS:
        strecke = leg.get("distance")
        if isinstance(strecke, (int, float)):
            teil["meter"] = round(strecke)
        return teil
    teil["linie"] = _text(leg.get("displayName")) or _text(leg.get("routeShortName")) or ""
    teil["richtung"] = _text(leg.get("headsign")) or ""
    teil["betreiber"] = _text(leg.get("agencyName")) or ""
    teil["echtzeit"] = leg.get("realTime") is True
    teil["ausfall"] = leg.get("cancelled") is True
    teil["verspaetung_min"] = max(0, _minuten(ab_plan, ab))
    halte = leg.get("intermediateStops")
    teil["zwischenhalte"] = len(halte) if isinstance(halte, list) else 0
    return teil


def _halt(ort: dict[str, Any], plan: str, echt: str) -> dict[str, Any]:
    halt: dict[str, Any] = {"name": _text(ort.get("name")) or "", "plan": plan, "zeit": echt}
    # „track“ ist das aktuelle Gleis, „scheduledTrack“ das geplante. Viele Regionalfahrpläne nennen keines.
    gleis = _text(ort.get("track")) or _text(ort.get("scheduledTrack"))
    if gleis:
        halt["gleis"] = gleis
        geplant = _text(ort.get("scheduledTrack"))
        if geplant and geplant != gleis:
            halt["gleis_plan"] = geplant
    lat, lon = ort.get("lat"), ort.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        halt["lat"], halt["lon"] = round(lat, 6), round(lon, 6)
    return halt


def _text(wert: Any) -> str | None:
    if not isinstance(wert, str):
        return None
    wert = wert.strip()
    return wert[:200] if wert else None


def _minuten(von: str, bis: str) -> int:
    """Ganze Minuten zwischen zwei ISO-Zeiten; 0, wenn eine nicht lesbar ist."""
    try:
        a = datetime.fromisoformat(von.replace("Z", "+00:00"))
        b = datetime.fromisoformat(bis.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return round((b - a).total_seconds() / 60)
