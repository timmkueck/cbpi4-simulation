#!/usr/bin/env python3
"""Legt in CraftBeerPi4 die komplette Brausimulation an (Kommandozeile).

Im Browser geht dasselbe ohne dieses Skript: Fühler vom Typ BrauSimulation
anlegen, dort Aktion "Simulation anlegen". Nur Standardbibliothek:

    python3 einrichten.py                              # alles, Dashboard 3
    python3 einrichten.py --zeitraffer-brauen 60 --zeitraffer-gaerung 120
    python3 einrichten.py --ohne-plaene                # Maisch- und Gaerplan nicht anfassen
"""
import argparse
import importlib.util
import sys
from pathlib import Path

# Modul direkt laden: das Paket selbst braucht cbpi, dieses Skript nicht
_spec = importlib.util.spec_from_file_location(
    "sim_einrichtung", Path(__file__).resolve().parent / "cbpi4_simulation" / "einrichtung.py")
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--dashboard", type=int, default=3, help="Nummer des Dashboards (1-4), wird überschrieben")
    ap.add_argument("--zeitraffer-brauen", type=float, default=60)
    ap.add_argument("--zeitraffer-gaerung", type=float, default=120)
    ap.add_argument("--ohne-plaene", action="store_true", help="Maischplan und Gärplan nicht schreiben")
    a = ap.parse_args()
    try:
        E.einrichten(a.url, a.zeitraffer_brauen, a.zeitraffer_gaerung, a.dashboard, not a.ohne_plaene)
    except E.EinrichtungsFehler as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
