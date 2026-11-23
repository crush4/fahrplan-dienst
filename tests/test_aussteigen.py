"""Tests ohne Netz: „früher aussteigen“ an einem Zwischenhalt der letzten Fahrt."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fahrplan_dienst import tafel, transitous
from fahrplan_dienst.konfig import Konfig
from fahrplan_dienst.server import Dienst

# Gekürzte echte Antwort von Transitous: Station Industriehof → Opernplatz in Frankfurt. Alle Fahrten enden an der
# Hauptwache, 659 m Fußweg vom Ziel; der Halt davor, Alte Oper, liegt 74 m vom Ziel.
ECHT = json.loads((Path(__file__).parent / "daten" / "plan_industriehof_opernplatz.json").read_text(encoding="utf-8"))
OPERNPLATZ = (50.1157, 8.6717)

# Erfundene Strecke: Alles liegt auf einem Längengrad, das Ziel bei 0 m, der Start 5 km nördlich.
ZIEL = (50.0, 8.0)
GRAD_JE_METER = 1 / 111194.93


def ort(meter: float) -> dict:
    return {"lat": 50.0 + meter * GRAD_JE_METER, "lon": 8.0}


def zeit(hhmm: str) -> str:
    return f"2026-01-31T{hhmm}:00Z"


def halt(name: str, meter: float, an: str, **mehr) -> dict:
    return {"name": name, **ort(meter), "arrival": zeit(an), "scheduledArrival": zeit(an), "departure": zeit(an),
            "cancelled": False, "dropoffType": "NORMAL", **mehr}


def antwort(zwischen: list, aus_an: str = "09:30", am_ziel: str = "09:39", fuss_meter: float | None = 650,
            art: str = "BUS", mit_fussweg: bool = True, **fahrt) -> dict:
    """Eine Verbindung: Fahrt vom Start bis 500 m hinter das Ziel, dann zu Fuß zurück."""
    legs = [{"mode": art, "displayName": "30", "headsign": "Endstation", "cancelled": False,
             "from": {"name": "Start", **ort(5000)}, "to": {"name": "Zu weit", **ort(-500)},
             "scheduledStartTime": zeit("09:10"), "startTime": zeit("09:10"),
             "scheduledEndTime": zeit(aus_an), "endTime": zeit(aus_an), "intermediateStops": zwischen, **fahrt}]
    if mit_fussweg:
        fussweg = {"mode": "WALK", "from": {"name": "Zu weit", **ort(-500)},
                   "to": {"name": "END", "lat": ZIEL[0], "lon": ZIEL[1]},
                   "startTime": zeit(aus_an), "endTime": zeit(am_ziel)}
        if fuss_meter is not None:
            fussweg["distance"] = fuss_meter
        legs.append(fussweg)
    ende = am_ziel if mit_fussweg else aus_an
    return {"itineraries": [{"startTime": zeit("09:10"), "endTime": zeit(ende), "legs": legs}]}


def forme(roh: dict, ziel=ZIEL) -> dict:
    return transitous.forme_um(roh, ziel)[0]


class EchterFall(unittest.TestCase):
    def test_ausstieg_alte_oper_statt_hauptwache(self):
        vorher = transitous.forme_um(ECHT)
        nachher = transitous.forme_um(ECHT, OPERNPLATZ)
        self.assertEqual(2, len(nachher))

        v = vorher[0]
        self.assertNotIn("angepasst", v)
        self.assertEqual(("2026-11-23T07:11:00Z", 18), (v["an"], v["dauer_min"]))
        self.assertEqual(("Frankfurt (Main) Hauptwache", 5), (v["teile"][1]["nach"]["name"], v["teile"][1]["zwischenhalte"]))
        self.assertEqual((659, 10), (v["teile"][2]["meter"], v["teile"][2]["dauer_min"]))

        n = nachher[0]
        self.assertEqual("frueher_aussteigen", n["angepasst"])
        self.assertEqual(("2026-11-23T06:53:00Z", "2026-11-23T07:02:00Z", 9, 0),
                         (n["ab"], n["an"], n["dauer_min"], n["umstiege"]))
        hin, bahn, fuss = n["teile"]
        self.assertEqual(vorher[0]["teile"][0], hin)
        self.assertEqual(("U_BAHN", "U6", 4, 6), (bahn["art"], bahn["linie"], bahn["zwischenhalte"], bahn["dauer_min"]))
        self.assertEqual({"name": "Frankfurt (Main) Alte Oper", "plan": "2026-11-23T07:00:00Z",
                          "zeit": "2026-11-23T07:00:00Z", "lat": 50.115314, "lon": 8.670851}, bahn["nach"])
        self.assertEqual(v["teile"][1]["von"], bahn["von"])
        # 74 m Luftlinie, mit Umweg 96 m, das sind aufgerundet zwei Minuten.
        self.assertEqual(("FUSS", 96, 2), (fuss["art"], fuss["meter"], fuss["dauer_min"]))
        self.assertEqual({"name": "Frankfurt (Main) Alte Oper", "plan": "2026-11-23T07:00:00Z",
                          "zeit": "2026-11-23T07:00:00Z", "lat": 50.115314, "lon": 8.670851}, fuss["von"])
        self.assertEqual(("END", "2026-11-23T07:02:00Z", "2026-11-23T07:02:00Z"),
                         (fuss["nach"]["name"], fuss["nach"]["plan"], fuss["nach"]["zeit"]))
        self.assertEqual([[50.11531, 8.67085], [50.1157, 8.6717]], fuss["weg"])

        # Der Verlauf der Fahrt endet am neuen Ausstieg und reicht nicht mehr bis zur Hauptwache.
        alt, neu = v["teile"][1]["weg"], bahn["weg"]
        self.assertEqual(alt[0], neu[0])
        self.assertEqual([50.11531, 8.67085], neu[-1])
        self.assertLess(len(neu), len(alt))
        self.assertLess(max(p[1] for p in neu), 8.6710)
        self.assertGreater(max(p[1] for p in alt), 8.6780)

        # Auch die zweite Verbindung (sieben Minuten später) steigt an der Alten Oper aus.
        self.assertEqual(("frueher_aussteigen", "2026-11-23T07:09:00Z", "Frankfurt (Main) Alte Oper"),
                         (nachher[1]["angepasst"], nachher[1]["an"], nachher[1]["teile"][1]["nach"]["name"]))

    def test_antwort_wird_nicht_groesser(self):
        def groesse(v):
            return len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        self.assertLess(groesse(transitous.forme_um(ECHT, OPERNPLATZ)), groesse(transitous.forme_um(ECHT)))


class Regel(unittest.TestCase):
    def unveraendert(self, roh: dict, ziel=ZIEL) -> None:
        self.assertEqual(transitous.forme_um(roh), transitous.forme_um(roh, ziel))
        self.assertNotIn("angepasst", forme(roh, ziel))

    def test_kein_zwischenhalt_naeher(self):
        self.unveraendert(antwort([halt("Fern", 3000, "09:25"), halt("Auch fern", 1500, "09:27")]))
        self.unveraendert(antwort([]))
        self.unveraendert(antwort(None))
        self.unveraendert(antwort("kaputt"))

    def test_naeher_aber_ankunft_spaeter(self):
        # 300 m vor dem Ziel statt 500 m dahinter, aber Transitous kennt hinten einen schnellen Weg (3 Minuten).
        roh = antwort([halt("Davor", 300, "09:29")], am_ziel="09:33", fuss_meter=200)
        self.unveraendert(roh)

    def test_zwei_minuten_frueher_reichen(self):
        # 200 m Luftlinie → 260 m → 4 Minuten; Ankunft 09:33 statt 09:35.
        v = forme(antwort([halt("Davor", 200, "09:29")], am_ziel="09:35", fuss_meter=300))
        self.assertEqual(("frueher_aussteigen", zeit("09:33"), 23), (v["angepasst"], v["an"], v["dauer_min"]))
        # Nur eine Minute früher und kaum weniger Fußweg: bleibt.
        self.unveraendert(antwort([halt("Davor", 200, "09:29")], am_ziel="09:34", fuss_meter=300))

    def test_nicht_spaeter_und_300_meter_kuerzer(self):
        zwischen = [halt("Davor", 200, "09:30")]
        # Ankunft 09:34 statt 09:35, 260 m statt 650 m.
        v = forme(antwort(zwischen, aus_an="09:31", am_ziel="09:35", fuss_meter=650))
        self.assertEqual(("frueher_aussteigen", zeit("09:34"), 260), (v["angepasst"], v["an"], v["teile"][-1]["meter"]))
        # Gleiche Ankunft zählt auch als „nicht später“.
        self.assertEqual(zeit("09:34"), forme(antwort(zwischen, aus_an="09:31", am_ziel="09:34"))["an"])
        # 299 m kürzer genügen nicht, 300 m schon.
        self.unveraendert(antwort(zwischen, aus_an="09:31", am_ziel="09:35", fuss_meter=559))
        self.assertIn("angepasst", forme(antwort(zwischen, aus_an="09:31", am_ziel="09:35", fuss_meter=560)))
        # Nennt Transitous keine Länge, gilt die Schätzung vom alten Ausstieg aus: 500 m × 1,3 = 650 m.
        self.assertIn("angepasst", forme(antwort(zwischen, aus_an="09:31", am_ziel="09:35", fuss_meter=None)))
        # Später ankommen ist nie erlaubt, auch nicht für einen viel kürzeren Weg.
        self.unveraendert(antwort(zwischen, aus_an="09:31", am_ziel="09:33", fuss_meter=2000))

    def test_frueheste_ankunft_gewinnt_dann_der_kuerzere_weg(self):
        # A: 100 m → 130 m → 2 Minuten, Ankunft 09:30. B: 20 m → 26 m → 1 Minute, Ankunft 09:30. Gleichstand → B.
        v = forme(antwort([halt("A", 100, "09:28"), halt("B", 20, "09:29")]))
        self.assertEqual(("B", zeit("09:30"), 26), (v["teile"][0]["nach"]["name"], v["an"], v["teile"][1]["meter"]))
        # Mit einem langsameren B (Ankunft 09:31) gewinnt A.
        v = forme(antwort([halt("A", 100, "09:28"), halt("B", 20, "09:30")], aus_an="09:31"))
        self.assertEqual(("A", zeit("09:30"), 0), (v["teile"][0]["nach"]["name"], v["an"], v["teile"][0]["zwischenhalte"]))

    def test_gehzeit(self):
        self.assertEqual([1, 1, 1, 2, 2, 3, 14], [transitous.gehzeit_min(m) for m in (0, 1, 75, 76, 150, 151, 1000)])
        self.assertAlmostEqual(74, transitous.luftlinie_m((50.115314, 8.670851), OPERNPLATZ), delta=1)
        self.assertAlmostEqual(1000, transitous.luftlinie_m((50.0, 8.0), (50.0 + 1000 * GRAD_JE_METER, 8.0)), delta=0.01)

    def test_fehlende_koordinaten_oder_zeiten(self):
        gut = halt("Davor", 20, "09:29")
        for kaputt in ({k: v for k, v in gut.items() if k != "lat"}, {k: v for k, v in gut.items() if k != "lon"},
                       {k: v for k, v in gut.items() if k != "arrival"}, {**gut, "arrival": "kaputt"},
                       {**gut, "arrival": None}, {**gut, "arrival": "2026-01-31T09:29:00"}, {**gut, "lat": "50.0"},
                       {**gut, "lat": 95.0}, {**gut, "lon": float("nan")}, {**gut, "lat": None}, "kaputt", None, 7):
            self.unveraendert(antwort([kaputt]))
        # Ein kaputter Halt verdirbt die anderen nicht; er zählt als Zwischenhalt aber mit.
        v = forme(antwort(["kaputt", gut]))
        self.assertEqual(("Davor", 1), (v["teile"][0]["nach"]["name"], v["teile"][0]["zwischenhalte"]))
        # Ohne geplante Ankunft gilt die tatsächliche.
        v = forme(antwort([{k: v for k, v in gut.items() if k != "scheduledArrival"}]))
        self.assertEqual((zeit("09:29"), zeit("09:29")), (v["teile"][0]["nach"]["plan"], v["teile"][0]["nach"]["zeit"]))
        # Unbrauchbares Ziel: nichts tun, nichts werfen.
        for ziel in ((95.0, 8.0), (float("nan"), 8.0), ("50", "8")):
            self.unveraendert(antwort([gut]), ziel)

    def test_ausgefallene_fahrt_und_halt_ohne_ausstieg(self):
        gut = halt("Davor", 20, "09:29")
        self.unveraendert(antwort([gut], cancelled=True))
        self.unveraendert(antwort([{**gut, "cancelled": True}]))
        self.unveraendert(antwort([{**gut, "dropoffType": "NOT_ALLOWED"}]))

    def test_ohne_fussweg_am_ende(self):
        gut = halt("Davor", 20, "09:29")
        self.unveraendert(antwort([gut], mit_fussweg=False))
        # Zwei Fußwege am Ende oder nur ein Fußweg: Es gibt keine „letzte Fahrt vor dem Fußweg“.
        roh = antwort([gut])
        roh["itineraries"][0]["legs"].append(copy.deepcopy(roh["itineraries"][0]["legs"][1]))
        self.unveraendert(roh)
        roh = antwort([gut])
        del roh["itineraries"][0]["legs"][0]
        self.unveraendert(roh)

    def test_nur_die_letzte_fahrt_wird_geprueft(self):
        roh = antwort([halt("Davor", 20, "09:29")])
        legs = roh["itineraries"][0]["legs"]
        # Eine zweite Fahrt ohne Zwischenhalte hinter der ersten: Die erste bleibt unberührt.
        legs.insert(1, {"mode": "TRAM", "displayName": "11", "from": {"name": "Zu weit", **ort(-500)},
                        "to": {"name": "Zu weit", **ort(-500)}, "startTime": zeit("09:30"), "endTime": zeit("09:30")})
        self.unveraendert(roh)


class Umbau(unittest.TestCase):
    def test_zeiten_gleis_und_verspaetung_des_neuen_ausstiegs(self):
        davor = halt("Davor", 20, "09:29", scheduledArrival=zeit("09:27"), track="2", scheduledTrack="1")
        v = forme(antwort([halt("Fern", 3000, "09:20"), davor], art="SUBURBAN", realTime=True))
        bahn, fuss = v["teile"]
        self.assertEqual({"name": "Davor", "plan": zeit("09:27"), "zeit": zeit("09:29"), "gleis": "2", "gleis_plan": "1",
                          "lat": round(ort(20)["lat"], 6), "lon": 8.0}, bahn["nach"])
        self.assertEqual(("S_BAHN", "30", "Endstation", True, False, 0, 1, 19),
                         (bahn["art"], bahn["linie"], bahn["richtung"], bahn["echtzeit"], bahn["ausfall"],
                          bahn["verspaetung_min"], bahn["zwischenhalte"], bahn["dauer_min"]))
        # Der Fußweg beginnt am Halt, trägt aber kein Gleis.
        self.assertEqual({"name": "Davor", "plan": zeit("09:29"), "zeit": zeit("09:29"),
                          "lat": round(ort(20)["lat"], 6), "lon": 8.0}, fuss["von"])
        self.assertEqual({"name": "END", "plan": zeit("09:30"), "zeit": zeit("09:30"), "lat": 50.0, "lon": 8.0},
                         fuss["nach"])
        self.assertEqual((1, 26, [[round(ort(20)["lat"], 5), 8.0], [50.0, 8.0]]),
                         (fuss["dauer_min"], fuss["meter"], fuss["weg"]))
        self.assertEqual((zeit("09:10"), zeit("09:30"), 20, 0), (v["ab"], v["an"], v["dauer_min"], v["umstiege"]))

    def test_weg_endet_am_halt(self):
        # Punkte alle 500 m von 5000 m bis −500 m; der Halt liegt bei 20 m, am nächsten ist der Punkt bei 0 m.
        punkte = [(ort(m)["lat"], 8.0) for m in range(5000, -501, -500)]
        geometrie = {"points": kodiere(punkte, 7), "precision": 7, "length": len(punkte)}
        roh = antwort([halt("Davor", 20, "09:29")], legGeometry=geometrie)
        self.assertEqual(12, len(transitous.forme_um(roh)[0]["teile"][0]["weg"]))
        weg = forme(roh)["teile"][0]["weg"]
        self.assertEqual(12, len(weg))
        self.assertEqual([[round(ort(5000)["lat"], 5), 8.0], [50.0, 8.0], [round(ort(20)["lat"], 5), 8.0]],
                         [weg[0], weg[-2], weg[-1]])
        # Liegt der Halt genau auf einem Punkt des Verlaufs, steht er nicht doppelt.
        weg = forme(antwort([halt("Davor", 500, "09:25")], legGeometry=geometrie))["teile"][0]["weg"]
        self.assertEqual(10, len(weg))
        self.assertEqual([round(ort(500)["lat"], 5), 8.0], weg[-1])
        self.assertNotEqual(weg[-1], weg[-2])

    def test_ohne_geometrie_die_gerade_linie(self):
        erwartet = [[round(ort(5000)["lat"], 5), 8.0], [round(ort(20)["lat"], 5), 8.0]]
        for geometrie in (None, {"points": "kein weg", "precision": 7}, {"points": "", "precision": 7}):
            roh = antwort([halt("Davor", 20, "09:29")], legGeometry=geometrie)
            self.assertEqual(erwartet, forme(roh)["teile"][0]["weg"], msg=str(geometrie))
        # Fehlen auch die Koordinaten des Einstiegs, fehlt der Verlauf der Fahrt; ausgestiegen wird trotzdem früher.
        roh = antwort([halt("Davor", 20, "09:29")])
        roh["itineraries"][0]["legs"][0]["from"] = {"name": "Start"}
        v = forme(roh)
        self.assertEqual("frueher_aussteigen", v["angepasst"])
        self.assertNotIn("weg", v["teile"][0])
        self.assertEqual(2, len(v["teile"][1]["weg"]))

    def test_langer_weg_bleibt_bei_120_punkten(self):
        punkte = [(ort(5000 - m)["lat"], 8.0) for m in range(0, 5501, 5)]
        roh = antwort([halt("Davor", 20, "09:29")], legGeometry={"points": kodiere(punkte, 7), "precision": 7})
        weg = forme(roh)["teile"][0]["weg"]
        self.assertEqual(120, len(weg))
        self.assertEqual(([round(ort(5000)["lat"], 5), 8.0], [round(ort(20)["lat"], 5), 8.0]), (weg[0], weg[-1]))
        self.assertGreaterEqual(min(p[0] for p in weg), round(ort(20)["lat"], 5))


def kodiere(punkte: list[tuple[float, float]], genauigkeit: int) -> str:
    """Gegenstück zu transitous.dekodiere, nur für die Tests."""
    text, vorher = [], (0, 0)
    for punkt in punkte:
        ganz = (round(punkt[0] * 10 ** genauigkeit), round(punkt[1] * 10 ** genauigkeit))
        for wert in (ganz[0] - vorher[0], ganz[1] - vorher[1]):
            wert = ~(wert << 1) if wert < 0 else wert << 1
            while wert >= 0x20:
                text.append(chr((0x20 | (wert & 0x1F)) + 63))
                wert >>= 5
            text.append(chr(wert + 63))
        vorher = ganz
    return "".join(text)


def konfig(**mehr) -> Konfig:
    # Ohne Bahnhofstafeln: Diese Tests dürfen nie ins Netz.
    env = {"KONTAKT": "betreiber@example.org", "OHNE_SCHLUESSEL": "1", "OHNE_TAFEL": "1"}
    env.update(mehr)
    return Konfig.aus_umgebung(env)


class Schalter(unittest.TestCase):
    WERTE = {"von": ["50.128616,8.629713"], "nach": ["50.1157,8.6717"]}

    def test_standard_an(self):
        self.assertTrue(konfig().frueher_aussteigen)
        v = Dienst(konfig(), hole=lambda url, kontakt: ECHT, uhr=lambda: 1.0).verbindungen(self.WERTE)
        self.assertEqual(["frueher_aussteigen"] * 2, [x.get("angepasst") for x in v])
        self.assertEqual("Frankfurt (Main) Alte Oper", v[0]["teile"][1]["nach"]["name"])

    def test_aus(self):
        for wert in ("0", "false", "nein", "NO", " 0 "):
            self.assertFalse(konfig(FRUEHER_AUSSTEIGEN=wert).frueher_aussteigen, msg=wert)
        for wert in ("1", "true", "Ja", ""):
            self.assertTrue(konfig(FRUEHER_AUSSTEIGEN=wert).frueher_aussteigen, msg=wert)
        with self.assertRaises(ValueError):
            konfig(FRUEHER_AUSSTEIGEN="vielleicht")
        v = Dienst(konfig(FRUEHER_AUSSTEIGEN="0"), hole=lambda url, kontakt: ECHT, uhr=lambda: 1.0).verbindungen(self.WERTE)
        self.assertEqual(transitous.forme_um(ECHT), v)
        self.assertEqual("Frankfurt (Main) Hauptwache", v[0]["teile"][1]["nach"]["name"])

    def test_die_abfrage_bei_transitous_bleibt_gleich(self):
        adressen = []
        for wert in ("1", "0"):
            Dienst(konfig(FRUEHER_AUSSTEIGEN=wert), hole=lambda url, kontakt: adressen.append(url) or ECHT,
                   uhr=lambda: 1.0).verbindungen(self.WERTE)
        self.assertEqual(adressen[0], adressen[1])
        self.assertNotIn("ntermediate", adressen[0])


class MitTafel(unittest.TestCase):
    """Das Gleis der Abfahrt kommt weiter von der Bahnhofstafel; der neue Ausstieg wird dort nicht nachgeschlagen."""

    JETZT = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)  # 10:00 Ortszeit (Winter)

    def dienst(self, gefragt: list) -> Dienst:
        def hole_tafel(url, kontakt, daten=None):
            gefragt.append((url, daten))
            if url.endswith("/_geolocation"):
                return {"candidates": [{"distance": 0.05, "ds100": "XST", "eva": 8000111, "hafas": 0}]}
            return {"departures": [{"train": "S S6", "trainNumber": "35635", "scheduledDeparture": "10:10",
                                    "platform": "5", "scheduledPlatform": "5"}]}

        tafeln = tafel.Tafeln("https://tafel.example", "betreiber@example.org", hole=hole_tafel, uhr=lambda: 1.0,
                              jetzt=lambda: self.JETZT)
        self.roh = antwort([halt("Davor", 20, "09:29")], art="SUBURBAN", displayName="S6")
        return Dienst(konfig(), hole=lambda url, kontakt: self.roh, uhr=lambda: 1.0, tafeln=tafeln)

    def test_gleis_der_abfahrt_bleibt_und_ausstieg_wird_uebersprungen(self):
        gefragt: list = []
        v = self.dienst(gefragt).verbindungen({"von": ["50.045,8.0"], "nach": ["50.0,8.0"]})[0]
        bahn, fuss = v["teile"]
        self.assertEqual("frueher_aussteigen", v["angepasst"])
        self.assertEqual(("5", "Bahnhofstafel"), (bahn["von"]["gleis"], bahn["von"]["gleis_quelle"]))
        self.assertEqual("Davor", bahn["nach"]["name"])
        for ende in (bahn["nach"], fuss["von"], fuss["nach"]):
            self.assertNotIn("gleis", ende)
            self.assertNotIn("gleis_quelle", ende)
        # Gefragt wurde nur der Einstiegsbahnhof: eine Suche, eine Tafel.
        self.assertEqual(["https://tafel.example/_geolocation", "https://tafel.example/8000111.json?version=3&limit=80"],
                         [u for u, _ in gefragt])
        self.assertIn(f"lat={ort(5000)['lat']:.6f}".encode("ascii"), gefragt[0][1])

    def test_gleis_des_halts_aus_transitous_bleibt(self):
        gefragt: list = []
        dienst = self.dienst(gefragt)
        self.roh = antwort([halt("Davor", 20, "09:29", track="7")], art="SUBURBAN", displayName="S6")
        bahn = dienst.verbindungen({"von": ["50.045,8.0"], "nach": ["50.0,8.0"]})[0]["teile"][0]
        self.assertEqual("7", bahn["nach"]["gleis"])
        self.assertNotIn("gleis_quelle", bahn["nach"])
        self.assertEqual("5", bahn["von"]["gleis"])

    def test_ausfall_der_tafel_stoert_nicht(self):
        def kaputt(url, kontakt, daten=None):
            raise OSError("kein Netz")

        tafeln = tafel.Tafeln("https://tafel.example", "betreiber@example.org", hole=kaputt, uhr=lambda: 1.0,
                              jetzt=lambda: self.JETZT)
        roh = antwort([halt("Davor", 20, "09:29")], art="SUBURBAN", displayName="S6")
        v = Dienst(konfig(), hole=lambda url, kontakt: roh, uhr=lambda: 1.0, tafeln=tafeln).verbindungen(
            {"von": ["50.045,8.0"], "nach": ["50.0,8.0"]})[0]
        self.assertEqual(("frueher_aussteigen", "Davor"), (v["angepasst"], v["teile"][0]["nach"]["name"]))
        self.assertNotIn("gleis", v["teile"][0]["von"])


if __name__ == "__main__":
    unittest.main()
