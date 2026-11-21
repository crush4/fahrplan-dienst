"""Tests ohne Netz: Gleise aus den Bahnhofstafeln."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fahrplan_dienst import tafel

JETZT = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)  # 10:00 Ortszeit (Winter)

GEO = {"candidates": [
    {"distance": 0, "eva": 8000111, "hafas": "X", "name": "Nordstadt"},
    {"distance": 0.09, "ds100": "FFG", "eva": 8000111, "hafas": 0, "name": "Nordstadt"},
    {"distance": 1.7, "ds100": "FGS", "eva": 8002105, "hafas": 0, "name": "Nordstadt Süd"},
]}
TAFEL = {"departures": [
    {"train": "S S6", "trainNumber": "35635", "scheduledDeparture": "10:20", "platform": "5", "scheduledPlatform": "5"},
    {"train": "HLB RE98", "trainNumber": "24811", "scheduledDeparture": "10:17", "platform": "3",
     "scheduledPlatform": "2"},
    {"train": "ICE 1573", "trainNumber": "1573", "scheduledDeparture": "10:11", "platform": "2"},
    {"train": "S S6", "trainNumber": "35600", "scheduledDeparture": None, "platform": "5"},
    "kaputt",
]}


def teil(art: str, linie: str, plan: str, **halt) -> dict:
    von = {"name": "Nordstadt Bahnhof", "plan": plan, "zeit": plan, "lat": 50.332863, "lon": 8.760918}
    von.update(halt)
    return {"art": art, "linie": linie, "von": von, "nach": {"name": "Ziel"}}


class Tafeltest(unittest.TestCase):
    def setUp(self):
        self.gefragt: list[tuple[str, bytes | None]] = []
        self.zeit = 500.0
        self.geo = GEO
        self.tafel = TAFEL

    def tafeln(self) -> tafel.Tafeln:
        def hole(url, kontakt, daten=None):
            self.gefragt.append((url, daten))
            self.assertEqual("betreiber@example.org", kontakt)
            if url.endswith("/_geolocation"):
                return self.geo
            return self.tafel

        return tafel.Tafeln("https://tafel.example/", "betreiber@example.org", hole=hole, uhr=lambda: self.zeit,
                            jetzt=lambda: JETZT)

    def test_gleis_fuer_s_bahn_regionalzug_und_fernzug(self):
        v = [{"teile": [
            teil("S_BAHN", "S6", "2026-01-31T09:20:00Z"),
            teil("REGIONALZUG", "RE98", "2026-01-31T09:17:00Z"),
            teil("FERNZUG", "ICE 1573", "2026-01-31T09:11:00Z"),
        ]}]
        t = self.tafeln()
        t.ergaenze(v)
        s, re, ice = (x["von"] for x in v[0]["teile"])
        self.assertEqual(("5", "Bahnhofstafel"), (s["gleis"], s["gleis_quelle"]))
        self.assertNotIn("gleis_plan", s)
        # Gleiswechsel: aktuelles und geplantes Gleis.
        self.assertEqual(("3", "2"), (re["gleis"], re["gleis_plan"]))
        self.assertEqual("2", ice["gleis"])
        # Ein Bahnhof: eine Suche und eine Tafel, nicht je Teilstück.
        self.assertEqual(["https://tafel.example/_geolocation", "https://tafel.example/8000111.json?version=3&limit=80"],
                         [u for u, _ in self.gefragt])
        self.assertEqual(b"lat=50.332863&lon=8.760918", self.gefragt[0][1])
        self.assertEqual(2, t.abfragen_letzte_stunde())

    def test_kein_gleis_ohne_treffer(self):
        v = [{"teile": [
            teil("S_BAHN", "S6", "2026-01-31T09:21:00Z"),  # andere Minute
            teil("S_BAHN", "S5", "2026-01-31T09:20:00Z"),  # andere Linie
            teil("BUS", "30", "2026-01-31T09:20:00Z"),  # Busse stehen auf keiner Tafel
            teil("S_BAHN", "S6", "2026-01-31T09:20:00Z", gleis="7"),  # Transitous kennt das Gleis schon
            teil("S_BAHN", "S6", "2026-01-31T13:20:00Z"),  # weiter voraus, als die Tafel reicht
            teil("S_BAHN", "S6", "2026-01-31T08:20:00Z"),  # längst abgefahren
        ]}]
        self.tafeln().ergaenze(v)
        gleise = [x["von"].get("gleis") for x in v[0]["teile"]]
        self.assertEqual([None, None, None, "7", None, None], gleise)

    def test_kein_bahnhof_in_der_naehe_wird_gemerkt(self):
        self.geo = {"candidates": [{"distance": 0.9, "ds100": "FGS", "eva": 8002105, "hafas": 0}]}
        t = self.tafeln()
        for _ in range(2):
            v = [{"teile": [teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")]}]
            t.ergaenze(v)
            self.assertNotIn("gleis", v[0]["teile"][0]["von"])
        self.assertEqual(1, len(self.gefragt))

    def test_zweite_tafel_am_selben_bahnhof(self):
        # Halle und Tiefbahnhof haben getrennte Tafeln: Steht der Zug nicht auf der ersten, wird die zweite gefragt.
        self.geo = {"candidates": [{"distance": 0.05, "ds100": "FF", "eva": 8000105, "hafas": 0},
                                   {"distance": 0.1, "ds100": "FFT", "eva": 8098105, "hafas": 0},
                                   {"distance": 0.1, "ds100": "FFT", "eva": 8098105, "hafas": 0}]}
        tief = self.tafel

        def hole(url, kontakt, daten=None):
            self.gefragt.append((url, daten))
            if url.endswith("/_geolocation"):
                return self.geo
            return tief if "/8098105." in url else {"departures": []}

        v = [{"teile": [teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")]}]
        tafel.Tafeln("https://tafel.example", "betreiber@example.org", hole=hole, uhr=lambda: 1.0,
                     jetzt=lambda: JETZT).ergaenze(v)
        self.assertEqual("5", v[0]["teile"][0]["von"]["gleis"])
        self.assertEqual(3, len(self.gefragt))

    def test_tafel_wird_eine_minute_gemerkt(self):
        t = self.tafeln()
        for sprung in (0, 59, 2):
            self.zeit += sprung
            t.ergaenze([{"teile": [teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")]}])
        self.assertEqual(3, len(self.gefragt))  # Suche, Tafel, nach 61 s noch eine Tafel

    def test_fehler_der_quelle_heisst_nur_kein_gleis(self):
        def hole(url, kontakt, daten=None):
            raise OSError("nicht erreichbar")

        v = [{"teile": [teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")]}]
        tafel.Tafeln("https://tafel.example", "betreiber@example.org", hole=hole, uhr=lambda: 1.0,
                     jetzt=lambda: JETZT).ergaenze(v)
        self.assertNotIn("gleis", v[0]["teile"][0]["von"])
        self.tafel = ["unerwartet"]
        v = [{"teile": [teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")]}]
        self.tafeln().ergaenze(v)
        self.assertNotIn("gleis", v[0]["teile"][0]["von"])

    def test_hoechstens_sechs_bahnhoefe_je_abfrage(self):
        teile = []
        for i in range(8):
            t = teil("S_BAHN", "S6", "2026-01-31T09:20:00Z")
            t["von"]["lat"] += i * 0.01
            teile.append(t)
        self.tafeln().ergaenze([{"teile": teile}])
        self.assertEqual(6, sum(1 for u, _ in self.gefragt if u.endswith("/_geolocation")))

    def test_ortszeit_im_winter_und_sommer(self):
        self.assertEqual("10:20", tafel.ortszeit("2026-01-31T09:20:00Z").strftime("%H:%M"))
        self.assertEqual("11:20", tafel.ortszeit("2026-07-01T09:20:00Z").strftime("%H:%M"))
        # Umstellung am letzten Sonntag im März und Oktober um 01:00 UTC.
        self.assertEqual("01:59", tafel.ortszeit("2026-03-29T00:59:00Z").strftime("%H:%M"))
        self.assertEqual("03:00", tafel.ortszeit("2026-03-29T01:00:00Z").strftime("%H:%M"))
        self.assertEqual("02:59", tafel.ortszeit("2026-10-25T00:59:00Z").strftime("%H:%M"))
        self.assertEqual("02:00", tafel.ortszeit("2026-10-25T01:00:00Z").strftime("%H:%M"))
        self.assertEqual("10:20", tafel.ortszeit("2026-01-31T10:20:00+01:00").strftime("%H:%M"))
        self.assertIsNone(tafel.ortszeit("morgen"))
        self.assertIsNone(tafel.ortszeit("2026-01-31T10:20:00"))

    def test_zuordnung_von_linie_und_zug(self):
        e = {"train": "HLB RE98", "trainNumber": "24811", "scheduledDeparture": "10:17"}
        self.assertTrue(tafel.passt(e, "RE98", "10:17"))
        self.assertTrue(tafel.passt(e, "RE 98", "10:17"))
        self.assertFalse(tafel.passt(e, "RE98", "10:18"))
        self.assertFalse(tafel.passt(e, "RE9", "10:17"))
        self.assertFalse(tafel.passt(e, "", "10:17"))
        self.assertTrue(tafel.passt({"train": "ICE 1573", "trainNumber": "1573", "scheduledDeparture": "10:11"},
                                    "ICE 1573", "10:11"))
        self.assertTrue(tafel.passt({"train": "IC 2273", "trainNumber": "2273", "scheduledDeparture": "10:11"},
                                    "Intercity 2273", "10:11"))
        self.assertFalse(tafel.passt({"train": None, "scheduledDeparture": "10:11"}, "S6", "10:11"))


if __name__ == "__main__":
    unittest.main()
