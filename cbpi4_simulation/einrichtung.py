"""Legt in CraftBeerPi4 die komplette Brausimulation an (nur Standardbibliothek).

Zwei Wege, beide mit demselben Ergebnis:

* im Browser: Fuehler vom Typ BrauSimulation anlegen, dort Aktion
  "Simulation anlegen" (ruft einrichten() im laufenden cbpi auf)
* auf der Kommandozeile: einrichten.py neben dem Paket

Angelegt werden Fuehler (SimFuehler), Aktoren (SicherAktor/VentilDosierer in
Simulation, sonst SimAktor), Sudkessel, Gaertank, Maischplan, Gaerplan und
ein Dashboard; der Fuehler BrauSimulation bekommt die Rollen der Aktoren.
Vorhandenes (gleicher Name) wird wiederverwendet, aber nur, wenn es selbst
simuliert ist: Ein echter Aktor oder Fuehler gleichen Namens bricht ab.
Der Maischplan von cbpi und der Gaerplan des Gaertanks werden ersetzt.
"""
import json
import urllib.error
import urllib.request

KESSEL = "Sudkessel (Sim)"
KOCHER = "Würzekocher (Sim)"
TANK = "FermZilla (Sim)"
ZENTRALE = "Brausimulation"
# Fuehlername -> Messgroesse (der Kuehlschrank als Aktor heisst schon "Kühlschrank")
FUEHLER = {name: name for name in ("Kesseltemperatur", "Kesselvolumen", "Würze Kessel",
                                   "Kochertemperatur", "Kochervolumen", "Würze Kocher",
                                   "Durchfluss", "Wassermenge",
                                   "Heizleistung", "Biertemperatur", "Tankdruck", "CO₂-Vordruck", "Dichte",
                                   "CO₂-Flasche", "Gärtage")}
FUEHLER["Kühlschrank Luft"] = "Kühlschrank"
SUDNAME = "Sim-Sud"
REZEPT_ID = "simulation-sud"  # Datei recipes/simulation-sud.yaml
# GPIOs wie auf der Platine (Biersteuerung, Zweig ventile-druck). Seit die
# Platine jeden GPIO von 2 bis 27 fuehrt, gibt es keine freien Pins mehr, auf die
# die Simulation ausweichen koennte -- sie bildet deshalb die echte Belegung ab.
# Unbedenklich, weil alle Teile mit "Simulation = Ja" laufen und keinen GPIO
# anfassen, und weil die Simulation ohnehin in einer eigenen cbpi-Instanz lebt.
AKTOREN = {  # Name -> (GPIO, Art)
    "Maischepfanne": (27, "Heizung"),          # Relais J4
    "Würzekocher": (21, "Heizung"),            # Relais J15
    "Pumpe": (22, "Sonstiges"),                # Relais J16
    "Rührwerk": (18, "Rührwerk"),              # Relais J7
    "Kühlschrank": (19, "Sonstiges"),          # Gaerschrank kuehlen, J13
    "Gärschrank Heizung": (20, "Sonstiges"),   # Gaerschrank heizen, J8
    "Kühlwasser": (10, "Sonstiges"),           # Ventil 6, Kuehlspirale
    "Dampfkondensator": (11, "Sonstiges"),     # Ventil 7
    "Spundventil": (16, "Sonstiges"),          # Ventil 1
    "CO₂-Ventil": (17, "Sonstiges"),           # Ventil 2
    "Wegeventil Maischebottich": (23, "Sonstiges"),  # Ventil 8
    "Wegeventil Kochkessel": (24, "Sonstiges"),      # Ventil 9
}
WASSERVENTIL = ("Wasserventil", 9, 6)  # Name, GPIO Ventil (J20), GPIO Durchfluss (J19)
WASSER_L = 28
# Maischplan in Simulationsminuten
RASTEN = [("Maltoserast", 63, 60), ("Verzuckerungsrast", 72, 30), ("Abmaischen", 78, 10)]
EINMAISCHEN_C = 57
KOCHEN_MIN = 60
ANSTELLEN_C = 20
# Umpumpen laeuft in Echtzeit: Menge durch Foerderleistung, aufgerundet
UMPUMPEN_MIN = 3
# Das Ruehrwerk laeuft ueber die ganze Maische, der Dampfkondensator ab dieser Temperatur
# bis zum Ende des Kochens. Beides schalten Schritte im Rezept (ToggleStep), nicht die
# Kessellogik: cbpi 4.7.7 fasst den Ruehrwerksausgang eines Kessels nirgends selbst an.
DAMPF_AB_C = 80
# Gaerplan: Name, Temperatur, Druck bar, Tage
GAERPLAN = [("Hauptgärung (spunden)", 18, 1.0, 4), ("Reifung", 19, 1.0, 1), ("Kaltcrash (nachdrücken)", 2, 1.0, 2)]


class EinrichtungsFehler(Exception):
    pass


aus = print  # Ausgabe; einrichten() setzt sie (Meldungen in cbpi oder Konsole)


class Cbpi:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def rufen(self, pfad, daten=None, methode=None):
        body = None if daten is None else json.dumps(daten).encode()
        methode = methode or ("POST" if body is not None else "GET")
        req = urllib.request.Request(self.url + pfad, data=body, method=methode,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            raise EinrichtungsFehler(f"{methode} {pfad}: HTTP {e.code} {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            raise EinrichtungsFehler(f"CraftBeerPi unter {self.url} nicht erreichbar: {e.reason}")
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            return text

    def liste(self, art):
        antwort = self.rufen(f"/{art}/")
        return antwort.get("data", antwort) if isinstance(antwort, dict) else antwort

    def typen(self, art):
        antwort = self.rufen(f"/{art}/")
        return set((antwort.get("types") or {}).keys()) if isinstance(antwort, dict) else set()


def nach_name(eintraege, name):
    return next((e for e in eintraege if e.get("name") == name), None)


def simuliert(eintrag):
    props = eintrag.get("props") or {}
    return eintrag.get("type") in ("SimFuehler", "SimAktor", "BrauSimulation") or props.get("Simulation") == "Ja"


def sicherstellen(cbpi, art, name, daten, pruefen=True, aktualisieren=False):
    """Legt an oder nimmt Vorhandenes. `aktualisieren`: Vorhandenes bekommt die neue
    Konfiguration (Kessel und Gaertank gehoeren ganz der Simulation)."""
    vorhanden = nach_name(cbpi.liste(art), name)
    if vorhanden:
        if pruefen and not simuliert(vorhanden):
            raise EinrichtungsFehler(f"{art} „{name}“ gibt es schon und ist NICHT simuliert (Typ {vorhanden.get('type')}). "
                     "Abbruch, damit die Simulation keine echte Hardware schaltet. Umbenennen oder eine eigene "
                     "cbpi-Instanz für die Simulation nehmen.")
        if aktualisieren:
            cbpi.rufen(f"/{art}/{vorhanden['id']}", dict(name=name, **daten), methode="PUT")
            aus(f"  erneuert:  {name}")
        else:
            aus(f"  vorhanden: {name}")
        return vorhanden["id"]
    neu = cbpi.rufen(f"/{art}/", dict(name=name, **daten))
    aus(f"  angelegt:  {name}")
    return neu["id"]


def echt_min(sim_min, faktor):
    """Timer in cbpi laufen in Echtzeit, nur ganze Minuten."""
    return max(1, round(sim_min / faktor))


# --- Plaene ---------------------------------------------------------------------

def maischplan(ids, faktor, mit_dosierer):
    """Schritte fuer cbpi (Maischplan). Zeiten in Echtminuten.

    Zwei Gefaesse: gemaischt wird in der Maischepfanne, gekocht im Wuerzekocher.
    Dazwischen schiebt die Pumpe (Relais J16) ueber das Wegeventil Richtung
    Kochkessel (Ventil 9) um."""
    k, sensor = ids["kessel"], ids["Kesseltemperatur"]
    kk, sensor_k = ids["kocher"], ids["Kochertemperatur"]
    s = []
    if mit_dosierer:
        s.append(("Wasser dosieren", "WasserDosieren", {"Liter": WASSER_L, "Ventil": ids["Wasserventil"]}))
    else:
        s.append(("Wasser einfüllen", "SimEreignis", {"Ereignis": "Wasser einfüllen", "Liter": WASSER_L}))
    # Ruehrwerk an, bevor geheizt wird, und erst nach dem Abmaischen wieder aus.
    # ToggleStep schaltet einmal und geht weiter; der Ausgang bleibt ueber alle
    # dazwischenliegenden Schritte stehen.
    s.append(("Rührwerk ein", "ToggleStep", {"toggle_type": "On", "Actor": ids["Rührwerk"]}))
    s.append(("Aufheizen zum Einmaischen", "MashStep", {"Temp": EINMAISCHEN_C, "Timer": 0, "Sensor": sensor,
                                                       "Kettle": k, "AutoMode": "Yes"}))
    s.append(("Einmaischen", "SimEreignis", {"Ereignis": "Einmaischen"}))
    for name, temp, minuten in RASTEN:
        s.append((name, "MashStep", {"Temp": temp, "Timer": echt_min(minuten, faktor), "Sensor": sensor,
                                     "Kettle": k, "AutoMode": "Yes"}))
    s.append(("Rührwerk aus", "ToggleStep", {"toggle_type": "Off", "Actor": ids["Rührwerk"]}))
    s.append(("Treber ziehen", "SimEreignis", {"Ereignis": "Treber ziehen"}))
    # Umpumpen: erst den Weg oeffnen, dann die Pumpe. Die Pumpe foerdert in
    # Echtzeit, die Wartezeit richtet sich also nach der Menge, nicht nach dem
    # Zeitraffer. Zum Schluss Pumpe aus, dann erst das Ventil -- nie umgekehrt,
    # sonst foerdert sie gegen ein geschlossenes Ventil.
    s.append(("Weg zum Kocher öffnen", "ToggleStep",
              {"toggle_type": "On", "Actor": ids["Wegeventil Kochkessel"]}))
    s.append(("Pumpe ein", "ToggleStep", {"toggle_type": "On", "Actor": ids["Pumpe"]}))
    s.append(("Umpumpen", "WaitStep", {"Timer": UMPUMPEN_MIN}))
    s.append(("Pumpe aus", "ToggleStep", {"toggle_type": "Off", "Actor": ids["Pumpe"]}))
    s.append(("Weg zum Kocher schließen", "ToggleStep",
              {"toggle_type": "Off", "Actor": ids["Wegeventil Kochkessel"]}))
    # Dampfkondensator ab DAMPF_AB_C: ein eigener Aufheizschritt ohne Timer endet genau
    # dort, danach schaltet der ToggleStep. Waehrend der Maische wird die Schwelle nie
    # erreicht (Abmaischen bei 78 Grad).
    s.append((f"Aufheizen auf {DAMPF_AB_C} °C", "MashStep", {"Temp": DAMPF_AB_C, "Timer": 0, "Sensor": sensor_k,
                                                             "Kettle": kk, "AutoMode": "Yes"}))
    s.append(("Dampfkondensator ein", "ToggleStep", {"toggle_type": "On", "Actor": ids["Dampfkondensator"]}))
    s.append(("Kochen", "BoilStep", {"Temp": 99, "Timer": echt_min(KOCHEN_MIN, faktor), "Sensor": sensor_k,
                                     "Kettle": kk, "AutoMode": "Yes", "LidAlert": "No", "First_Wort": "No"}))
    s.append(("Dampfkondensator aus", "ToggleStep", {"toggle_type": "Off", "Actor": ids["Dampfkondensator"]}))
    s.append(("Abkühlen", "CooldownStep", {"Temp": ANSTELLEN_C, "Sensor": sensor_k, "Actor": ids["Kühlwasser"],
                                           "Kettle": kk}))
    s.append(("Anstellen", "SimEreignis", {"Ereignis": "Anstellen", "Gärtank": ids["tank"],
                                           "Gärplan starten": "Ja"}))
    return s


def gaerplan(ids, faktor):
    return [(name, "FermenterStep", {"Temp": temp, "Pressure": druck, "TimerD": 0, "TimerH": 0,
                                     "TimerM": echt_min(tage * 1440, faktor), "Sensor": ids["Biertemperatur"],
                                     "AutoMode": "Yes"})
            for name, temp, druck, tage in GAERPLAN]


def plaene_schreiben(cbpi, ids, zeitraffer_brauen, zeitraffer_gaerung, mit_dosierer):
    """Maischplan als Rezept (das Schritt-Widget zeigt nur einen Plan mit Namen) und
    gleich zum Brauen laden; Gaerplan direkt in den Gaertank."""
    aus(f"Maischplan „{SUDNAME}“ (Echtminuten):")
    schritte = [dict(name=name, type=typ, props=props)
                for name, typ, props in maischplan(ids, zeitraffer_brauen, mit_dosierer)]
    cbpi.rufen(f"/recipe/{REZEPT_ID}", dict(basic=dict(name=SUDNAME, author="cbpi4-simulation"), steps=schritte),
               methode="PUT")
    cbpi.rufen(f"/recipe/{REZEPT_ID}/brew", {})
    for st in schritte:
        zeit = f", {st['props']['Timer']} min" if "Timer" in st["props"] else ""
        aus(f"  {st['name']} ({st['type']}{zeit})")
    aus("Gärplan (Echtminuten):")
    cbpi.rufen(f"/fermenter/{ids['tank']}/clearsteps", {})
    for name, typ, props in gaerplan(ids, zeitraffer_gaerung):
        cbpi.rufen(f"/fermenter/{ids['tank']}/addstep", dict(name=name, type=typ, props=props))
        aus(f"  {name}: {props['Temp']} °C, {props['Pressure']} bar, {props['TimerM']} min")
    # clearsteps loescht den Sudnamen; ohne ihn zeigt das Gaerplan-Widget nichts
    f = next(x for x in cbpi.liste("fermenter") if x["id"] == ids["tank"])
    felder = ("name", "sensor", "pressure_sensor", "heater", "cooler", "valve", "description", "target_temp",
              "target_pressure", "props", "type")
    cbpi.rufen(f"/fermenter/{ids['tank']}", {**{k: f.get(k) for k in felder}, "brewname": SUDNAME}, methode="PUT")


# --- Dashboard ------------------------------------------------------------------

def dashboard(t):
    e = []
    n = iter(range(1, 200))
    weiss, grau, gruen, blau, orange, rot = "#ffffff", "#9e9e9e", "#4caf50", "#4fc3f7", "#ffb74d", "#ef5350"

    def add(name, typ, x, y, **props):
        e.append(dict(id=f"sim{next(n):03d}", name=name, type=typ, x=x, y=y,
                      props={k: str(v) for k, v in props.items()}))

    def wert(titel, sensor, x, y, einheit, stellen, farbe=weiss, groesse=18, aktion="no"):
        """aktion="yes": Knopf neben dem Wert oeffnet die Aktionen des Fuehlers (z. B. Fuehlerausfall)"""
        add(titel, "Text", x, y, size=11, color=grau)
        add(f"Wert {titel}", "Sensor", x, y + 14, sensor=sensor, unit=f" {einheit}", size=groesse, digits=stellen,
            color=farbe, fontweight="bold", action=aktion)

    add("Brausimulation", "Text", 20, 10, size=24, fontweight="bold", color=weiss)
    add("Uhr", "Clock", 820, 14, size=18, showDate="yes", color=grau)
    # Knopf neben dem Zaehler: alle Aktionen der Simulation (Stoerfaelle, Ereignisse, Pruefung, Anlegen)
    add("Störfälle aktiv", "Text", 300, 18, size=12, color=grau)
    add("Störfälle", "Sensor", 400, 12, sensor=t["zentrale"], unit="", size=20, digits=0, color=rot, action="yes")

    # Kessel
    add("Sudkessel", "Text", 20, 50, size=16, fontweight="bold", color=weiss)
    add("Kessel", "Kettle", 20, 76, width=110, heigth=160)
    wert("Kessel", t["Kesseltemperatur"], 150, 76, "°C", 1, orange, 24, aktion="yes")
    add("Soll", "Text", 150, 124, size=11, color=grau)
    add("Soll Kessel", "TargetTemp", 150, 138, kettle=t["kessel"], unit=" °C", size=16, color=weiss)
    wert("Volumen", t["Kesselvolumen"], 240, 76, "L", 1)
    wert("Würze", t["Würze Kessel"], 240, 124, "", 3)
    wert("Wassermenge", t["Wassermenge"], 150, 172, "L", 1, blau, 14)
    wert("Durchfluss", t["Durchfluss"], 240, 172, "L/min", 1, blau, 14)
    wert("Heizleistung", t["Heizleistung"], 240, 210, "W", 0, rot, 14)
    add("Kessel ein/aus", "KettleControl", 20, 250, kettle=t["kessel"], size="normal", orientation="horizontal")
    for i, (name, farbe) in enumerate((("Maischepfanne", "red"), ("Rührwerk", "green"), ("Kühlwasser", "blue"),
                                       ("Wasserventil", "blue"))):
        if t.get(name):
            add(f"LED {name}", "Led", 20 + i * 85, 300, actor=t[name], color=farbe)
            add(name, "Text", 44 + i * 85, 302, size=11, color=weiss)

    # Brauablauf
    add("Brauablauf", "Text", 360, 50, size=16, fontweight="bold", color=weiss)
    add("Schritte", "Steps", 360, 76, width=280, maxheight=250, stepsize=12, namesize=12)

    # Gaertank
    add("Gärtank", "Text", 670, 50, size=16, fontweight="bold", color=weiss)
    add("Tank", "Tank", 670, 76, width=90, heigth=160)
    wert("Bier", t["Biertemperatur"], 780, 76, "°C", 1, orange, 24, aktion="yes")
    wert("Kühlschrank", t["Kühlschrank Luft"], 880, 76, "°C", 1, blau, 14)
    wert("Tankdruck", t["Tankdruck"], 780, 124, "bar", 2, gruen, 22, aktion="yes")
    wert("CO₂-Vordruck", t["CO₂-Vordruck"], 880, 124, "bar", 2, blau, 14, aktion="yes")
    wert("Dichte", t["Dichte"], 780, 172, "", 4, weiss, 18)
    wert("Gärtage", t["Gärtage"], 880, 172, "d", 1, grau, 14)
    wert("CO₂-Flasche", t["CO₂-Flasche"], 880, 210, "bar", 1, grau, 12)
    add("Soll °C", "Text", 670, 250, size=11, color=grau)
    add("Soll Temp", "FermenterTargetTemp", 670, 264, fermenter=t["tank"], unit=" °C", size=16, color=weiss)
    add("Soll bar", "Text", 760, 250, size=11, color=grau)
    add("Soll Druck", "FermenterTargetPressure", 760, 264, fermenter=t["tank"], unit=" bar", size=16, color=weiss)
    add("Gärtank ein/aus", "FermenterControl", 850, 256, fermenter=t["tank"], size="normal",
        orientation="horizontal")
    for i, (name, farbe) in enumerate((("Kühlschrank", "blue"), ("Spundventil", "green"), ("CO₂-Ventil", "yellow"))):
        add(f"LED {name}", "Led", 670 + i * 110, 300, actor=t[name], color=farbe)
        add(f"{name} ", "Text", 694 + i * 110, 302, size=11, color=weiss)

    # Verlaeufe und Gaerplan
    for i, (titel, sensor, farbe) in enumerate((("Kesseltemperatur °C", "Kesseltemperatur", orange),
                                                ("Biertemperatur °C", "Biertemperatur", orange),
                                                ("Tankdruck bar", "Tankdruck", gruen), ("Dichte SG", "Dichte", weiss))):
        x = 20 + i * 250
        add(titel, "Text", x, 340, size=11, color=grau)
        add(f"Verlauf {titel}", "Chart", x, 356, sensor=t[sensor], linecolor=farbe, refresh=10, width=235,
            height=170)
    add("Gärplan", "FermenterSteps", 20, 540, fermenter=t["tank"], width=980, maxheight=200, stepsize=12,
        namesize=12)
    return dict(elements=e, pathes=[])


def aktoren_anlegen(cbpi, ids, sicher, dosierer):
    """Legt die Aktoren an oder zieht ihre Einstellungen nach; gibt Name -> ID zurueck."""
    neu = {}
    for name, (gpio, art) in AKTOREN.items():
        if sicher:
            props = {"GPIO": gpio, "Aktiv bei": "HIGH", "Art": art, "Takt s": 5, "Simulation": "Ja"}
            if art == "Heizung":
                props.update({"Fühler": ids["Kochertemperatur" if name == "Würzekocher"
                                           else "Kesseltemperatur"], "Max Temperatur": 105, "Anstieg Minuten": 10,
                              "Mindestanstieg K": 1, "Anstieg ab °C ignorieren": 95})
            # aktualisieren: auch vorhandene simulierte Aktoren bekommen die aktuellen GPIOs
            neu[name] = sicherstellen(cbpi, "actor", name, dict(type="SicherAktor", props=props), aktualisieren=True)
        else:
            neu[name] = sicherstellen(cbpi, "actor", name, dict(type="SimAktor", props={}))
    name, gpio_v, gpio_f = WASSERVENTIL
    if dosierer:
        neu[name] = sicherstellen(cbpi, "actor", name, dict(type="VentilDosierer", props={
            "GPIO Ventil": gpio_v, "GPIO Durchfluss": gpio_f, "Ventil offen bei": "HIGH", "Impulse pro Liter": 387,
            "Max Liter": 40, "Simulation": "Ja"}), aktualisieren=True)
    else:
        neu[name] = sicherstellen(cbpi, "actor", name, dict(type="SimAktor", props={}))
    return neu


# --- Hauptteil ------------------------------------------------------------------

def einrichten(url, zeitraffer_brauen=60.0, zeitraffer_gaerung=120.0, dashboard_nr=3, plaene=True, zentrale_id=None,
               ausgabe=print, zentrale_props=None):
    """Legt alles an. `zentrale_id`: vorhandener Fuehler BrauSimulation (Aufruf aus cbpi),
    sonst wird "Brausimulation" angelegt; `zentrale_props` kommen zusaetzlich in dessen
    Einstellungen. Fehler -> EinrichtungsFehler."""
    global aus
    aus = ausgabe

    cbpi = Cbpi(url)

    s_typen, a_typen = cbpi.typen("sensor"), cbpi.typen("actor")
    k_typen, f_typen = cbpi.typen("kettle"), cbpi.typen("fermenter")
    if not {"BrauSimulation", "SimFuehler"} <= s_typen:
        raise EinrichtungsFehler("Plugin cbpi4-simulation fehlt in CraftBeerPi (installieren, cbpi neu starten)")
    sicher = "SicherAktor" in a_typen
    dosierer = "VentilDosierer" in a_typen
    for fehlt, folge in ((not sicher, "cbpi4-brausicher fehlt: SimAktor statt SicherAktor, kein Heizschutz"),
                         (not dosierer, "cbpi4-ventil fehlt: Wasser per SimEreignis statt Dosierung"),
                         ("GaerDruckLogik" not in f_typen, "cbpi4-gaerdruck fehlt: Gärtank ohne Druckregelung")):
        if fehlt:
            aus(f"HINWEIS: {folge}")

    aus("Fühler:")
    ids = {name: sicherstellen(cbpi, "sensor", name, dict(type="SimFuehler", props={
        "Messgröße": groesse, "Max Alter s": 10})) for name, groesse in FUEHLER.items()}

    aus("Aktoren:")
    ids.update(aktoren_anlegen(cbpi, ids, sicher, dosierer))
    # Zweiter Durchgang: Beim Umstellen auf neue GPIOs kann ein Aktor kurz auf einer Nummer landen, die ein
    # anderer noch belegt (SicherAktor/VentilDosierer melden dann "nicht bereit"). Jetzt stehen alle Ziele fest.
    sprechen, aus = aus, (lambda *a: None)
    try:
        aktoren_anlegen(cbpi, ids, sicher, dosierer)
    finally:
        aus = sprechen

    aus("Kessel und Gärtank:")
    ids["kessel"] = sicherstellen(cbpi, "kettle", KESSEL, dict(
        type="SicherHysterese" if "SicherHysterese" in k_typen else "Hysteresis",
        sensor=ids["Kesseltemperatur"], heater=ids["Maischepfanne"], agitator=ids["Rührwerk"], target_temp=0,
        props={"OffsetOn": 0.5, "OffsetOff": 0}), pruefen=False, aktualisieren=True)
    ids["kocher"] = sicherstellen(cbpi, "kettle", KOCHER, dict(
        type="SicherHysterese" if "SicherHysterese" in k_typen else "Hysteresis",
        sensor=ids["Kochertemperatur"], heater=ids["Würzekocher"], agitator="", target_temp=0,
        props={"OffsetOn": 0.5, "OffsetOff": 0}), pruefen=False, aktualisieren=True)
    if "GaerDruckLogik" in f_typen:
        f_typ = "GaerDruckLogik"
        f_props = {"Ventil Drücken": ids["CO₂-Ventil"], "Fühler CO₂-Vordruck": ids["CO₂-Vordruck"],
                   "Grenzdruck bar": 2.0, "Nachdrücken": "Ja", "Bei Fühlerausfall": "Spundventil pulsen",
                   # Kompressorpause laeuft in Echtzeit: 5 Simulationsminuten
                   "Kompressorpause min": round(5 / zeitraffer_gaerung, 3),
                   # Pause nach jedem Ventilpuls kuerzer, sonst kommt das Spunden gegen die
                   # beschleunigte Gaerung nicht an (Ventile laufen in Echtzeit)
                   "Pause s": 1, "AutoStart": "No"}
    else:
        f_typ, f_props = "FermenterHysteresis", {"HeaterOffsetOn": 0.3, "HeaterOffsetOff": 0, "CoolerOffsetOn": 0.3,
                                                 "CoolerOffsetOff": 0, "AutoStart": "No"}
    ids["tank"] = sicherstellen(cbpi, "fermenter", TANK, dict(
        type=f_typ, sensor=ids["Biertemperatur"], pressure_sensor=ids["Tankdruck"],
        heater=ids["Gärschrank Heizung"],
        cooler=ids["Kühlschrank"], valve=ids["Spundventil"], brewname=SUDNAME,
        description="Simulierter FermZilla", target_temp=20, target_pressure=0, props=f_props), pruefen=False,
        aktualisieren=True)

    aus("Zentrale:")
    rollen = {"Heizung Kessel": ids["Maischepfanne"], "Heizung Würzekocher": ids["Würzekocher"],
              "Wasserventil": ids["Wasserventil"],
              "Kühlwasser Würzekühler": ids["Kühlwasser"], "Kühlung Gärtank": ids["Kühlschrank"],
              "Heizung Gärtank": ids["Gärschrank Heizung"],
              "Spundventil": ids["Spundventil"], "CO₂-Ventil": ids["CO₂-Ventil"],
              "Pumpe": ids["Pumpe"],
              "Wegeventil Maischebottich": ids["Wegeventil Maischebottich"],
              "Wegeventil Kochkessel": ids["Wegeventil Kochkessel"]}
    params = {"Zeitraffer Brauen": zeitraffer_brauen, "Zeitraffer Gärung": zeitraffer_gaerung}
    if zentrale_id:  # aus cbpi heraus: der Fuehler, an dem die Aktion aufgerufen wurde
        z = next((x for x in cbpi.liste("sensor") if x["id"] == zentrale_id), None)
        if z is None:
            raise EinrichtungsFehler(f"Fühler {zentrale_id} nicht gefunden")
        aus(f"  verwendet: {z['name']}")
    else:
        sicherstellen(cbpi, "sensor", ZENTRALE, dict(type="BrauSimulation", props={**rollen, **params}))
        z = nach_name(cbpi.liste("sensor"), ZENTRALE)
    ids["zentrale"] = z["id"]
    # Rollen und Zeitraffer nachziehen, uebrige Parameter bleiben
    props = {**(z.get("props") or {}), **rollen, **params, **(zentrale_props or {})}
    cbpi.rufen(f"/sensor/{z['id']}", dict(name=z["name"], type="BrauSimulation", props=props), methode="PUT")

    if plaene:
        plaene_schreiben(cbpi, ids, zeitraffer_brauen, zeitraffer_gaerung, dosierer)

    cbpi.rufen(f"/dashboard/{dashboard_nr}/content", dashboard(ids))
    aus(f"Dashboard {dashboard_nr} geschrieben: {url}/  (oben rechts Dashboard {dashboard_nr} wählen)")
    aus("Start: Brauen -> Start. Störfälle und weitere Aktionen: Dashboard, Knopf neben „Störfälle aktiv“.")
    return ids
