#!/usr/bin/env python3
"""Stoerfaelle der Brausimulation pruefen (Kommandozeile; im Browser: Aktion
"Störfälle prüfen" am Fuehler BrauSimulation). Nur Standardbibliothek:

    python3 stoerfaelle.py --url http://127.0.0.1:8000 --log ~/cbpi4/logs/cbpi.log

Rueckgabewert: Zahl der Fehlschlaege.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

# Modul direkt laden: das Paket selbst braucht cbpi, dieses Skript nicht
_spec = importlib.util.spec_from_file_location(
    "sim_stoerfaelle", Path(__file__).resolve().parent / "cbpi4_simulation" / "stoerfaelle.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--log", help="Logdatei von cbpi (Ausgabe von cbpi start), dann werden auch Meldungen geprüft")
    a = ap.parse_args()
    sys.exit(S.pruefen(a.url, a.log, lambda text: print(text, flush=True)))


if __name__ == "__main__":
    main()
