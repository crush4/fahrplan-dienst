"""Tests ohne Netz: „später aussteigen“ an einem Halt hinter dem geplanten Ausstieg derselben Fahrt."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from fahrplan_dienst import transitous
from fahrplan_dienst.konfig import Konfig
from fahrplan_dienst.server import FAHRT_MERKEN_S, Abgelehnt, Dienst

from .test_aussteigen import GRAD_JE_METER, ZIEL, kodiere, ort, zeit

# Gekürzte echte Antwort von Transitous: Station Industriehof → Opernplatz in Frankfurt (U6 bis zur Hauptwache).
ECHT = json.loads((Path(__file__).parent / "daten" / "plan_industriehof_opernplatz.json").read_text(encoding="utf-8"))
OPERNPLATZ = (50.1157, 8.6717)


def echter_fall() -> tuple[dict, dict]:
    """Plan und Fahrt wie am Opernplatz beobachtet: Transitous lässt die U6 an der Westend enden (950 m zu Fuß),
    dieselbe Fahrt hält eine Minute später an der Alten Oper. Die Fahrt ist die ungekürzte U6 aus ECHT."""
    plan = {"itineraries": [copy.deepcopy(ECHT["itineraries"][0])]}
    legs = plan["itineraries"][0]["legs"]
    u6 = legs[1]
    fahrt = {"legs": [copy.deepcopy(u6)]}
    westend = u6["intermediateStops"][3]
    assert westend["name"] == "Frankfurt (Main) Westend"
    u6["intermediateStops"] = u6["intermediateStops"][:3]
    u6["to"] = {k: v for k, v in westend.items() if not k.startswith("departure") and k != "scheduledDeparture"}
    u6["endTime"] = u6["scheduledEndTime"] = westend["arrival"]
    # Verlauf des Plans bis zur Westend, wie Transitous ihn für den kürzeren Abschnitt schicken würde.
    punkte = transitous.dekodiere(u6["legGeometry"]["points"], u6["legGeometry"]["precision"])
    ende = min(range(len(punkte)), key=lambda i: transitous.luftlinie_m(punkte[i], (westend["lat"], westend["lon"])))
    u6["legGeometry"] = {"points": kodiere(punkte[:ende + 1], 7), "precision": 7}
    fuss = legs[2]
    fuss["from"] = u6["to"]
    fuss["startTime"] = fuss["scheduledStartTime"] = westend["arrival"]
    fuss["endTime"] = fuss["scheduledEndTime"] = "2026-11-23T07:12:00Z"
    fuss["distance"] = 950
    fuss.pop("legGeometry", None)
    plan["itineraries"][0]["endTime"] = "2026-11-23T07:12:00Z"
    plan["itineraries"][0].pop("duration", None)
    return plan, fahrt


# Erfundene Strecke auf einem Längengrad wie in test_aussteigen: Ziel bei 0 m, Start 5 km nördlich. Der Plan endet
# 800 m vor dem Ziel; die Fahrt hält danach bei 50 m vor, 400 m und 1500 m hinter dem Ziel.
def halt(name: str, meter: float, an: str, **mehr) -> dict:
    return {"name": name, **ort(meter), "arrival": zeit(an), "scheduledArrival": zeit(an), "departure": zeit(an),
            "scheduledDeparture": zeit(an), "cancelled": False, "dropoffType": "NORMAL", **mehr}


AUSSTIEG = halt("Zu früh", 800, "09:25")
DANACH = [halt("Am Ziel", 50, "09:27"), halt("Weiter", -400, "09:29"), halt("Ende", -1500, "09:32")]


def plan(trip_id: str | None = "fahrt-1", am_ziel: str = "09:39", fuss_meter: float | None = 1000,
         aus: dict = AUSSTIEG, **fahrt) -> dict:
    leg = {"mode": "BUS", "displayName": "30", "headsign": "Ende", "cancelled": False,
           "from": {"name": "Start", **ort(5000)}, "to": {k: v for k, v in aus.items() if "eparture" not in k},
           "scheduledStartTime": zeit("09:10"), "startTime": zeit("09:10"),
           "scheduledEndTime": aus["scheduledArrival"], "endTime": aus["arrival"],
           "intermediateStops": [halt("Mitte", 3000, "09:17")], **fahrt}
    if trip_id is not None:
        leg["tripId"] = trip_id
    fussweg = {"mode": "WALK", "from": leg["to"], "to": {"name": "END", "lat": ZIEL[0], "lon": ZIEL[1]},
               "startTime": aus["arrival"], "endTime": zeit(am_ziel)}
    if fuss_meter is not None:
        fussweg["distance"] = fuss_meter
    return {"itineraries": [{"startTime": zeit("09:10"), "endTime": zeit(am_ziel), "legs": [leg, fussweg]}]}


def fahrt(danach: list = DANACH, trip_id: str = "fahrt-1", aus: dict = AUSSTIEG, **mehr) -> dict:
    """Antwort von /api/v1/trip: die ganze Fahrt vom Start bis zum letzten Halt."""
    halte = [halt("Mitte", 3000, "09:17"), aus] + danach
    return {"legs": [{"mode": "BUS", "tripId": trip_id, "from": {"name": "Start", **ort(5000)}, "to": halte[-1],
                      "intermediateStops": halte[:-1], "startTime": zeit("09:10"),
                      "endTime": halte[-1]["arrival"], **mehr}]}


class Holer:
    """Ersatz für den Holer des Dienstes: zählt die Abfragen und liefert vorbereitete Fahrten."""

    def __init__(self, antworten: dict | None = None, fehler: Exception | None = None):
        self.antworten = antworten or {}
        self.fehler = fehler
        self.gefragt: list[str] = []

    def __call__(self, trip_id: str):
        self.gefragt.append(trip_id)
        if self.fehler is not None:
            raise self.fehler
        return self.antworten.get(trip_id)


def forme(roh: dict, holer: Holer, ziel=ZIEL, **mehr) -> dict:
    return transitous.forme_um(roh, ziel, hole_fahrt=holer, **mehr)[0]


class EchterFall(unittest.TestCase):
    def test_ausstieg_alte_oper_statt_westend(self):
        roh, u6 = echter_fall()
        vorher = transitous.forme_um(roh, OPERNPLATZ)[0]
        self.assertNotIn("angepasst", vorher)
        self.assertEqual(("Frankfurt (Main) Westend", 3), (vorher["teile"][1]["nach"]["name"],
                                                           vorher["teile"][1]["zwischenhalte"]))

        holer = Holer({"20261123_07:50_de-DELFI_3031889926": u6})
        n = forme(roh, holer, OPERNPLATZ)
        self.assertEqual(["20261123_07:50_de-DELFI_3031889926"], holer.gefragt)
        self.assertEqual("spaeter_aussteigen", n["angepasst"])
        self.assertEqual(("2026-11-23T06:53:00Z", "2026-11-23T07:02:00Z", 9, 0),
                         (n["ab"], n["an"], n["dauer_min"], n["umstiege"]))
        hin, bahn, fuss = n["teile"]
        self.assertEqual(vorher["teile"][0], hin)
        self.assertEqual(vorher["teile"][1]["von"], bahn["von"])
        self.assertEqual(("U_BAHN", "U6", 4, 6), (bahn["art"], bahn["linie"], bahn["zwischenhalte"], bahn["dauer_min"]))
        self.assertEqual({"name": "Frankfurt (Main) Alte Oper", "plan": "2026-11-23T07:00:00Z",
                          "zeit": "2026-11-23T07:00:00Z", "lat": 50.115314, "lon": 8.670851}, bahn["nach"])
        self.assertEqual(("FUSS", 96, 2, [[50.11531, 8.67085], [50.1157, 8.6717]]),
                         (fuss["art"], fuss["meter"], fuss["dauer_min"], fuss["weg"]))
        self.assertEqual(("Frankfurt (Main) Alte Oper", "2026-11-23T07:02:00Z"), (fuss["von"]["name"], fuss["nach"]["zeit"]))

        # Der Verlauf beginnt wie bisher und reicht jetzt über die Westend hinaus bis zur Alten Oper.
        alt, neu = vorher["teile"][1]["weg"], bahn["weg"]
        self.assertEqual(alt, neu[:len(alt)])
        self.assertGreater(len(neu), len(alt) + 1)
        self.assertEqual([50.11531, 8.67085], neu[-1])
        self.assertLess(max(p[1] for p in neu), 8.6710)

    def test_ohne_fahrt_bleibt_alles(self):
        roh, _ = echter_fall()
        self.assertEqual(transitous.forme_um(roh, OPERNPLATZ), transitous.forme_um(roh, OPERNPLATZ, hole_fahrt=Holer()))


class Regel(unittest.TestCase):
    def unveraendert(self, roh: dict, holer: Holer, ziel=ZIEL) -> None:
        self.assertEqual(transitous.forme_um(roh, ziel), transitous.forme_um(roh, ziel, hole_fahrt=holer))

    def test_naechster_guter_halt(self):
        holer = Holer({"fahrt-1": fahrt()})
        v = forme(plan(), holer)
        bus, fuss = v["teile"]
        self.assertEqual(["fahrt-1"], holer.gefragt)
        self.assertEqual(("spaeter_aussteigen", zeit("09:28"), 18), (v["angepasst"], v["an"], v["dauer_min"]))
        # Ein Zwischenhalt bisher, dazu der alte Ausstieg.
        self.assertEqual(("Am Ziel", zeit("09:27"), 2, 17), (bus["nach"]["name"], bus["nach"]["zeit"],
                                                             bus["zwischenhalte"], bus["dauer_min"]))
        self.assertEqual((65, 1), (fuss["meter"], fuss["dauer_min"]))

    def test_fussweg_bis_300_meter_fragt_nicht(self):
        # 230 m Luftlinie → 299 m: nicht fragen. 232 m → 302 m: fragen.
        for meter, gefragt in ((230, 0), (232, 1)):
            holer = Holer({"fahrt-1": fahrt()})
            transitous.forme_um(plan(aus=halt("Nah", meter, "09:25"), fuss_meter=2000), ZIEL, hole_fahrt=holer)
            self.assertEqual(gefragt, len(holer.gefragt), msg=meter)

    def test_ohne_tripid_ohne_ziel_oder_ohne_fussweg_fragt_nicht(self):
        holer = Holer({"fahrt-1": fahrt()})
        self.unveraendert(plan(trip_id=None), holer)
        self.unveraendert(plan(trip_id=""), holer)
        self.unveraendert(plan(cancelled=True), holer)
        transitous.forme_um(plan(), None, hole_fahrt=holer)
        roh = plan()
        del roh["itineraries"][0]["legs"][1]
        self.unveraendert(roh, holer)
        self.assertEqual([], holer.gefragt)

    def test_frueher_aussteigen_geht_vor(self):
        holer = Holer({"fahrt-1": fahrt()})
        roh = plan(intermediateStops=[halt("Davor", 20, "09:22")])
        self.assertEqual("frueher_aussteigen", forme(roh, holer)["angepasst"])
        self.assertEqual([], holer.gefragt)
        # Abgeschaltet wird dann später geprüft.
        self.assertEqual("spaeter_aussteigen", forme(roh, holer, frueher=False)["angepasst"])

    def test_fehler_und_unpassende_antworten(self):
        for holer in (Holer(), Holer({"fahrt-1": {}}), Holer({"fahrt-1": {"legs": "kaputt"}}),
                      Holer({"fahrt-1": {"legs": [{"mode": "WALK"}]}}), Holer({"fahrt-1": fahrt(trip_id="andere")}),
                      Holer(fehler=transitous.QuelleFehler("Transitous ist nicht erreichbar", 504)),
                      Holer(fehler=RuntimeError("unerwartet")),
                      Holer({"fahrt-1": ECHT})):
            self.unveraendert(plan(), holer)
            self.assertEqual(["fahrt-1"], holer.gefragt)

    def test_ausstieg_muss_in_der_fahrt_stehen(self):
        # Andere Zeit (etwa die Fahrt eine Runde später): kein Treffer.
        anders = fahrt()
        anders["legs"][0]["intermediateStops"][1] = {**AUSSTIEG, "scheduledArrival": zeit("09:26")}
        self.unveraendert(plan(), Holer({"fahrt-1": anders}))
        # Gleiche Zeit, aber eine andere Haltestelle: kein Treffer.
        anders["legs"][0]["intermediateStops"][1] = {**halt("Woanders", 2000, "09:25")}
        self.unveraendert(plan(), Holer({"fahrt-1": anders}))
        # Gleiche Zeit, anderer Name, aber dieselbe Stelle: Treffer.
        anders["legs"][0]["intermediateStops"][1] = {**AUSSTIEG, "name": "Zu früh (Bstg. 2)"}
        self.assertIn("angepasst", forme(plan(), Holer({"fahrt-1": anders})))
        # Mit stopId zählt nur sie.
        mit_id = fahrt()
        mit_id["legs"][0]["intermediateStops"][1] = {**AUSSTIEG, "stopId": "b"}
        self.unveraendert(plan(aus={**AUSSTIEG, "stopId": "a"}), Holer({"fahrt-1": mit_id}))
        mit_id["legs"][0]["intermediateStops"][1] = {**AUSSTIEG, "stopId": "a", "name": "Anders"}
        self.assertIn("angepasst", forme(plan(aus={**AUSSTIEG, "stopId": "a"}), Holer({"fahrt-1": mit_id})))
        # Der Ausstieg ist der letzte Halt der Fahrt: Danach kommt nichts.
        self.unveraendert(plan(), Holer({"fahrt-1": fahrt(danach=[])}))

    def test_abgesagt_ohne_ausstieg_und_hoechstens_drei_halte(self):
        ziel_halt = halt("Am Ziel", 50, "09:30")
        abgesagt = [{**DANACH[0], "cancelled": True}, {**DANACH[0], "name": "Kein Ausstieg", "dropoffType": "NOT_ALLOWED"},
                    ziel_halt]
        v = forme(plan(), Holer({"fahrt-1": fahrt(abgesagt)}))
        self.assertEqual(("Am Ziel", 4, zeit("09:31")), (v["teile"][0]["nach"]["name"], v["teile"][0]["zwischenhalte"],
                                                         v["an"]))
        # Der vierte Halt hinter dem Ausstieg ist zu weit, auch wenn er am Ziel liegt.
        fern = [halt("A", -800, "09:26"), halt("B", -1200, "09:27"), halt("C", -1600, "09:28"), ziel_halt]
        self.unveraendert(plan(), Holer({"fahrt-1": fahrt(fern)}))
        # Halte ohne Koordinaten oder Zeit zählen mit, taugen aber nicht.
        kaputt = ["kaputt", {k: w for k, w in DANACH[0].items() if k != "arrival"}, {**DANACH[0], "lat": None}]
        self.unveraendert(plan(), Holer({"fahrt-1": fahrt(kaputt + [ziel_halt])}))

    def test_lohnt_nur_frueher_oder_kuerzer(self):
        # Am Ziel 09:28 statt 09:29: nur eine Minute früher, aber 935 m weniger Fußweg → lohnt.
        self.assertIn("angepasst", forme(plan(am_ziel="09:29"), Holer({"fahrt-1": fahrt()})))
        # Später am Ziel als geplant: nie.
        self.unveraendert(plan(am_ziel="09:27"), Holer({"fahrt-1": fahrt()}))
        # Nicht später, aber nur 200 m kürzer und nur eine Minute früher → bleibt.
        # (330 m → 429 m; 160 m → 208 m → 3 Minuten, am Ziel 09:29 statt 09:30.)
        aus = halt("Zu früh", 330, "09:25")
        self.unveraendert(plan(aus=aus, am_ziel="09:30", fuss_meter=429),
                          Holer({"fahrt-1": fahrt([halt("Etwas näher", 160, "09:26")], aus=aus)}))
        self.assertIn("angepasst", forme(plan(aus=aus, am_ziel="09:31", fuss_meter=429),
                                         Holer({"fahrt-1": fahrt([halt("Etwas näher", 160, "09:26")], aus=aus)})))
        # Ein Halt vor dem geplanten Ausstieg in der Zeit: Die Antwort passt nicht.
        self.unveraendert(plan(), Holer({"fahrt-1": fahrt([halt("Am Ziel", 50, "09:20")])}))

    def test_frueheste_ankunft_gewinnt(self):
        # „Weiter“ (−400 m) ist 09:29 + 7 min = 09:36, „Am Ziel“ 09:28: Am Ziel gewinnt, auch wenn es zuerst kommt.
        v = forme(plan(), Holer({"fahrt-1": fahrt([DANACH[1], DANACH[0]])}))
        self.assertEqual(("Am Ziel", 3), (v["teile"][0]["nach"]["name"], v["teile"][0]["zwischenhalte"]))


class Weg(unittest.TestCase):
    def test_verlauf_folgt_der_fahrt(self):
        # Die Fahrt macht hinter dem alten Ausstieg einen Bogen nach Osten; der Verlauf soll ihm folgen.
        punkte = [(ort(m)["lat"], 8.0) for m in range(5000, 799, -500)] + [(ort(400)["lat"], 8.002),
                                                                          (ort(50)["lat"], 8.0), (ort(-1500)["lat"], 8.0)]
        geometrie = {"points": kodiere(punkte, 7), "precision": 7}
        roh = plan()
        bisher = transitous.forme_um(roh)[0]["teile"][0]["weg"]
        weg = forme(roh, Holer({"fahrt-1": fahrt(legGeometry=geometrie)}))["teile"][0]["weg"]
        self.assertEqual(bisher, weg[:len(bisher)])
        self.assertEqual([[round(ort(400)["lat"], 5), 8.002], [round(ort(50)["lat"], 5), 8.0]], weg[len(bisher):])

    def test_ohne_geometrie_die_gerade_linie(self):
        roh = plan()
        bisher = transitous.forme_um(roh)[0]["teile"][0]["weg"]
        for geometrie in (None, {"points": "kein weg", "precision": 7}, {"points": "??", "precision": 99}):
            weg = forme(roh, Holer({"fahrt-1": fahrt(legGeometry=geometrie)}))["teile"][0]["weg"]
            self.assertEqual(bisher + [[round(ort(50)["lat"], 5), 8.0]], weg, msg=str(geometrie))
        # Ohne bisherigen Verlauf fehlt er auch danach; ausgestiegen wird trotzdem später.
        roh["itineraries"][0]["legs"][0]["from"] = {"name": "Start"}
        v = forme(roh, Holer({"fahrt-1": fahrt()}))
        self.assertEqual("spaeter_aussteigen", v["angepasst"])
        self.assertNotIn("weg", v["teile"][0])

    def test_langer_weg_bleibt_bei_120_punkten(self):
        punkte = [(ort(5000 - m)["lat"], 8.0) for m in range(0, 6501, 5)]
        geometrie = {"points": kodiere(punkte, 7), "precision": 7}
        roh = plan(legGeometry={"points": kodiere(punkte[:841], 7), "precision": 7})
        weg = forme(roh, Holer({"fahrt-1": fahrt(legGeometry=geometrie)}))["teile"][0]["weg"]
        self.assertEqual(120, len(weg))
        self.assertEqual([round(ort(50)["lat"], 5), 8.0], weg[-1])
        self.assertGreaterEqual(min(p[0] for p in weg), round(ort(50)["lat"], 5))


# ---------------------------------------------------------------- Dienst: wie oft er Transitous fragt


def konfig(**mehr) -> Konfig:
    env = {"KONTAKT": "betreiber@example.org", "OHNE_SCHLUESSEL": "1", "OHNE_TAFEL": "1"}
    env.update(mehr)
    return Konfig.aus_umgebung(env)


def viele(trip_ids: list[str]) -> dict:
    """Ein Plan mit je einer Verbindung je tripId, alle mit langem Fußweg am Ende."""
    return {"itineraries": [plan(t)["itineraries"][0] for t in trip_ids]}


class Dienstabfragen(unittest.TestCase):
    WERTE = {"von": ["50.045,8.0"], "nach": ["50.0,8.0"], "anzahl": ["6"]}

    def setUp(self):
        self.zeit = 1000.0
        self.geholt: list[str] = []
        self.roh = viele(["fahrt-1"])
        self.fehler: dict[str, Exception] = {}

    def dienst(self, **mehr) -> Dienst:
        def hole(url, kontakt):
            self.geholt.append(url)
            if "/api/v1/trip?" in url:
                trip_id = url.split("tripId=", 1)[1]
                if trip_id in self.fehler:
                    raise self.fehler[trip_id]
                return fahrt(trip_id=trip_id)
            return self.roh

        return Dienst(konfig(**mehr), hole=hole, uhr=lambda: self.zeit)

    def fahrten(self) -> list[str]:
        return [u.split("tripId=", 1)[1] for u in self.geholt if "/api/v1/trip?" in u]

    def test_adresse_und_ergebnis(self):
        v = self.dienst().verbindungen(self.WERTE)
        self.assertEqual("https://api.transitous.org/api/v1/trip?tripId=fahrt-1", self.geholt[1])
        self.assertEqual(("spaeter_aussteigen", "Am Ziel"), (v[0]["angepasst"], v[0]["teile"][0]["nach"]["name"]))
        self.assertEqual("https://api.transitous.org/api/v1/trip?tripId=a%2Fb+c%26d",
                         transitous.fahrt_adresse("https://api.transitous.org", "a/b c&d"))

    def test_hoechstens_drei_fahrten_je_anfrage(self):
        self.roh = viele([f"f{i}" for i in range(6)])
        v = self.dienst().verbindungen(self.WERTE)
        self.assertEqual(["f0", "f1", "f2"], self.fahrten())
        self.assertEqual(["spaeter_aussteigen"] * 3 + [None] * 3, [x.get("angepasst") for x in v])

    def test_jede_fahrt_nur_einmal(self):
        self.roh = viele(["f1", "f1", "f2", "f1", "f2"])
        d = self.dienst()
        v = d.verbindungen(self.WERTE)
        self.assertEqual(["f1", "f2"], self.fahrten())
        self.assertEqual(["spaeter_aussteigen"] * 5, [x.get("angepasst") for x in v])
        # Eine andere Anfrage (andere Zeit) nutzt die gemerkten Fahrten mit; das Budget gilt nur für neue.
        self.roh = viele(["f1", "f2", "f3", "f4", "f5", "f6"])
        d.verbindungen({**self.WERTE, "zeit": ["2026-01-31T09:00:00Z"]})
        self.assertEqual(["f1", "f2", "f3", "f4", "f5"], self.fahrten())
        # Nach FAHRT_MERKEN_S wird wieder gefragt.
        self.zeit += FAHRT_MERKEN_S
        d.verbindungen({**self.WERTE, "zeit": ["2026-01-31T10:00:00Z"]})
        self.assertEqual(["f1", "f2", "f3", "f4", "f5", "f1", "f2", "f3"], self.fahrten())

    def test_nur_fuer_verbindungen_in_der_antwort(self):
        self.roh = viele(["f1", "f2", "f3"])
        v = self.dienst().verbindungen({**self.WERTE, "anzahl": ["1"]})
        self.assertEqual((1, ["f1"]), (len(v), self.fahrten()))

    def test_fehler_der_fahrt(self):
        self.roh = viele(["f1", "f2"])
        self.fehler["f1"] = transitous.QuelleFehler("Transitous bremst gerade (zu viele Abfragen)", 503)
        d = self.dienst()
        with self.assertLogs("fahrplan_dienst", "WARNING"):
            v = d.verbindungen(self.WERTE)
        self.assertEqual([None, "spaeter_aussteigen"], [x.get("angepasst") for x in v])
        self.assertEqual(transitous.forme_um(self.roh)[0], v[0])
        # Auch eine gescheiterte Fahrt wird nicht gleich wieder gefragt.
        d.verbindungen({**self.WERTE, "zeit": ["2026-01-31T09:00:00Z"]})
        self.assertEqual(["f1", "f2"], self.fahrten())

    def test_grenze_je_stunde_gilt_auch_fuer_fahrten(self):
        self.roh = viele(["f1", "f2", "f3"])
        d = self.dienst(MAX_JE_STUNDE="3")
        v = d.verbindungen(self.WERTE)
        # Eine Abfrage für den Plan, dann nur noch zwei Fahrten.
        self.assertEqual((["f1", "f2"], 3), (self.fahrten(), d.abfragen_letzte_stunde()))
        self.assertEqual(["spaeter_aussteigen", "spaeter_aussteigen", None], [x.get("angepasst") for x in v])
        with self.assertRaises(Abgelehnt):
            d.verbindungen({**self.WERTE, "zeit": ["2026-01-31T09:00:00Z"]})

    def test_schalter(self):
        self.assertTrue(konfig().spaeter_aussteigen)
        for wert in ("0", "false", "nein", " NO "):
            self.assertFalse(konfig(SPAETER_AUSSTEIGEN=wert).spaeter_aussteigen, msg=wert)
        for wert in ("1", "ja", ""):
            self.assertTrue(konfig(SPAETER_AUSSTEIGEN=wert).spaeter_aussteigen, msg=wert)
        with self.assertRaises(ValueError):
            konfig(SPAETER_AUSSTEIGEN="manchmal")
        v = self.dienst(SPAETER_AUSSTEIGEN="0").verbindungen(self.WERTE)
        self.assertEqual(([], None), (self.fahrten(), v[0].get("angepasst")))
        # Beide Schalter unabhängig: Ohne „früher“ wird trotzdem später ausgestiegen.
        self.assertTrue(konfig(FRUEHER_AUSSTEIGEN="0").spaeter_aussteigen)
        v = self.dienst(FRUEHER_AUSSTEIGEN="0").verbindungen(self.WERTE)
        self.assertEqual("spaeter_aussteigen", v[0]["angepasst"])


if __name__ == "__main__":
    unittest.main()
