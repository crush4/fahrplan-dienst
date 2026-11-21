"""Konfiguration aus der Umgebung. Nichts davon steht im Repo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

MIN_SCHLUESSEL = 16
STANDARD_QUELLE = "https://api.transitous.org"
STANDARD_TAFEL = "https://dbf.finalrewind.org"
# Transitous nennt mehr als 720 Abfragen in 24 Stunden „hohe Last“. 25 je Stunde bleiben sicher darunter.
STANDARD_MAX_JE_STUNDE = 25
_JA = {"1", "true", "ja", "yes"}


@dataclass(frozen=True)
class Konfig:
    port: int
    # Wer den Dienst betreibt: Mail-Adresse oder Webseite. Transitous verlangt sie in jeder Abfrage.
    kontakt: str
    quelle: str = STANDARD_QUELLE
    max_je_stunde: int = STANDARD_MAX_JE_STUNDE
    # Adresse der Bahnhofstafeln (DBF) für das Gleis; None schaltet das Nachschlagen ab.
    tafel: str | None = STANDARD_TAFEL
    # Abfragen nur mit Kopfzeile X-Zugang. Nie im Log und nie in repr().
    zugang_schluessel: str | None = field(default=None, repr=False)

    @staticmethod
    def aus_umgebung(env: Mapping[str, str]) -> "Konfig":
        """Liest PORT, KONTAKT, ZUGANG_SCHLUESSEL, TRANSITOUS_URL, MAX_JE_STUNDE, TAFEL_URL und OHNE_TAFEL.
        OHNE_SCHLUESSEL=1 erlaubt lokale Tests ohne Zugangsschlüssel."""
        try:
            port = int(env.get("PORT") or "8080")
        except ValueError:
            raise ValueError("PORT ist keine Zahl") from None
        if not 1 <= port <= 65535:
            raise ValueError("PORT liegt außerhalb von 1 bis 65535")

        kontakt = (env.get("KONTAKT") or "").strip()
        if not _kontakt_gueltig(kontakt):
            raise ValueError("KONTAKT fehlt: Mail-Adresse oder Webseite des Betreibers (geht an Transitous)")

        quelle = (env.get("TRANSITOUS_URL") or STANDARD_QUELLE).strip().rstrip("/")
        teile = urlsplit(quelle)
        if teile.scheme not in ("http", "https") or not teile.netloc:
            raise ValueError("TRANSITOUS_URL ist keine gültige Adresse")

        try:
            max_je_stunde = int(env.get("MAX_JE_STUNDE") or STANDARD_MAX_JE_STUNDE)
        except ValueError:
            raise ValueError("MAX_JE_STUNDE ist keine Zahl") from None
        if not 1 <= max_je_stunde <= 30:
            raise ValueError("MAX_JE_STUNDE liegt außerhalb von 1 bis 30")

        tafel: str | None = None
        if (env.get("OHNE_TAFEL") or "").strip().lower() not in _JA:
            tafel = (env.get("TAFEL_URL") or STANDARD_TAFEL).strip().rstrip("/")
            teile = urlsplit(tafel)
            if teile.scheme not in ("http", "https") or not teile.netloc:
                raise ValueError("TAFEL_URL ist keine gültige Adresse")

        schluessel = env.get("ZUGANG_SCHLUESSEL") or None
        ohne = (env.get("OHNE_SCHLUESSEL") or "").strip().lower() in _JA
        if schluessel is None:
            if not ohne:
                raise ValueError("ZUGANG_SCHLUESSEL fehlt; nur für lokale Tests OHNE_SCHLUESSEL=1 setzen")
        elif len(schluessel) < MIN_SCHLUESSEL or not schluessel.isascii() or not schluessel.isalnum():
            raise ValueError(
                f"ZUGANG_SCHLUESSEL braucht mindestens {MIN_SCHLUESSEL} Zeichen (nur Buchstaben und Ziffern)"
            )
        return Konfig(port, kontakt, quelle, max_je_stunde, tafel, schluessel)


def _kontakt_gueltig(kontakt: str) -> bool:
    """Eine Mail-Adresse oder eine Webseite, ohne Zeichen, die eine Kopfzeile sprengen."""
    if not kontakt or len(kontakt) > 120 or not kontakt.isascii() or not kontakt.isprintable():
        return False
    if any(z in kontakt for z in " ()<>\"\\"):
        return False
    if kontakt.startswith(("http://", "https://")):
        return bool(urlsplit(kontakt).netloc)
    name, _, ort = kontakt.partition("@")
    return bool(name) and "." in ort and "@" not in ort
