"""Einstieg des Fahrplan-Dienstes: Konfiguration lesen, Server bedienen."""

from __future__ import annotations

import logging
import os
import sys

from . import VERSION
from .konfig import Konfig
from .server import Dienst, baue_server

LOG = logging.getLogger("fahrplan_dienst")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        konfig = Konfig.aus_umgebung(os.environ)
    except ValueError as fehler:
        print(f"Fahrplan-Dienst: {fehler}", file=sys.stderr)
        return 2
    if konfig.zugang_schluessel:
        LOG.info("Zugangsschlüssel gesetzt (%d Zeichen)", len(konfig.zugang_schluessel))
    else:
        LOG.warning("Ohne Zugangsschlüssel gestartet (OHNE_SCHLUESSEL=1): nur für lokale Tests")
    server = baue_server(Dienst(konfig), "0.0.0.0", konfig.port)
    LOG.info("fahrplan-dienst %s auf Port %d, höchstens %d Abfragen je Stunde", VERSION, konfig.port,
             konfig.max_je_stunde)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
