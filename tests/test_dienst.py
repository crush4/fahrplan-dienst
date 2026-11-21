"""Tests ohne Netz: Umformen, Werteprüfung, Grenze je Stunde, Zwischenspeicher, HTTP."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from fahrplan_dienst import transitous
from fahrplan_dienst.konfig import Konfig
from fahrplan_dienst.server import Abgelehnt, Dienst, baue_server

SCHLUESSEL = "abcdefgh12345678"

ROH = {
    "itineraries": [
        {
            "startTime": "2026-01-31T09:10:00Z",
            "endTime": "2026-01-31T10:02:00Z",
            "duration": 3120,
            "legs": [
                {
                    "mode": "WALK",
                    "from": {"name": "START", "lat": 50.33, "lon": 8.76},
                    "to": {"name": "Bahnhof Nord"},
                    "startTime": "2026-01-31T09:10:00Z",
                    "endTime": "2026-01-31T09:14:00Z",
                    "distance": 280.4,
                },
                {
                    "mode": "REGIONAL_RAIL",
                    "displayName": "RE30",
                    "headsign": "Hauptstadt Hbf",
                    "agencyName": "Beispielbahn",
                    "realTime": True,
                    "cancelled": False,
                    "from": {"name": "Bahnhof Nord", "track": "3", "scheduledTrack": "2"},
                    "to": {"name": "Hauptstadt Hbf", "scheduledTrack": "14"},
                    "scheduledStartTime": "2026-01-31T09:14:00Z",
                    "startTime": "2026-01-31T09:17:00Z",
                    "scheduledEndTime": "2026-01-31T09:40:00Z",
                    "endTime": "2026-01-31T09:42:00Z",
                    "intermediateStops": [{}, {}],
                },
                {
                    "mode": "SUBWAY",
                    "routeShortName": "U4",
                    "headsign": "Ostend",
                    "from": {"name": "Hauptstadt Hbf"},
                    "to": {"name": "Markt"},
                    "startTime": "2026-01-31T09:50:00Z",
                    "endTime": "2026-01-31T10:02:00Z",
                },
            ],
        },
        {"startTime": "kaputt"},
        "auch kaputt",
    ]
}


def konfig(**mehr) -> Konfig:
    # Ohne Bahnhofstafeln: Diese Tests dürfen nie ins Netz.
    env = {"KONTAKT": "betreiber@example.org", "ZUGANG_SCHLUESSEL": SCHLUESSEL, "OHNE_TAFEL": "1"}
    env.update(mehr)
    return Konfig.aus_umgebung(env)


class Umformen(unittest.TestCase):
    def test_verbindung_mit_linie_richtung_gleis_und_zeiten(self):
        v = transitous.forme_um(ROH)
        self.assertEqual(1, len(v))
        self.assertEqual(("2026-01-31T09:10:00Z", "2026-01-31T10:02:00Z", 52, 1),
                         (v[0]["ab"], v[0]["an"], v[0]["dauer_min"], v[0]["umstiege"]))
        fuss, zug, ubahn = v[0]["teile"]
        self.assertEqual(("FUSS", 280, 4), (fuss["art"], fuss["meter"], fuss["dauer_min"]))
        self.assertNotIn("linie", fuss)
        self.assertEqual(("REGIONALZUG", "RE30", "Hauptstadt Hbf", "Beispielbahn"),
                         (zug["art"], zug["linie"], zug["richtung"], zug["betreiber"]))
        self.assertTrue(zug["echtzeit"])
        self.assertFalse(zug["ausfall"])
        self.assertEqual(3, zug["verspaetung_min"])
        self.assertEqual(2, zug["zwischenhalte"])
        self.assertEqual({"name": "Bahnhof Nord", "plan": "2026-01-31T09:14:00Z", "zeit": "2026-01-31T09:17:00Z",
                          "gleis": "3", "gleis_plan": "2"}, zug["von"])
        self.assertEqual("14", zug["nach"]["gleis"])
        self.assertNotIn("gleis_plan", zug["nach"])
        # Ohne Angabe der Quelle gibt es kein Gleis und keine erfundene Verspätung.
        self.assertEqual(("U_BAHN", "U4", "Ostend"), (ubahn["art"], ubahn["linie"], ubahn["richtung"]))
        self.assertNotIn("gleis", ubahn["von"])
        self.assertEqual(0, ubahn["verspaetung_min"])
        self.assertFalse(ubahn["echtzeit"])

    def test_leere_und_fremde_antworten(self):
        self.assertEqual([], transitous.forme_um({}))
        self.assertEqual([], transitous.forme_um({"itineraries": None}))
        self.assertEqual("SONST", transitous.forme_um({"itineraries": [{
            "startTime": "2026-01-31T09:10:00Z", "endTime": "2026-01-31T09:20:00Z",
            "legs": [{"mode": "ZEPPELIN", "from": {}, "to": {}, "startTime": "2026-01-31T09:10:00Z",
                      "endTime": "2026-01-31T09:20:00Z"}]}]})[0]["teile"][0]["art"])

    def test_adresse_und_kennung(self):
        url = transitous.adresse("https://quelle.example", (50.332, 8.7607), (50.1071, 8.6638), None, False, 4)
        self.assertEqual("https://quelle.example/api/v1/plan?fromPlace=50.332000%2C8.760700"
                         "&toPlace=50.107100%2C8.663800&numItineraries=4", url)
        url = transitous.adresse("https://quelle.example", (1, 2), (3, 4), "2026-01-31T08:15:00Z", True, 2)
        self.assertIn("time=2026-01-31T08%3A15%3A00Z", url)
        self.assertIn("arriveBy=true", url)
        self.assertRegex(transitous.nutzer_kennung("betreiber@example.org"),
                         r"^fahrplan-dienst/\d+\.\d+\.\d+ \(betreiber@example\.org\)$")


class Einstellungen(unittest.TestCase):
    def test_pflichtwerte(self):
        k = konfig()
        self.assertEqual((8080, "betreiber@example.org", "https://api.transitous.org", 25),
                         (k.port, k.kontakt, k.quelle, k.max_je_stunde))
        self.assertNotIn(SCHLUESSEL, repr(k))
        self.assertIsNone(k.tafel)
        ohne_schluessel = {"KONTAKT": "a@b.de", "OHNE_SCHLUESSEL": "1"}
        self.assertEqual("https://dbf.finalrewind.org", Konfig.aus_umgebung(ohne_schluessel).tafel)
        self.assertEqual("https://tafel.example",
                         Konfig.aus_umgebung({**ohne_schluessel, "TAFEL_URL": "https://tafel.example/"}).tafel)
        with self.assertRaises(ValueError):
            Konfig.aus_umgebung({**ohne_schluessel, "TAFEL_URL": "tafel"})
        for env in ({"ZUGANG_SCHLUESSEL": SCHLUESSEL}, {"KONTAKT": "kein kontakt", "ZUGANG_SCHLUESSEL": SCHLUESSEL},
                    {"KONTAKT": "a@b.de"}, {"KONTAKT": "a@b.de", "ZUGANG_SCHLUESSEL": "kurz"},
                    {"KONTAKT": "a@b.de", "OHNE_SCHLUESSEL": "1", "MAX_JE_STUNDE": "31"},
                    {"KONTAKT": "a@b.de", "OHNE_SCHLUESSEL": "1", "TRANSITOUS_URL": "ftp://x"},
                    {"KONTAKT": "a@b.de", "OHNE_SCHLUESSEL": "1", "PORT": "0"}):
            with self.assertRaises(ValueError, msg=str(env)):
                Konfig.aus_umgebung(env)
        self.assertEqual("https://example.org/kontakt",
                         Konfig.aus_umgebung({"KONTAKT": "https://example.org/kontakt", "OHNE_SCHLUESSEL": "1"}).kontakt)


class Abfragen(unittest.TestCase):
    def setUp(self):
        self.geholt: list[tuple[str, str]] = []
        self.zeit = 1000.0

    def dienst(self, **mehr) -> Dienst:
        def hole(url, kontakt):
            self.geholt.append((url, kontakt))
            return ROH

        return Dienst(konfig(**mehr), hole=hole, uhr=lambda: self.zeit)

    def test_werte_werden_geprueft(self):
        d = self.dienst()
        for werte in ({}, {"von": ["50,8"]}, {"von": ["50"], "nach": ["50,8"]}, {"von": ["91,8"], "nach": ["50,8"]},
                      {"von": ["nan,8"], "nach": ["50,8"]}, {"von": ["50,8,1"], "nach": ["50,8"]},
                      {"von": ["50,8"], "nach": ["50,9"], "zeit": ["morgen"]},
                      {"von": ["50,8"], "nach": ["50,9"], "anzahl": ["7"]},
                      {"von": ["50,8"], "nach": ["50,9"], "anzahl": ["x"]}):
            with self.assertRaises(Abgelehnt, msg=str(werte)) as fehler:
                d.verbindungen(werte)
            self.assertEqual(400, fehler.exception.status)
        self.assertEqual([], self.geholt)

    def test_gleiche_abfrage_kommt_eine_minute_aus_dem_speicher(self):
        d = self.dienst()
        werte = {"von": ["50.332,8.7607"], "nach": ["50.1071,8.6638"]}
        self.assertEqual(1, len(d.verbindungen(werte)))
        self.zeit += 59
        d.verbindungen(werte)
        self.assertEqual(1, len(self.geholt))
        self.assertEqual("betreiber@example.org", self.geholt[0][1])
        self.zeit += 2
        d.verbindungen(werte)
        self.assertEqual(2, len(self.geholt))
        self.assertEqual(2, d.abfragen_letzte_stunde())

    def test_grenze_je_stunde(self):
        d = self.dienst(MAX_JE_STUNDE="2")
        for i in range(2):
            d.verbindungen({"von": [f"50.{i},8"], "nach": ["50.9,8"]})
        with self.assertRaises(Abgelehnt) as fehler:
            d.verbindungen({"von": ["50.7,8"], "nach": ["50.9,8"]})
        self.assertEqual(429, fehler.exception.status)
        self.assertEqual(2, len(self.geholt))
        self.zeit += 3600
        d.verbindungen({"von": ["50.7,8"], "nach": ["50.9,8"]})
        self.assertEqual(3, len(self.geholt))

    def test_fehler_der_quelle_wird_weitergereicht(self):
        def hole(url, kontakt):
            raise transitous.QuelleFehler("Transitous ist nicht erreichbar", 504)

        d = Dienst(konfig(), hole=hole, uhr=lambda: self.zeit)
        with self.assertRaises(Abgelehnt) as fehler:
            d.verbindungen({"von": ["50,8"], "nach": ["50.9,8"]})
        self.assertEqual(504, fehler.exception.status)

    def test_zugang(self):
        d = self.dienst()
        self.assertTrue(d.zugang_ok(SCHLUESSEL))
        self.assertFalse(d.zugang_ok(None))
        self.assertFalse(d.zugang_ok(SCHLUESSEL + "x"))
        self.assertFalse(d.zugang_ok("schlüssel"))


class Http(unittest.TestCase):
    def setUp(self):
        self.server = baue_server(Dienst(konfig(), hole=lambda url, kontakt: ROH), "127.0.0.1", 0)
        self.basis = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def hole(self, pfad: str, schluessel: str | None = None) -> tuple[int, dict]:
        anfrage = urllib.request.Request(self.basis + pfad, headers={"X-Zugang": schluessel} if schluessel else {})
        try:
            with urllib.request.urlopen(anfrage, timeout=5) as antwort:
                return antwort.status, json.loads(antwort.read())
        except urllib.error.HTTPError as fehler:
            return fehler.code, json.loads(fehler.read())

    def test_status_ohne_schluessel(self):
        status, inhalt = self.hole("/status")
        self.assertEqual(200, status)
        self.assertEqual(("fahrplan-dienst", "Transitous", 0, 25),
                         (inhalt["dienst"], inhalt["quelle"], inhalt["abfragen_letzte_stunde"], inhalt["max_je_stunde"]))

    def test_verbindungen_nur_mit_schluessel(self):
        pfad = "/verbindungen?von=50.332,8.7607&nach=50.1071,8.6638"
        self.assertEqual(401, self.hole(pfad)[0])
        self.assertEqual(401, self.hole(pfad, "falsch")[0])
        status, inhalt = self.hole(pfad, SCHLUESSEL)
        self.assertEqual(200, status)
        self.assertEqual("https://transitous.org/sources/", inhalt["quellen_url"])
        self.assertEqual("RE30", inhalt["verbindungen"][0]["teile"][1]["linie"])
        self.assertEqual(400, self.hole("/verbindungen?von=50,8", SCHLUESSEL)[0])
        self.assertEqual(404, self.hole("/anderes")[0])


if __name__ == "__main__":
    unittest.main()
