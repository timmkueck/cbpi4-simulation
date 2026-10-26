# CLAUDE.md – cbpi4-simulation

Gilt zusätzlich zur übergeordneten `~/Claude/CLAUDE.md`. CraftBeerPi4-Plugin, das den ganzen Brautag ohne
Hardware nachbildet und dabei die echten Plugins regeln lässt (SicherAktor/SicherHysterese aus `cbpi4-brausicher`,
GaerDruckLogik aus `cbpi4-gaerdruck`, VentilDosierer/WasserDosieren aus `cbpi4-ventil`). Teil des **Brau-Verbunds** von Timm. Gesamtbild in `docs/systeme.md` der Repos `Biersteuerung` und
`BrewPiLess-PCB` (gleiche Datei). Nachbar-Repos liegen lokal nebeneinander unter `~/Claude/`:
`cbpi4-brausicher`, `cbpi4-gaerdruck`, `cbpi4-ventil`, `cbpi4-simulation`, `cbpi4-bme280`, `Biersteuerung` (HAT,
Braubrücke, Plugin `cbpi4-kellersensor`).

## Aufbau

- `cbpi4_simulation/modell.py`: Physik ohne cbpi (Kessel, Würzekühler, Gärtank mit Kühlschrank, Gärung, CO₂ in
  Bier/Kopfraum/Leitung, Störfälle). Alles Rechnen gehört hierher, dort testbar.
- `__init__.py`: `BrauSimulation` (zentraler Fühler: Parameter, Rollen, Aktionen), `SimFuehler` (Unterklasse von
  `SicherFuehler`, damit der Heizschutz greift), `SimAktor`, `SimEreignis` (Schritt), `SimulationsTakt` (Erweiterung,
  0,1-s-Takt, Stand in `simulation_zustand.json`).
- `einrichtung.py` und `stoerfaelle.py`: nur Standardbibliothek, laufen per REST gegen cbpi, aus cbpi heraus (Aktionen,
  im Thread) und über `einrichten.py` / `stoerfaelle.py` auf der Kommandozeile (laden die Module direkt, ohne cbpi).
- Kopplung: SicherAktor über den Pegel `steuerung.a.an` des Simulationsausgangs, VentilDosierer über `regler.b`
  (`SimBackend.fluss_lpm` setzen, Zähler `_n` lesen).

## Regeln

- Einrichtung muss **im Browser** gehen (Wunsch von Timm): Fühler Typ BrauSimulation, „Beim Speichern anlegen = Ja“;
  alle weiteren Aktionen über den ⋮-Knopf im Dashboard. Neue Funktionen nicht nur als Skript anbieten.
- Nur in einer eigenen cbpi-Instanz betreiben (nur an 127.0.0.1). Das Anlegen bricht bei gleichnamigen *nicht*
  simulierten Teilen ab; simulierte Aktoren liegen auf freien GPIOs (5–12).
- Zeitraffer gilt für langsame Vorgänge (Wärme, Gärung, Lösen von CO₂), **Wasser und Gas laufen in Echtzeit**, weil
  die echten Plugins Durchfluss und Ventilpulse in Echtzeit messen. Folgen: Plan-Zeiten = Simulationszeit /
  Zeitraffer (ganze Minuten), GaerDruckLogik `Pause s` = 1, Kompressorpause skaliert.
- Fehlmeldungen der echten Plugins erst gegen die Physik prüfen (Echtzeit nachrechnen), bevor man das Modell
  „glattzieht“: Mehrere Funde waren echte Fehler (SicherHysterese-Neustart), andere Zeitraffer-Artefakte.
- Zweig `main`. Tests: `pip install cbpi4 pytest && python -m pytest tests/ -q` (prüft die Kopplung, wenn die
  Nachbar-Repos daneben liegen oder installiert sind). Störfälle: `python3 stoerfaelle.py --url … --log …`.
- UI-Texte mit Umlauten, Code-Kommentare ohne.

## cbpi-4.7.7-Fallen (gemessen, gelten für alle Plugins des Verbunds)

- Ein Sensor darf **nie `None`** liefern: Kessellogik, MashStep und Gärschritte vergleichen ungeprüft. Gültigkeit
  steckt im Alter des Werts (`alter()`).
- Aktor-`get_state()` liefert einen **bool**, kein dict, sonst zeigt die Oberfläche LED/SVG/Leitungen immer „an“.
- Schritte schalten nur mit `asyncio.create_task(self.next())` aus `run()` weiter.
- Kessel- und Gärtanklogik setzen im `finally` von `run()` `self.running = False`. cbpi stoppt sie am Schrittende nur
  über `state` und verweigert sonst den nächsten Start („already running“).
- Der eingebaute „Fermenter Autostart“ startet nichts (ruft `start()` statt `start_logic()`).
- `POST /kettle/{id}/off` liefert HTTP 500 (Methode fehlt), Kessel nur per `toggle` schalten.
- cbpi findet Plugins über `pkgutil.iter_modules`: `pip install -e` wird nicht geladen. Normal installieren oder
  `pip install -e . --config-settings editable_mode=compat`.
- Lokal testen: `web.run_app` bindet 0.0.0.0, also über einen Wrapper mit `host="127.0.0.1"` starten, eigener
  Konfigordner und Port (`cbpi --config-folder-path=… start`, Port in `config.yaml`).
