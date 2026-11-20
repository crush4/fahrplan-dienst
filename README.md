# Fahrplan-Dienst

Ein kleiner Dienst, der Verbindungen im öffentlichen Verkehr abfragt und kurz und einheitlich zurückgibt:
Linie, Richtung, Abfahrt und Ankunft nach Plan und in Echtzeit, Umstiege und – wo die Quelle es kennt – das Gleis.

Die Daten kommen von [Transitous](https://transitous.org), einem freien, gemeinschaftlich betriebenen
Fahrplan-Dienst auf Basis von [MOTIS](https://github.com/motis-project/motis). Der Fahrplan-Dienst ist dafür gedacht,
dass eine einzelne Navi-App ihn fragt, statt selbst mit Transitous zu sprechen. Er hält die Regeln von Transitous
an einer Stelle ein: Kennung mit Kontakt, wenige Abfragen, Nennung der Quellen.

Nur Python-Standardbibliothek, ab Python 3.11. Kein Framework, keine Pakete.

## Was er liefert

`GET /verbindungen?von=50.3320,8.7607&nach=50.1071,8.6638` mit der Kopfzeile `X-Zugang: <Schlüssel>`

| Wert | Bedeutung |
|---|---|
| `von`, `nach` | Breite und Länge, mit Komma getrennt |
| `zeit` | optional, ISO 8601, zum Beispiel `2026-01-31T08:15:00Z`; ohne Angabe: jetzt |
| `ankunft=1` | optional; `zeit` ist dann die späteste Ankunft |
| `anzahl` | optional, 1 bis 6, Standard 4 |

Antwort (gekürzt):

```json
{
  "quelle": "Transitous",
  "quellen_url": "https://transitous.org/sources/",
  "verbindungen": [
    {
      "ab": "2026-01-31T09:10:00Z",
      "an": "2026-01-31T10:02:00Z",
      "dauer_min": 52,
      "umstiege": 1,
      "teile": [
        {"art": "FUSS", "meter": 280, "dauer_min": 4, "von": {"name": "START"}, "nach": {"name": "Bahnhof Nord"}},
        {
          "art": "REGIONALZUG",
          "linie": "RE30",
          "richtung": "Hauptstadt Hbf",
          "betreiber": "Beispielbahn",
          "echtzeit": true,
          "ausfall": false,
          "verspaetung_min": 3,
          "zwischenhalte": 2,
          "dauer_min": 25,
          "von": {"name": "Bahnhof Nord", "plan": "2026-01-31T09:14:00Z", "zeit": "2026-01-31T09:17:00Z", "gleis": "3", "gleis_plan": "2"},
          "nach": {"name": "Hauptstadt Hbf", "plan": "2026-01-31T09:40:00Z", "zeit": "2026-01-31T09:42:00Z", "gleis": "14"}
        }
      ]
    }
  ]
}
```

- Zeiten stehen in UTC. Die App rechnet sie in die Ortszeit um.
- `art` ist eines von `FUSS`, `FERNZUG`, `REGIONALZUG`, `S_BAHN`, `U_BAHN`, `TRAM`, `BUS`, `FERNBUS`, `FAEHRE`, `SONST`.
- `gleis` fehlt, wenn die Quelle keines nennt. Bei Fernzügen ist es meist da, im Regional- und Stadtverkehr oft nicht.

`GET /status` (ohne Schlüssel) nennt Version, Quelle und die Zahl der Abfragen der letzten Stunde.

Fehler kommen als `{"fehler": "<Text>"}`: 400 (Werte), 401 (Schlüssel), 429 (eigene Grenze je Stunde),
502/503/504 (Transitous antwortet nicht oder lehnt ab).

## Sparsam mit Transitous

Transitous wird von Freiwilligen betrieben. Der Dienst hält sich deshalb an die
[Regeln für die Nutzung](https://transitous.org/api/):

- Jede Abfrage nennt Name, Version und Kontakt des Betreibers (`User-Agent`).
- Höchstens 25 Abfragen je Stunde (einstellbar, nie mehr als 30). Das bleibt unter der Marke von 720 Abfragen
  in 24 Stunden, ab der Transitous um Absprache bittet.
- Gleiche Abfragen innerhalb einer Minute kommen aus dem Zwischenspeicher.
- Kein automatisches Abfragen: Der Dienst fragt nur, wenn die App fragt.
- Eine App, die die Verbindungen zeigt, nennt Transitous als Quelle und verlinkt `quellen_url`.

Wer den Dienst für mehr als den eigenen Gebrauch einsetzen will, spricht das bitte vorher mit Transitous ab.

## Starten

```sh
KONTAKT=du@example.org ZUGANG_SCHLUESSEL=<mindestens16Zeichen> python -m fahrplan_dienst
```

| Umgebung | Bedeutung |
|---|---|
| `KONTAKT` | Pflicht. Mail-Adresse oder Webseite des Betreibers. Geht in jeder Abfrage an Transitous. |
| `ZUGANG_SCHLUESSEL` | Pflicht. Mindestens 16 Buchstaben und Ziffern. Die App schickt ihn als `X-Zugang`. |
| `OHNE_SCHLUESSEL=1` | Nur für lokale Tests: ohne Zugangsschlüssel starten. |
| `PORT` | Standard 8080 |
| `MAX_JE_STUNDE` | Standard 25, höchstens 30 |
| `TRANSITOUS_URL` | Standard `https://api.transitous.org`; für eine eigene MOTIS-Instanz |

Mit Docker: `docker build -t fahrplan-dienst .`, dann mit denselben Werten als Umgebung starten.
Für CapRover liegt eine `captain-definition` bei.

Der Dienst schreibt nichts auf die Platte und führt kein Zugriffsprotokoll: Start und Ziel einer Abfrage stehen
nirgends.

## Tests

```sh
python -m unittest discover -s tests -t .
```

Die Tests brauchen kein Netz.

## Lizenz

[MIT](LICENSE). Die Fahrplandaten gehören ihren jeweiligen Quellen, siehe
[transitous.org/sources](https://transitous.org/sources/).
