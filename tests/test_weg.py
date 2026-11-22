"""Tests ohne Netz: Streckenverlauf ("weg") je Teilstück aus der kodierten Polylinie von Transitous."""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from fahrplan_dienst import transitous
from fahrplan_dienst.konfig import Konfig
from fahrplan_dienst.server import Dienst

# Gekürzte echte Antwort von Transitous: Friedberg (Hessen) Bahnhof → Frankfurt (Main) Hauptbahnhof.
ECHT = json.loads((Path(__file__).parent / "daten" / "plan_friedberg_frankfurt.json").read_text(encoding="utf-8"))

# Das Beispiel aus der Beschreibung des Verfahrens von Google, Genauigkeit 5.
BEISPIEL = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
BEISPIEL_PUNKTE = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]

AB, AN = "2026-01-31T09:10:00Z", "2026-01-31T09:20:00Z"


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


def teil(geometrie=None, von=None, nach=None) -> dict:
    """Ein Abschnitt mit der gegebenen Geometrie, umgeformt."""
    leg = {"mode": "BUS", "from": {"name": "A", "lat": 50.1, "lon": 8.1} if von is None else von,
           "to": {"name": "B", "lat": 50.2, "lon": 8.2} if nach is None else nach, "startTime": AB, "endTime": AN}
    if geometrie is not None:
        leg["legGeometry"] = geometrie
    roh = {"itineraries": [{"startTime": AB, "endTime": AN, "legs": [leg]}]}
    return transitous.forme_um(roh)[0]["teile"][0]


class Dekodieren(unittest.TestCase):
    def test_bekanntes_beispiel(self):
        self.assertEqual(BEISPIEL_PUNKTE, transitous.dekodiere(BEISPIEL, 5))

    def test_genauigkeit_zaehlt(self):
        # Derselbe Text mit sechs Stellen gelesen liegt um den Faktor zehn daneben.
        self.assertEqual((3.85, -12.02), transitous.dekodiere(BEISPIEL, 6)[0])
        punkte = [(50.3328637, 8.7609181), (50.1066805, 8.6628279)]
        self.assertEqual(punkte, transitous.dekodiere(kodiere(punkte, 7), 7))

    def test_leer_und_kaputt(self):
        self.assertEqual([], transitous.dekodiere("", 5))
        for text in (BEISPIEL[:-1], BEISPIEL[:5], "_p~iF ~ps|U", "äöü", "\x00", "~" * 30):
            with self.assertRaises(ValueError, msg=repr(text)):
                transitous.dekodiere(text, 5)


class Ausduennen(unittest.TestCase):
    def test_kurze_wege_bleiben(self):
        for anzahl in (0, 1, 2, 119, 120):
            punkte = [[i, i] for i in range(anzahl)]
            self.assertEqual(punkte, transitous.duenne_aus(punkte))

    def test_hoechstens_120_erster_und_letzter_bleiben(self):
        for anzahl in (121, 122, 239, 240, 577, 5000):
            punkte = [[i, i] for i in range(anzahl)]
            duenn = transitous.duenne_aus(punkte)
            self.assertEqual(120, len(duenn), msg=anzahl)
            self.assertEqual((punkte[0], punkte[-1]), (duenn[0], duenn[-1]))
            # Gleichmäßig und in der Reihenfolge des Weges, kein Punkt doppelt.
            schritte = [b[0] - a[0] for a, b in zip(duenn, duenn[1:])]
            self.assertGreater(min(schritte), 0)
            self.assertLessEqual(max(schritte) - min(schritte), 1)

    def test_langer_weg_im_teil(self):
        punkte = [(50.0 + i * 0.001, 8.0 + i * 0.001) for i in range(1000)]
        weg = teil({"points": kodiere(punkte, 7), "precision": 7, "length": 1000})["weg"]
        self.assertEqual(120, len(weg))
        self.assertEqual(([50.0, 8.0], [50.999, 8.999]), (weg[0], weg[-1]))


class Weg(unittest.TestCase):
    GERADE = [[50.1, 8.1], [50.2, 8.2]]

    def test_geometrie_wird_gelesen_und_gerundet(self):
        weg = teil({"points": kodiere([(50.1234567, 8.7654321), (50.1234999, 8.7654999)], 7), "precision": 7})["weg"]
        self.assertEqual([[50.12346, 8.76543], [50.1235, 8.7655]], weg)
        # Genauigkeit 5 und 6 liefern denselben Weg, wenn sie zur Polylinie passen.
        for genauigkeit in (5, 6):
            punkte = [(38.5, -120.2), (40.7, -120.95)]
            self.assertEqual([[38.5, -120.2], [40.7, -120.95]],
                             teil({"points": kodiere(punkte, genauigkeit), "precision": genauigkeit})["weg"])

    def test_doppelte_punkte_in_folge_fallen_weg(self):
        punkte = [(50.1, 8.1), (50.1, 8.1), (50.15, 8.15), (50.15, 8.15), (50.2, 8.2)]
        self.assertEqual([[50.1, 8.1], [50.15, 8.15], [50.2, 8.2]],
                         teil({"points": kodiere(punkte, 7), "precision": 7})["weg"])

    def test_fehlende_oder_kaputte_geometrie_ergibt_die_gerade_linie(self):
        gut = kodiere([(50.11, 8.11), (50.19, 8.19)], 7)
        for geometrie in (None, {}, "kaputt", [], 7, {"points": None, "precision": 7}, {"points": "", "precision": 7},
                          {"points": gut[:-1], "precision": 7}, {"points": "kein weg", "precision": 7},
                          {"points": gut}, {"points": gut, "precision": None}, {"points": gut, "precision": "7"},
                          {"points": gut, "precision": 7.0}, {"points": gut, "precision": True},
                          {"points": gut, "precision": 0}, {"points": gut, "precision": 400},
                          {"points": gut, "precision": -3}, {"points": 12345, "precision": 7},
                          {"points": "?" * (transitous.MAX_POLYLINIE + 2), "precision": 7},
                          # Ein einzelner Punkt ist kein Weg.
                          {"points": kodiere([(50.15, 8.15)], 7), "precision": 7}):
            self.assertEqual(self.GERADE, teil(geometrie)["weg"], msg=repr(geometrie)[:80])

    def test_ohne_geometrie_und_ohne_koordinaten_fehlt_das_feld(self):
        for von, nach in (({"name": "A"}, {"name": "B"}), ({"name": "A", "lat": 50.1, "lon": 8.1}, {"name": "B"}),
                          ({"name": "A", "lat": 91, "lon": 8.1}, {"name": "B", "lat": 50.2, "lon": 8.2}),
                          ({"name": "A", "lat": 50.1, "lon": math.nan}, {"name": "B", "lat": 50.2, "lon": 8.2}),
                          ({"name": "A", "lat": "50.1", "lon": 8.1}, {"name": "B", "lat": 50.2, "lon": 8.2}),
                          ({"name": "A", "lat": True, "lon": 8.1}, {"name": "B", "lat": 50.2, "lon": 8.2})):
            umgeformt = teil(None, von, nach)
            self.assertNotIn("weg", umgeformt, msg=str((von, nach)))
            self.assertEqual("BUS", umgeformt["art"])
        # Mit Geometrie braucht es die Koordinaten der Haltestellen nicht.
        gut = {"points": kodiere([(50.11, 8.11), (50.19, 8.19)], 7), "precision": 7}
        self.assertEqual([[50.11, 8.11], [50.19, 8.19]], teil(gut, {"name": "A"}, {"name": "B"})["weg"])

    def test_unsinnige_punkte_fallen_weg(self):
        punkte = [(50.1, 8.1), (95.0, 8.1), (50.15, 200.0), (-90.5, 8.1), (50.15, -180.5), (50.2, 8.2)]
        self.assertEqual(self.GERADE, teil({"points": kodiere(punkte, 5), "precision": 5})["weg"])
        # Bleibt nach dem Verwerfen kein Weg übrig, gilt die gerade Linie zwischen den Haltestellen.
        nur_unsinn = {"points": kodiere([(95.0, 8.1), (50.15, 200.0), (50.12, 8.12)], 5), "precision": 5}
        self.assertEqual(self.GERADE, teil(nur_unsinn)["weg"])
        # Die falsche Genauigkeit schiebt alles aus der Welt: 50° werden mit einer Stelle zu wenig zu 500°.
        self.assertEqual(self.GERADE, teil({"points": kodiere([(50.1, 8.1), (50.2, 8.2)], 7), "precision": 6})["weg"])
        for weg in (teil({"points": kodiere(punkte, 5), "precision": 5})["weg"], teil(nur_unsinn)["weg"]):
            for lat, lon in weg:
                self.assertTrue(-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(lat) and math.isfinite(lon))


class EchteAntwort(unittest.TestCase):
    def test_ganze_umformung(self):
        verbindungen = transitous.forme_um(ECHT)
        self.assertEqual(1, len(verbindungen))
        hin, zug, weiter = verbindungen[0]["teile"]
        self.assertEqual(("FUSS", "REGIONALZUG", "FUSS"), (hin["art"], zug["art"], weiter["art"]))
        self.assertEqual(("RB40", "Friedberg (Hessen) Bahnhof", "Frankfurt (Main) Hauptbahnhof", 6),
                         (zug["linie"], zug["von"]["name"], zug["nach"]["name"], zug["verspaetung_min"]))

        # Der Zug: 577 Punkte in der Quelle, 120 in der Antwort, vom Einstieg zum Ausstieg.
        self.assertEqual(577, ECHT["itineraries"][0]["legs"][1]["legGeometry"]["length"])
        self.assertEqual(120, len(zug["weg"]))
        self.assertEqual(([50.33288, 8.76099], [50.1067, 8.66281]), (zug["weg"][0], zug["weg"][-1]))
        for ende, halt in ((zug["weg"][0], zug["von"]), (zug["weg"][-1], zug["nach"])):
            self.assertAlmostEqual(halt["lat"], ende[0], delta=0.001)
            self.assertAlmostEqual(halt["lon"], ende[1], delta=0.001)

        # Die Fußwege folgen den Wegen (Transitous rechnet sie ohne Zutun genau), nicht der Luftlinie.
        self.assertEqual(20, len(hin["weg"]))
        self.assertEqual(([50.33198, 8.76061], [50.33286, 8.76091]), (hin["weg"][0], hin["weg"][-1]))
        self.assertEqual(9, len(weiter["weg"]))

        for stueck in (hin, zug, weiter):
            for punkt in stueck["weg"]:
                self.assertEqual(2, len(punkt))
                self.assertEqual(punkt, [round(punkt[0], 5), round(punkt[1], 5)])
                self.assertTrue(50.0 < punkt[0] < 50.4 and 8.6 < punkt[1] < 8.8, msg=str(punkt))
            # Kein Sprung: Benachbarte Punkte liegen höchstens wenige Kilometer auseinander.
            for a, b in zip(stueck["weg"], stueck["weg"][1:]):
                self.assertLess(abs(a[0] - b[0]) + abs(a[1] - b[1]), 0.05)

    def test_groesse_bleibt_klein(self):
        antwort = json.dumps({"verbindungen": transitous.forme_um(ECHT)}, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(antwort.encode("utf-8")), 6000)

    def test_zwischenspeicher_traegt_den_weg(self):
        geholt = []

        def hole(url, kontakt):
            geholt.append(url)
            return ECHT

        # Ohne Bahnhofstafeln: Dieser Test darf nie ins Netz.
        konfig = Konfig.aus_umgebung({"KONTAKT": "betreiber@example.org", "OHNE_SCHLUESSEL": "1", "OHNE_TAFEL": "1"})
        dienst = Dienst(konfig, hole=hole, uhr=lambda: 1000.0)
        werte = {"von": ["50.332,8.7607"], "nach": ["50.1071,8.6638"]}
        erste, zweite = dienst.verbindungen(werte), dienst.verbindungen(werte)
        self.assertEqual(1, len(geholt))
        self.assertEqual(120, len(zweite[0]["teile"][1]["weg"]))
        self.assertEqual(erste, zweite)


if __name__ == "__main__":
    unittest.main()
