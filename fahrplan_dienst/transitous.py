"""Abfrage bei Transitous (MOTIS-2-API) und Umformen in die kurze Antwort des Dienstes.

Die Antwort nennt je Verbindung Abfahrt, Ankunft, Dauer und Umstiege, und je Teilstück Linie, Richtung, Haltestellen,
Gleis (wenn die Quelle eines kennt), Zeiten nach Plan und in Echtzeit. Zeiten bleiben, wie Transitous sie liefert:
ISO 8601 in UTC. Die App rechnet sie in die Ortszeit um.

Jedes Teilstück trägt außerdem seinen Streckenverlauf als "weg": Punkte [Breite, Länge] vom Einstieg zum Ausstieg,
höchstens MAX_PUNKTE. Transitous liefert ihn je Abschnitt in "legGeometry" als kodierte Polylinie mit eigener
Genauigkeit ("precision", bei /api/v1/plan 7 Nachkommastellen).

„Früher aussteigen“: Transitous lässt eine Fahrt manchmal einen Halt zu weit laufen und schickt den Fahrgast zu Fuß
zurück. Kennt der Dienst das Ziel der Anfrage, prüft er deshalb die Zwischenhalte der letzten Fahrt und lässt sie an
dem Halt enden, von dem man früher oder mit deutlich kürzerem Fußweg ankommt. Die Verbindung trägt dann
"angepasst": "frueher_aussteigen".

„Später aussteigen“: Manchmal wählt Transitous einen Halt nie als Ausstieg (etwa wenn der Bahnsteig in OSM nicht an
die Fußwege angebunden ist) und schickt den Fahrgast vom Halt davor weit zu Fuß. Ist der Fußweg am Ende länger als
MIN_FUSSWEG_SPAETER_M, holt der Dienst die Halte derselben Fahrt nach dem Ausstieg (/api/v1/trip) und lässt die Fahrt
an einem der nächsten MAX_HALTE_SPAETER Halte enden, wenn man von dort früher oder mit deutlich kürzerem Fußweg
ankommt. Die Verbindung trägt dann "angepasst": "spaeter_aussteigen". Wie oft er Fahrten nachfragt, begrenzt der
Aufrufer über `hole_fahrt` (server.py).
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import VERSION

ZEITLIMIT_S = 20
MAX_ANTWORT = 4 * 1024 * 1024
FUSS = "FUSS"
# So viele Punkte trägt der Streckenverlauf eines Teilstücks höchstens; mehr wird gleichmäßig ausgedünnt.
MAX_PUNKTE = 120
# Längere Polylinien liest der Dienst nicht (der Verlauf eines ICE quer durchs Land braucht etwa 50.000 Zeichen).
MAX_POLYLINIE = 400_000
# Früher aussteigen: Der Fußweg ist nur als Luftlinie bekannt. Umweg und Gehtempo sind vorsichtig geschätzt.
UMWEG = 1.3
GEHTEMPO_M_S = 1.25
# Ein früherer Ausstieg lohnt, wenn man so viele Minuten eher am Ziel ist …
MIN_FRUEHER_MIN = 2
# … oder nicht später ankommt und so viele Meter weniger läuft.
MIN_KUERZER_M = 300
FRUEHER_AUSSTEIGEN = "frueher_aussteigen"
SPAETER_AUSSTEIGEN = "spaeter_aussteigen"
# Später aussteigen: nur prüfen, wenn der Fußweg am Ende (Luftlinie × UMWEG) länger ist …
MIN_FUSSWEG_SPAETER_M = 300
# … und höchstens so viele Halte hinter dem geplanten Ausstieg.
MAX_HALTE_SPAETER = 3

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


def fahrt_adresse(quelle: str, trip_id: str) -> str:
    """Abfrage aller Halte einer Fahrt."""
    return f"{quelle}/api/v1/trip?{urllib.parse.urlencode({'tripId': trip_id})}"


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


def forme_um(roh: dict[str, Any], ziel: tuple[float, float] | None = None, *, frueher: bool = True,
             hole_fahrt: Callable[[str], dict[str, Any] | None] | None = None,
             anzahl: int | None = None) -> list[dict[str, Any]]:
    """Die Verbindungen der Antwort in der kurzen Form, höchstens `anzahl`. Kaputte Einträge fallen weg.

    `ziel` ist das Ziel der Anfrage (Breite, Länge). Mit ihm prüft der Dienst, ob man eher aussteigen sollte
    (abschaltbar mit `frueher=False`); ohne bleibt jede Verbindung, wie Transitous sie liefert.
    `hole_fahrt(tripId)` liefert die Antwort von /api/v1/trip oder None. Nur mit ihr (und `ziel`) prüft der Dienst,
    ob man später aussteigen sollte. Gefragt wird sie nur für Verbindungen, die in die Antwort kommen.
    """
    verbindungen = []
    for eintrag in roh.get("itineraries") or []:
        if anzahl is not None and len(verbindungen) >= anzahl:
            break
        v = _verbindung(eintrag, ziel, frueher, hole_fahrt)
        if v is not None:
            verbindungen.append(v)
    return verbindungen


def _verbindung(eintrag: Any, ziel: tuple[float, float] | None = None, frueher: bool = True,
                hole_fahrt: Callable[[str], dict[str, Any] | None] | None = None) -> dict[str, Any] | None:
    if not isinstance(eintrag, dict):
        return None
    paare = [(leg, teil) for leg, teil in ((l, _teil(l)) for l in eintrag.get("legs") or []) if teil is not None]
    teile = [teil for _, teil in paare]
    ab, an = _text(eintrag.get("startTime")), _text(eintrag.get("endTime"))
    if not teile or not ab or not an:
        return None
    dauer = eintrag.get("duration")
    verbindung = {
        "ab": ab,
        "an": an,
        "dauer_min": round(dauer / 60) if isinstance(dauer, (int, float)) else _minuten(ab, an),
        "umstiege": max(0, sum(1 for t in teile if t["art"] != FUSS) - 1),
        "teile": teile,
    }
    if ziel is not None and len(paare) >= 2:
        if frueher:
            try:
                _steige_frueher_aus(verbindung, paare[-2][0], ziel)
            except Exception:  # noqa: BLE001 - eine Zugabe; im Zweifel bleibt die Verbindung, wie sie kam
                pass
        if hole_fahrt is not None and "angepasst" not in verbindung:
            try:
                _steige_spaeter_aus(verbindung, paare[-2][0], ziel, hole_fahrt)
            except Exception:  # noqa: BLE001 - wie oben
                pass
    return verbindung


# ---------------------------------------------------------------- Früher aussteigen


def _steige_frueher_aus(verbindung: dict[str, Any], leg: dict[str, Any], ziel: tuple[float, float]) -> None:
    """Lässt die letzte Fahrt an einem Zwischenhalt enden, wenn man von dort besser ans Ziel kommt.

    `leg` ist der Abschnitt der Rohantwort zur letzten Fahrt. Geprüft wird nur, wenn nach ihr nichts als ein Fußweg
    zum Ziel folgt. Erst wenn alles Neue fertig gerechnet ist, wird die Verbindung geändert.
    """
    teile = verbindung["teile"]
    fahrt, fussweg = teile[-2], teile[-1]
    zielpunkt = _punkt(ziel[0], ziel[1])
    halte = leg.get("intermediateStops")
    if (fussweg["art"] != FUSS or fahrt["art"] == FUSS or fahrt.get("ausfall") or zielpunkt is None
            or not isinstance(halte, list)):
        return
    bisher_an = _zeit(fussweg["nach"]["zeit"])
    if bisher_an is None:
        return
    bisher_meter = _fussweg_meter(fussweg, fahrt, ziel)

    bester: tuple[_Ausstieg, int] | None = None
    for nummer, halt in enumerate(halte):
        kandidat = _bewerte(halt, ziel, bisher_an, bisher_meter)
        # Die früheste Ankunft am Ziel gewinnt, bei Gleichstand der kürzere Fußweg.
        if kandidat is not None and (bester is None or kandidat.rang < bester[0].rang):
            bester = (kandidat, nummer)
    if bester is None:
        return
    ausstieg, nummer = bester
    _setze_ausstieg(verbindung, ausstieg, zielpunkt, nummer, _weg_bis(leg, fahrt["von"], ausstieg.punkt),
                    FRUEHER_AUSSTEIGEN)


class _Ausstieg:
    """Ein Halt, an dem sich der Ausstieg lohnt, mit allem, was der neue Fußweg braucht."""

    def __init__(self, halt: dict[str, Any], punkt: list[float], ankunft: datetime, meter: int, minuten: int):
        self.halt, self.punkt, self.ankunft, self.meter, self.minuten = halt, punkt, ankunft, meter, minuten
        self.am_ziel = ankunft + timedelta(minutes=minuten)
        self.rang = (self.am_ziel, meter)


def _bewerte(halt: Any, ziel: tuple[float, float], bisher_an: datetime, bisher_meter: int | None) -> _Ausstieg | None:
    """Der Halt als möglicher Ausstieg; None, wenn er nicht taugt oder sich nicht lohnt.

    Er lohnt, wenn man mindestens MIN_FRUEHER_MIN Minuten früher am Ziel ist, oder nicht später und mindestens
    MIN_KUERZER_M weniger läuft.
    """
    if not isinstance(halt, dict) or halt.get("cancelled") is True or halt.get("dropoffType") == "NOT_ALLOWED":
        return None
    punkt = _punkt(halt.get("lat"), halt.get("lon"))
    ankunft = _zeit(_text(halt.get("arrival")) or "")
    if punkt is None or ankunft is None:
        return None
    # Gemessen wird mit den genauen Koordinaten, nicht mit den für die Karte gerundeten.
    meter = round(luftlinie_m((halt["lat"], halt["lon"]), ziel) * UMWEG)
    kandidat = _Ausstieg(halt, punkt, ankunft, meter, gehzeit_min(meter))
    frueher = kandidat.am_ziel <= bisher_an - timedelta(minutes=MIN_FRUEHER_MIN)
    kuerzer = kandidat.am_ziel <= bisher_an and bisher_meter is not None and bisher_meter - meter >= MIN_KUERZER_M
    return kandidat if frueher or kuerzer else None


def _setze_ausstieg(verbindung: dict[str, Any], ausstieg: _Ausstieg, zielpunkt: list[float], zwischenhalte: int,
                    weg: list[list[float]] | None, grund: str) -> None:
    """Lässt die letzte Fahrt am neuen Halt enden, ersetzt den Fußweg und rechnet die Verbindung neu."""
    teile = verbindung["teile"]
    fahrt, fussweg = teile[-2], teile[-1]
    halt = ausstieg.halt
    aus_zeit, an_zeit = _iso(ausstieg.ankunft), _iso(ausstieg.am_ziel)
    neue_fahrt = dict(fahrt, nach=_halt(halt, _text(halt.get("scheduledArrival")) or aus_zeit, aus_zeit),
                      dauer_min=_minuten(fahrt["von"]["zeit"], aus_zeit), zwischenhalte=zwischenhalte)
    if weg is not None:
        neue_fahrt["weg"] = weg
    else:
        neue_fahrt.pop("weg", None)
    neuer_fussweg = {
        "art": FUSS,
        "von": _halt(halt, aus_zeit, aus_zeit, mit_gleis=False),
        "nach": dict(fussweg["nach"], plan=an_zeit, zeit=an_zeit),
        "dauer_min": ausstieg.minuten,
        "weg": [ausstieg.punkt, zielpunkt],
        "meter": ausstieg.meter,
    }
    teile[-2:] = [neue_fahrt, neuer_fussweg]
    verbindung["an"] = an_zeit
    verbindung["dauer_min"] = _minuten(verbindung["ab"], an_zeit)
    verbindung["angepasst"] = grund


# ---------------------------------------------------------------- Später aussteigen


def _steige_spaeter_aus(verbindung: dict[str, Any], leg: dict[str, Any], ziel: tuple[float, float],
                        hole_fahrt: Callable[[str], dict[str, Any] | None]) -> None:
    """Lässt die letzte Fahrt erst an einem der nächsten Halte enden, wenn man von dort besser ans Ziel kommt.

    `leg` ist der Abschnitt der Rohantwort zur letzten Fahrt. Die Halte nach dem geplanten Ausstieg kennt nur die
    Antwort von /api/v1/trip; gefragt wird sie erst, wenn der Fußweg am Ende lang genug ist. Erst wenn alles Neue
    fertig gerechnet ist, wird die Verbindung geändert.
    """
    teile = verbindung["teile"]
    fahrt, fussweg = teile[-2], teile[-1]
    zielpunkt = _punkt(ziel[0], ziel[1])
    trip_id = _text(leg.get("tripId"))
    if fussweg["art"] != FUSS or fahrt["art"] == FUSS or fahrt.get("ausfall") or zielpunkt is None or not trip_id:
        return
    bisher_an = _zeit(fussweg["nach"]["zeit"])
    bisher_aus = _zeit(fahrt["nach"]["zeit"])
    alt = leg.get("to")
    if bisher_an is None or bisher_aus is None or _punkt(alt.get("lat"), alt.get("lon")) is None:
        return
    # Die Schwelle misst wie der neue Fußweg: Luftlinie mal Umweg, mit den genauen Koordinaten.
    if luftlinie_m((alt["lat"], alt["lon"]), ziel) * UMWEG <= MIN_FUSSWEG_SPAETER_M:
        return
    bisher_meter = _fussweg_meter(fussweg, fahrt, ziel)

    fahrt_roh = hole_fahrt(trip_id)
    trip_leg = _fahrt_leg(fahrt_roh, trip_id)
    if trip_leg is None:
        return
    halte = [trip_leg.get("from")] + list(trip_leg.get("intermediateStops") or []) + [trip_leg.get("to")]
    stelle = _finde_ausstieg(halte, leg)
    if stelle is None:
        return

    bester: tuple[_Ausstieg, int] | None = None
    for schritt, halt in enumerate(halte[stelle + 1:stelle + 1 + MAX_HALTE_SPAETER]):
        kandidat = _bewerte(halt, ziel, bisher_an, bisher_meter)
        # Ein späterer Halt kann nicht vor dem geplanten Ausstieg erreicht sein; sonst passt die Antwort nicht.
        if kandidat is None or kandidat.ankunft < bisher_aus:
            continue
        if bester is None or kandidat.rang < bester[0].rang:
            bester = (kandidat, schritt)
    if bester is None:
        return
    ausstieg, schritt = bester
    # Der alte Ausstieg und alle Halte bis zum neuen werden zu Zwischenhalten.
    zwischenhalte = fahrt["zwischenhalte"] + 1 + schritt
    weg = _weg_weiter(fahrt.get("weg"), trip_leg, halte[stelle], ausstieg.punkt)
    _setze_ausstieg(verbindung, ausstieg, zielpunkt, zwischenhalte, weg, SPAETER_AUSSTEIGEN)


def _fahrt_leg(roh: Any, trip_id: str) -> dict[str, Any] | None:
    """Der Abschnitt der Fahrt aus der Antwort von /api/v1/trip; None, wenn sie nicht passt."""
    if not isinstance(roh, dict):
        return None
    for leg in roh.get("legs") or []:
        if (isinstance(leg, dict) and leg.get("mode") != "WALK" and _text(leg.get("tripId")) in (None, trip_id)
                and isinstance(leg.get("from"), dict) and isinstance(leg.get("to"), dict)
                and isinstance(leg.get("intermediateStops") or [], list)):
            return leg
    return None


def _finde_ausstieg(halte: list[Any], leg: dict[str, Any]) -> int | None:
    """Stelle des geplanten Ausstiegs unter allen Halten der Fahrt.

    Er muss zur selben Zeit (nach Plan) ankommen und dieselbe Haltestelle sein: gleiche stopId, sonst gleicher Name
    oder höchstens 100 m entfernt. Eine Ringlinie hält so nur einmal zur passenden Zeit.
    """
    alt = leg["to"]
    plan = _zeit(_text(alt.get("scheduledArrival")) or _text(leg.get("scheduledEndTime")) or "")
    if plan is None:
        return None
    for stelle, halt in enumerate(halte):
        if not isinstance(halt, dict):
            continue
        if _zeit(_text(halt.get("scheduledArrival")) or _text(halt.get("arrival")) or "") != plan:
            continue
        kennung, alt_kennung = _text(halt.get("stopId")), _text(alt.get("stopId"))
        if kennung and alt_kennung:
            if kennung == alt_kennung:
                return stelle
            continue
        if _text(halt.get("name")) and _text(halt.get("name")) == _text(alt.get("name")):
            return stelle
        if (_punkt(halt.get("lat"), halt.get("lon")) is not None
                and luftlinie_m((halt["lat"], halt["lon"]), (alt["lat"], alt["lon"])) <= 100):
            return stelle
    return None


def _weg_weiter(bisher: Any, trip_leg: dict[str, Any], alt: dict[str, Any],
                neu: list[float]) -> list[list[float]] | None:
    """Streckenverlauf der Fahrt bis zum neuen Ausstieg: der bisherige, verlängert um das Stück der ganzen Fahrt vom
    alten Ausstieg bis zum neuen. Ohne brauchbare Geometrie der Fahrt die gerade Linie zum neuen Halt; ohne
    bisherigen Verlauf keiner."""
    if not isinstance(bisher, list) or len(bisher) < 2:
        return None
    try:
        ganz = _aus_geometrie(trip_leg.get("legGeometry"))
    except (ValueError, TypeError, OverflowError, ArithmeticError):
        ganz = []
    stueck: list[list[float]] = []
    alt_punkt = _punkt(alt.get("lat"), alt.get("lon"))
    if len(ganz) >= 2 and alt_punkt is not None:
        von = min(range(len(ganz)), key=lambda i: luftlinie_m(ganz[i], alt_punkt))
        bis = min(range(von, len(ganz)), key=lambda i: luftlinie_m(ganz[i], neu))
        stueck = ganz[von + 1:bis + 1]
    weg = list(bisher)
    for punkt in stueck + [neu]:
        if weg[-1] != punkt:
            weg.append(punkt)
    return duenne_aus(weg)


def luftlinie_m(a: list[float] | tuple[float, float], b: list[float] | tuple[float, float]) -> float:
    """Entfernung zweier Punkte (Breite, Länge) in Metern auf der Kugel."""
    lat1, lon1, lat2, lon2 = (math.radians(w) for w in (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


def gehzeit_min(meter: float) -> int:
    """Gehzeit für einen Fußweg dieser Länge: auf volle Minuten aufgerundet, mindestens eine."""
    return max(1, math.ceil(meter / GEHTEMPO_M_S / 60))


def _fussweg_meter(fussweg: dict[str, Any], fahrt: dict[str, Any], ziel: tuple[float, float]) -> int | None:
    """Länge des bisherigen Fußwegs: wie Transitous sie nennt, sonst geschätzt wie bei einem neuen Ausstieg."""
    if "meter" in fussweg:
        return fussweg["meter"]
    ausstieg = _punkt(fahrt["nach"].get("lat"), fahrt["nach"].get("lon"))
    if ausstieg is None:
        return None
    return round(luftlinie_m((fahrt["nach"]["lat"], fahrt["nach"]["lon"]), ziel) * UMWEG)


def _weg_bis(leg: dict[str, Any], von: dict[str, Any], halt: list[float]) -> list[list[float]] | None:
    """Streckenverlauf der Fahrt bis zu einem Zwischenhalt: am Punkt abgeschnitten, der dem Halt am nächsten
    liegt, und mit dem Halt als Ende. Ohne brauchbare Geometrie die gerade Linie vom Einstieg zum Halt."""
    try:
        weg = _aus_geometrie(leg.get("legGeometry"))
    except (ValueError, TypeError, OverflowError, ArithmeticError):
        weg = []
    if len(weg) >= 2:
        naechster = min(range(len(weg)), key=lambda i: luftlinie_m(weg[i], halt))
        weg = weg[:naechster + 1]
        if weg[-1] != halt:
            weg.append(halt)
        if len(weg) >= 2:
            return duenne_aus(weg)
    start = _punkt(von.get("lat"), von.get("lon"))
    return [start, halt] if start is not None else None


def _zeit(iso: str) -> datetime | None:
    """Eine ISO-Zeit mit Zeitzone als Zeitpunkt in UTC; None, wenn sie nicht lesbar ist."""
    try:
        zeit = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return zeit.astimezone(timezone.utc) if zeit.tzinfo is not None else None


def _iso(zeit: datetime) -> str:
    return zeit.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        # Ein Fußweg hat kein Gleis, auch wenn er an einem Bahnsteig beginnt oder endet.
        "von": _halt(von, ab_plan, ab, mit_gleis=art != FUSS),
        "nach": _halt(nach, an_plan, an, mit_gleis=art != FUSS),
        "dauer_min": _minuten(ab, an),
    }
    weg = _weg(leg, von, nach)
    if weg is not None:
        teil["weg"] = weg
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


def _halt(ort: dict[str, Any], plan: str, echt: str, mit_gleis: bool = True) -> dict[str, Any]:
    halt: dict[str, Any] = {"name": _text(ort.get("name")) or "", "plan": plan, "zeit": echt}
    # „track“ ist das aktuelle Gleis, „scheduledTrack“ das geplante. Viele Regionalfahrpläne nennen keines.
    gleis = (_text(ort.get("track")) or _text(ort.get("scheduledTrack"))) if mit_gleis else None
    if gleis:
        halt["gleis"] = gleis
        geplant = _text(ort.get("scheduledTrack"))
        if geplant and geplant != gleis:
            halt["gleis_plan"] = geplant
    lat, lon = ort.get("lat"), ort.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        halt["lat"], halt["lon"] = round(lat, 6), round(lon, 6)
    return halt


def dekodiere(text: str, genauigkeit: int) -> list[tuple[float, float]]:
    """Kodierte Polylinie (Verfahren von Google) → Punkte (Breite, Länge) in Dezimalgrad.

    `genauigkeit` ist die Zahl der Nachkommastellen, mit der kodiert wurde. Wirft ValueError, wenn der Text keine
    Polylinie ist oder mitten in einer Zahl endet.
    """
    teiler = 10 ** genauigkeit
    punkte: list[tuple[float, float]] = []
    werte = [0, 0]  # Breite und Länge als ganze Zahlen; jede Zahl im Text ist der Abstand zum Punkt davor
    achse = 0
    zahl = schub = 0
    for zeichen in text:
        stueck = ord(zeichen) - 63
        if not 0 <= stueck <= 63:
            raise ValueError("Zeichen gehört nicht zu einer Polylinie")
        zahl |= (stueck & 0x1F) << schub
        schub += 5
        if stueck & 0x20:  # die Zahl geht im nächsten Zeichen weiter
            if schub > 60:
                raise ValueError("Zahl in der Polylinie ist zu lang")
            continue
        werte[achse] += ~(zahl >> 1) if zahl & 1 else zahl >> 1
        zahl = schub = 0
        if achse == 1:
            punkte.append((werte[0] / teiler, werte[1] / teiler))
        achse = 1 - achse
    if schub or achse:
        raise ValueError("Polylinie endet mitten in einem Punkt")
    return punkte


def duenne_aus(punkte: list[Any], hoechstens: int = MAX_PUNKTE) -> list[Any]:
    """Höchstens `hoechstens` Punkte in gleichmäßigen Abständen; der erste und der letzte bleiben."""
    anzahl = len(punkte)
    if anzahl <= hoechstens or hoechstens < 2:
        return list(punkte)
    return [punkte[round(i * (anzahl - 1) / (hoechstens - 1))] for i in range(hoechstens)]


def _punkt(lat: Any, lon: Any) -> list[float] | None:
    """[Breite, Länge] auf fünf Nachkommastellen (etwa ein Meter); None bei unsinnigen Werten."""
    for wert in (lat, lon):
        if isinstance(wert, bool) or not isinstance(wert, (int, float)) or not math.isfinite(wert):
            return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return [round(lat, 5), round(lon, 5)]


def _weg(leg: dict[str, Any], von: dict[str, Any], nach: dict[str, Any]) -> list[list[float]] | None:
    """Streckenverlauf eines Abschnitts. Ohne brauchbare Geometrie die gerade Linie vom Einstieg zum Ausstieg,
    ohne deren Koordinaten None. Wirft nie."""
    try:
        weg = _aus_geometrie(leg.get("legGeometry"))
        if len(weg) >= 2:
            return duenne_aus(weg)
    except (ValueError, TypeError, OverflowError, ArithmeticError):
        pass
    start, ziel = _punkt(von.get("lat"), von.get("lon")), _punkt(nach.get("lat"), nach.get("lon"))
    return [start, ziel] if start is not None and ziel is not None else None


def _aus_geometrie(geometrie: Any) -> list[list[float]]:
    """Punkte aus "legGeometry"; leer, wenn sie fehlt. Unsinnige Punkte und Doppelte in Folge fallen weg."""
    if not isinstance(geometrie, dict):
        return []
    text, genauigkeit = geometrie.get("points"), geometrie.get("precision")
    if not isinstance(text, str) or len(text) > MAX_POLYLINIE:
        return []
    # Die Genauigkeit wird nicht geraten: Mit der falschen läge der ganze Weg woanders.
    if isinstance(genauigkeit, bool) or not isinstance(genauigkeit, int) or not 1 <= genauigkeit <= 9:
        return []
    weg: list[list[float]] = []
    for lat, lon in dekodiere(text, genauigkeit):
        punkt = _punkt(lat, lon)
        if punkt is not None and (not weg or weg[-1] != punkt):
            weg.append(punkt)
    return weg


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
