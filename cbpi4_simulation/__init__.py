"""CraftBeerPi4-Plugin: der ganze Brautag als Simulation.

BrauSimulation  zentraler "Fuehler": Parameter (Zeitraffer, Volumen,
                Heizleistung, Waermeverluste, Gaerrate ...), Zuordnung der
                Aktoren, Aktionen fuer Ereignisse und Stoerfaelle, dazu
                "Simulation anlegen" (alles Uebrige per Klick im Browser)
                und "Störfälle prüfen". Wert = Zahl der aktiven Stoerfaelle.
SimFuehler      liefert eine Messgroesse aus dem Modell. Unterklasse von
                SicherFuehler (cbpi4-brausicher), damit der Heizschutz des
                echten SicherAktor greift. Nie None, kennt sein Alter.
SimAktor        einfacher Aktor fuer Rollen ohne echtes Plugin.
SimEreignis     Schritt im Maischplan: Wasser einfuellen, Einmaischen,
                Treber ziehen, Anstellen (startet auf Wunsch den Gaerplan).
SimulationsTakt Erweiterung: rechnet das Modell alle 0,1 s fort und liest
                dazu die echten Ausgaenge (SicherAktor: Pegel des
                Simulationsausgangs mit Takt, Heizschutz und Waechter;
                VentilDosierer: Impulse seiner Simulation).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from cbpi.api import (CBPiActor, CBPiExtension, CBPiSensor, CBPiStep, Property, action, on_startup,
                      parameters)
from cbpi.api.dataclasses import NotificationType

from . import einrichtung, modell, stoerfaelle
from .modell import MESSGROESSEN, STOERFAELLE, Ausgaenge, Parameter, Welt

try:  # mit cbpi4-brausicher: SimFuehler gilt dort als SicherFuehler (Heizschutz)
    from cbpi4_brausicher import SicherFuehler as _FuehlerBasis
except Exception:  # pragma: no cover - ohne brausicher geht es auch
    _FuehlerBasis = CBPiSensor

log = logging.getLogger("cbpi4_simulation")

WELT = Welt()
TAKT_S = 0.1
SICHERN_S = 10
DATEI = "simulation_zustand.json"

# Parameter im Fuehler BrauSimulation: (Name in cbpi, Feld, Faktor zur Modelleinheit, Beschreibung)
PARAMETER = [
    ("Zeitraffer Brauen", "zeitraffer_brauen", 1, "Kessel: 60 = eine Minute in einer Sekunde. Maischplan-Zeiten "
     "durch diesen Faktor teilen (einrichten.py tut das)"),
    ("Zeitraffer Gärung", "zeitraffer_gaerung", 1, "Gärtank: 120 = ein Tag in 12 Minuten. Über ~200 kommen die "
     "Ventilpulse (Echtzeit) gegen die Gärung nicht mehr an"),
    ("Raumtemperatur °C", "raum_c", 1, ""),
    ("Leitungswasser °C", "leitungswasser_c", 1, ""),
    ("Durchfluss L/min", "durchfluss_lpm", 1, "Wasserhahn, Echtzeit (der VentilDosierer misst in Echtzeit)"),
    ("Heizleistung W", "heizleistung_w", 1, ""),
    ("Wärmeverlust Kessel W/K", "kessel_verlust_w_k", 1, "Kessel an den Raum, je K Unterschied"),
    ("Würzekühler W/K", "kuehler_w_k", 1, "Wärmeübergang des Kühlers bei laufendem Kühlwasser"),
    ("Kühlwasser °C", "kuehlwasser_c", 1, ""),
    ("Siedepunkt °C", "siedepunkt_c", 1, ""),
    ("Schüttung kg", "schuettung_kg", 1, "für Einmaischen"),
    ("Ausbeute %", "ausbeute", 0.01, "kg Extrakt je kg Malz"),
    ("Gärtank L", "gaertank_l", 1, "Gesamtvolumen, Rest über dem Bier ist Kopfraum"),
    ("Würze L ohne Brautag", "wuerze_l", 1, "für „Gärtank füllen“ ohne Würze im Kessel"),
    ("Stammwürze °P ohne Brautag", "stammwuerze_p", 1, ""),
    ("Vergärungsgrad %", "vergaerungsgrad", 0.01, "scheinbar"),
    ("Gärrate", "gaerrate", 1, "1 = obergärig bei 18 °C, Hauptgärung etwa 3 Tage"),
    ("Kälteleistung W", "kaelteleistung_w", 1, "Kühlschrank"),
    ("Wärmeverlust Kühlschrank W/K", "kuehlschrank_verlust_w_k", 1, ""),
    ("Heizung Gärtank W", "gaertank_heizung_w", 1, ""),
    ("CO₂-Vordruck bar", "co2_vordruck_bar", 1, "Druckminderer"),
    ("CO₂-Flasche g", "co2_flasche_g", 1, "Inhalt einer vollen Flasche"),
    ("CO₂-Flasche L", "co2_flasche_l", 1, "Innenvolumen der Flasche (2-kg-Flasche: 2,7 L). Bestimmt, "
     "ab wann der Druck fällt: Solange flüssiges CO₂ da ist, steht der Dampfdruck an"),
    ("Überdruckventil bar", "ueberdruckventil_bar", 1, "mechanisches PRV am Deckel (All Rounder: 2,4 bar)"),
    ("Rauschen", "rauschen", 1, "Messrauschen, 0 = aus, 1 = üblich"),
]
ROLLEN = [
    ("Heizung Kessel", "heizung"),
    ("Wasserventil", "wasser"),
    ("Kühlwasser Würzekühler", "kuehlwasser"),
    ("Kühlung Gärtank", "kuehlung"),
    ("Heizung Gärtank", "gaertank_heizung"),
    ("Spundventil", "spund"),
    ("CO₂-Ventil", "co2"),
]
# Stoerfall in cbpi -> (Schluessel im Modell, erwartete Reaktion der echten Plugins)
STOERFALL_AUSWAHL = {
    "Fühlerausfall Kesseltemperatur": ("ausfall:Kesseltemperatur",
                                       "SicherAktor sperrt die Heizung, sobald der Wert älter als „Max Alter s“ ist"),
    "Fühlerausfall Biertemperatur": ("ausfall:Biertemperatur", "GaerDruckLogik schaltet Heizung und Kühlung aus"),
    "Fühlerausfall Tankdruck": ("ausfall:Tankdruck", "GaerDruckLogik: „Tankdruck unbekannt“, Spundventil pulst, "
                                "kein Nachdrücken"),
    "Fühlerausfall CO₂-Vordruck": ("ausfall:CO₂-Vordruck", "GaerDruckLogik: „CO₂-Vordruck unbekannt“, Nachdrücken "
                                   "ausgesetzt"),
    STOERFAELLE["co2_leer"]: ("co2_leer", "Vordruck 0: „CO₂-Vordruck zu niedrig“, ohne Vordruckfühler nach "
                              "6 Pulsen „Leck oder CO₂ leer“"),
    STOERFAELLE["gaspfosten_zu"]: ("gaspfosten_zu", "Druck springt je Puls: „Gasweg zum Tank verstopft“. Im Tank "
                                   "steigt der Druck weiter, bis das Überdruckventil abbläst"),
    STOERFAELLE["kessel_trocken"]: ("kessel_trocken", "SicherAktor sperrt bei „Max Temperatur“ bis „Schutz "
                                    "zurücksetzen“"),
    STOERFAELLE["heizung_klebt"]: (
        "heizung_klebt", "Heizung bleibt an, obwohl cbpi aus schaltet. Keine Software fängt das ab: "
        "Temperaturbegrenzer und Schütz sind Pflicht"),
    STOERFAELLE["kuehlung_defekt"]: ("kuehlung_defekt", "Biertemperatur steigt über Soll"),
    STOERFAELLE["kein_wasser"]: ("kein_wasser", "VentilDosierer: „kein Fluss“ nach der Anlaufzeit, Dosierung bricht "
                                 "ab"),
}


def _zahl(props, name, vorgabe):
    try:
        wert = props.get(name, vorgabe)
        return float(vorgabe if wert in (None, "") else wert)
    except (TypeError, ValueError):
        return float(vorgabe)


def _instanz(controller, item_id):
    item = controller.find_by_id(item_id) if item_id else None
    return getattr(item, "instance", None)


def parameter_aus(props):
    vorgabe = Parameter()
    werte = {}
    for name, feld, faktor, _ in PARAMETER:
        werte[feld] = _zahl(props, name, getattr(vorgabe, feld) / faktor) * faktor
    return Parameter(**werte)


def zentrale(cbpi):
    """Erster Fuehler vom Typ BrauSimulation (Eintrag in cbpi.sensor.data) oder None."""
    for item in getattr(cbpi.sensor, "data", []) or []:
        if getattr(item, "type", None) == "BrauSimulation" or isinstance(getattr(item, "instance", None),
                                                                         BrauSimulation):
            return item
    return None


def ausgang(cbpi, aktor_id):
    """Was der Aktor wirklich tut, 0..1."""
    inst = _instanz(cbpi.actor, aktor_id)
    if inst is None:
        return 0.0
    if hasattr(inst, "steuerung"):  # SicherAktor: der Pegel am (simulierten) Ausgang
        st = inst.steuerung
        return 1.0 if st is not None and getattr(st.a, "an", False) else 0.0
    if not getattr(inst, "state", False):
        return 0.0
    leistung = getattr(inst, "power", 100)
    return max(0.0, min(1.0, (100 if leistung is None else float(leistung)) / 100.0))


class Wasserzaehler:
    """Liest die Impulse aus der Simulation eines VentilDosierer, ohne in dessen
    Thread einzugreifen (nur Attribute lesen), und stellt dessen Durchfluss."""

    def __init__(self):
        self.backend = None
        self.stand = 0

    def liter(self, cbpi, aktor_id, welt):
        inst = _instanz(cbpi.actor, aktor_id)
        regler = getattr(inst, "regler", None)
        backend = getattr(regler, "b", None)
        if backend is None or not hasattr(backend, "fluss_lpm"):  # kein VentilDosierer in Simulation
            self.backend = None
            return None, False
        backend.fluss_lpm = 0.0 if welt.aktiv("kein_wasser") else welt.p.durchfluss_lpm
        n = int(getattr(backend, "_n", 0))
        if backend is not self.backend or n < self.stand:
            self.backend, self.stand = backend, n
        liter = (n - self.stand) / float(getattr(backend, "ipl", 387) or 387)
        self.stand = n
        return liter, bool(getattr(backend, "offen", False))


def ausgaenge_lesen(cbpi, rollen, zaehler, welt):
    a = Ausgaenge(
        heizung=ausgang(cbpi, rollen.get("heizung")),
        kuehlwasser=ausgang(cbpi, rollen.get("kuehlwasser")),
        kuehlung=ausgang(cbpi, rollen.get("kuehlung")),
        gaertank_heizung=ausgang(cbpi, rollen.get("gaertank_heizung")),
        spund=ausgang(cbpi, rollen.get("spund")) > 0,
        co2=ausgang(cbpi, rollen.get("co2")) > 0,
    )
    liter, offen = zaehler.liter(cbpi, rollen.get("wasser"), welt)
    if liter is None:
        a.wasser_offen = ausgang(cbpi, rollen.get("wasser")) > 0
    else:
        a.wasser_l, a.wasser_offen = liter, offen
    return a


def eigene_url(cbpi):
    """cbpi ruft sich fuer Einrichtung und Pruefung ueber die eigene REST-Schnittstelle auf."""
    return f"http://127.0.0.1:{(getattr(cbpi, 'static_config', None) or {}).get('port', 8000)}"


def melden(cbpi, text, typ=NotificationType.INFO):
    log.info(text)
    cbpi.notify("Simulation", text, typ)


# --- Zentrale --------------------------------------------------------------------

ANLEGEN = "Beim Speichern anlegen"


@parameters(
    [Property.Select(ANLEGEN, options=["Nein", "Ja"],
                     description="Ja: Speichern legt Fühler, Aktoren, Kessel, Gärtank, Maisch- und Gärplan und "
                     "Dashboard 3 an, danach steht hier wieder Nein. Weitere Aktionen: Dashboard 3, Klick auf "
                     "„Störfälle“")] +
    # cbpi zeigt ueber Aktorfeldern fest "Actor" an, die Rolle steht deshalb vorn in der Beschreibung
    [Property.Actor(name, description=f"{name}: Aktor, der in der Simulation diese Rolle spielt")
     for name, _ in ROLLEN] +
    [Property.Number(name, configurable=True, default_value=round(getattr(Parameter(), feld) / faktor, 3),
                     description=beschr) for name, feld, faktor, beschr in PARAMETER]
)
class BrauSimulation(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        self.value = 0

    async def start(self):
        await super().start()
        if self.props.get(ANLEGEN) == "Ja":
            # erst nach dem Speichern loslegen: das Anlegen ruft cbpi per REST auf
            asyncio.create_task(self._anlegen_nach_speichern())

    async def _anlegen_nach_speichern(self):
        await asyncio.sleep(1)
        await self.anlegen()

    async def run(self):
        while self.running:
            self.value = len(WELT.z.stoerungen)
            self.push_update(self.value)
            await asyncio.sleep(1)

    def get_state(self):
        return dict(value=self.value)

    def get_unit(self):
        return "Störfälle"

    @action("Simulation anlegen", parameters=[
        Property.Select("Maisch- und Gärplan schreiben", options=["Ja", "Nein"],
                        description="Ja ersetzt den Maischplan und den Gärplan des Gärtanks"),
        Property.Number("Dashboard", configurable=True, default_value=3, description="Nummer 1-4, wird überschrieben"),
    ])
    async def anlegen(self, **kwargs):
        """Legt Fuehler, Aktoren, Sudkessel, Gaertank, Plaene und Dashboard an; Zeitraffer
        aus den eigenen Einstellungen. Laeuft im Thread, weil es cbpi per REST aufruft."""
        p = parameter_aus(self.props)
        plaene = kwargs.get("Maisch- und Gärplan schreiben", "Ja") != "Nein"
        nr = max(1, min(4, int(_zahl(kwargs, "Dashboard", 3))))
        zeilen = []
        melden(self.cbpi, "Simulation wird angelegt …")
        try:
            await asyncio.to_thread(einrichtung.einrichten, eigene_url(self.cbpi), p.zeitraffer_brauen,
                                    p.zeitraffer_gaerung, nr, plaene, self.id, zeilen.append, {ANLEGEN: "Nein"})
        except Exception as e:
            log.error("Simulation anlegen: %s\n%s", e, "\n".join(zeilen))
            melden(self.cbpi, f"Anlegen fehlgeschlagen: {e}", NotificationType.ERROR)
            return
        log.info("Simulation angelegt:\n%s", "\n".join(zeilen))
        neu = sum(z.strip().startswith("angelegt") for z in zeilen)
        for z in zeilen:
            if z.startswith("HINWEIS"):
                melden(self.cbpi, z, NotificationType.WARNING)
        melden(self.cbpi, f"Simulation angelegt ({neu} neu, Rest vorhanden). Dashboard {nr} wählen, "
               "dann Brauen → Start." + (" Maischplan „Sim-Sud“ und Gärplan geschrieben." if plaene else ""),
               NotificationType.SUCCESS)

    @action("Störfälle prüfen", parameters=[])
    async def stoerfaelle_pruefen(self, **kwargs):
        """Loest jeden Stoerfall aus und prueft die Reaktion der echten Plugins (etwa 5 min).
        Nicht waehrend eines Brautags: Kessel, Gaertank und Simulation werden benutzt."""
        logdatei = Path(getattr(self.cbpi.config_folder, "logsFolderPath", "") or ".") / "cbpi.log"
        melden(self.cbpi, "Störfallprüfung läuft (etwa 5 Minuten) …")
        schleife = asyncio.get_running_loop()

        def zeile(text):  # kommt aus dem Pruef-Thread
            typ = NotificationType.ERROR if text.startswith("FEHLER") else (
                NotificationType.SUCCESS if text.startswith("OK") else NotificationType.INFO)
            log.info("Störfallprüfung: %s", text)  # info: die Pruefung sucht selbst im Log
            schleife.call_soon_threadsafe(self.cbpi.notify, "Störfallprüfung", text, typ)
        try:
            await asyncio.to_thread(stoerfaelle.pruefen, eigene_url(self.cbpi),
                                    str(logdatei) if logdatei.exists() else None, zeile)
        except Exception as e:
            log.exception("Störfallprüfung")
            melden(self.cbpi, f"Störfallprüfung abgebrochen: {e}", NotificationType.ERROR)

    @action("Störfall auslösen", parameters=[Property.Select("Störfall", options=list(STOERFALL_AUSWAHL))])
    async def stoerfall_ausloesen(self, **kwargs):
        name = kwargs.get("Störfall")
        if name not in STOERFALL_AUSWAHL:
            melden(self.cbpi, f"Unbekannter Störfall: {name}", NotificationType.ERROR)
            return
        schluessel, erwartet = STOERFALL_AUSWAHL[name]
        WELT.stoerung(schluessel, True)
        melden(self.cbpi, f"Störfall „{name}“ ausgelöst. Erwartet: {erwartet}", NotificationType.WARNING)

    @action("Störfälle aufheben", parameters=[])
    async def stoerfaelle_aufheben(self, **kwargs):
        for s in list(WELT.z.stoerungen):
            WELT.stoerung(s, False)
        melden(self.cbpi, "Alle Störfälle aufgehoben (CO₂-Flasche voll)", NotificationType.SUCCESS)

    @action("Wasser einfüllen", parameters=[Property.Number("Liter", configurable=True, default_value=28)])
    async def wasser(self, **kwargs):
        liter = _zahl(kwargs, "Liter", 28)
        WELT.wasser_einfuellen(liter)
        melden(self.cbpi, f"{liter:g} L Wasser im Kessel")

    @action("Einmaischen", parameters=[])
    async def einmaischen(self, **kwargs):
        WELT.einmaischen()
        melden(self.cbpi, f"{WELT.p.schuettung_kg:g} kg Malz eingemaischt, Maische {WELT.z.k_temp:.1f} °C")

    @action("Treber ziehen", parameters=[])
    async def treber(self, **kwargs):
        WELT.treber_ziehen()
        melden(self.cbpi, f"Treber gezogen, {WELT.z.k_vol_l:.1f} L Würze")

    @action("Anstellen (Würze in den Gärtank)", parameters=[])
    async def anstellen(self, **kwargs):
        WELT.anstellen()
        melden(self.cbpi, anstell_text())

    @action("Alles zurücksetzen", parameters=[])
    async def zuruecksetzen(self, **kwargs):
        WELT.zuruecksetzen()
        melden(self.cbpi, "Simulation zurückgesetzt: Kessel und Gärtank leer", NotificationType.WARNING)


def anstell_text():
    z = WELT.z
    return (f"Angestellt: {z.g_bier_l:.1f} L, Stammwürze {z.g_og_p:.1f} °P "
            f"(SG {modell.plato_zu_sg(z.g_og_p):.3f}), {z.g_temp:.1f} °C")


# --- Fuehler ---------------------------------------------------------------------

@parameters([
    Property.Select("Messgröße", options=list(MESSGROESSEN), description="Was dieser Fühler aus der Simulation misst"),
    Property.Number("Max Alter s", configurable=True, default_value=10,
                    description="Ohne gültigen Wert so lange -> Meldung (wie SicherFuehler)"),
])
class SimFuehler(_FuehlerBasis):

    def __init__(self, cbpi, id, props):
        CBPiSensor.__init__(self, cbpi, id, props)
        # Immer eine Zahl: cbpi-Teile vergleichen Werte ungeprueft
        self.value = 0.0
        self.zeit = None
        self.gemeldet = False
        self.fehler = ""

    async def start(self):
        await CBPiSensor.start(self)

    def groesse(self):
        g = self.props.get("Messgröße")
        return g if g in MESSGROESSEN else "Kesseltemperatur"

    def alter(self):
        """Sekunden seit dem letzten gueltigen Wert, None ohne Wert."""
        return None if self.zeit is None else time.monotonic() - self.zeit

    def _name(self):
        item = self.cbpi.sensor.find_by_id(self.id)
        return getattr(item, "name", None) or self.id

    def messen(self):
        """Ein Durchlauf; True, wenn ein gueltiger Wert kam."""
        g = self.groesse()
        if WELT.ausgefallen(g):
            self.fehler = "Störfall Fühlerausfall"
            return False
        self.fehler = ""
        self.value = WELT.messwert(g)
        self.zeit = time.monotonic()
        return True

    async def run(self):
        start = time.monotonic()
        while self.running:
            if self.messen():
                self.push_update(self.value)
                self.log_data(self.value)
                if self.gemeldet:
                    self.gemeldet = False
                    self.cbpi.notify("Fühler", f"{self._name()}: liefert wieder ({self.value})",
                                     NotificationType.SUCCESS)
            else:
                alter = self.alter() if self.zeit is not None else time.monotonic() - start
                if alter > _zahl(self.props, "Max Alter s", 10) and not self.gemeldet:
                    self.gemeldet = True
                    log.error("SimFuehler %s: seit %.0f s kein gültiger Wert", self._name(), alter)
                    self.cbpi.notify("Fühler", f"{self._name()}: seit {alter:.0f} s kein gültiger Wert "
                                     f"({self.fehler})", NotificationType.ERROR)
            await asyncio.sleep(1)

    @action("Störfall: Fühlerausfall an/aus", parameters=[])
    async def ausfall_umschalten(self, **kwargs):
        schluessel = "ausfall:" + self.groesse()
        an = not WELT.aktiv(schluessel)
        WELT.stoerung(schluessel, an)
        melden(self.cbpi, f"{self._name()}: Fühlerausfall {'ausgelöst' if an else 'aufgehoben'}",
               NotificationType.WARNING if an else NotificationType.SUCCESS)

    def get_state(self):
        return dict(value=self.value)

    def get_unit(self):
        return MESSGROESSEN[self.groesse()]


# --- Aktor -----------------------------------------------------------------------

@parameters([])
class SimAktor(CBPiActor):
    """Ohne Hardware und ohne Schutz; fuer Rollen, die kein echtes Plugin hat."""

    async def start(self):
        await super().start()
        self.state = False
        self.power = 100

    async def on(self, power=None, output=None):
        if power is not None:
            self.power = int(power)
        self.state = True

    async def off(self):
        self.state = False

    async def set_power(self, power):
        self.power = max(0, min(100, int(power)))
        await self.cbpi.actor.actor_update(self.id, self.power)

    def get_state(self):
        return self.state  # bool wie GPIOActor, sonst zeigt die Oberflaeche immer "an"

    async def run(self):
        while self.running:
            await asyncio.sleep(1)


# --- Schritt ---------------------------------------------------------------------

EREIGNISSE = ["Wasser einfüllen", "Einmaischen", "Treber ziehen", "Anstellen"]


@parameters([
    Property.Select("Ereignis", options=EREIGNISSE, description="Was der Brauer jetzt tut"),
    Property.Number("Liter", configurable=True, default_value=28, description="nur für Wasser einfüllen"),
    Property.Fermenter("Gärtank", description="nur für Anstellen"),
    Property.Select("Gärplan starten", options=["Ja", "Nein"], description="Anstellen: Gärplan des Gärtanks starten"),
])
class SimEreignis(CBPiStep):

    async def run(self):
        ereignis = self.props.get("Ereignis", "Einmaischen")
        try:
            if ereignis == "Wasser einfüllen":
                WELT.wasser_einfuellen(_zahl(self.props, "Liter", 28))
                self.summary = f"{WELT.z.k_vol_l:.1f} L im Kessel"
            elif ereignis == "Einmaischen":
                WELT.einmaischen()
                self.summary = f"eingemaischt, {WELT.z.k_temp:.1f} °C"
            elif ereignis == "Treber ziehen":
                WELT.treber_ziehen()
                self.summary = f"{WELT.z.k_vol_l:.1f} L Würze"
            elif ereignis == "Anstellen":
                WELT.anstellen()
                self.summary = anstell_text()
                gaertank = self.props.get("Gärtank")
                if gaertank and self.props.get("Gärplan starten", "Ja") == "Ja":
                    await self.cbpi.fermenter.start(gaertank)
                    self.summary += ", Gärplan läuft"
            melden(self.cbpi, f"{self.name}: {self.summary}", NotificationType.SUCCESS)
            ok = True
        except Exception as e:
            log.exception("SimEreignis")
            self.summary = f"Fehler: {e}"
            self.cbpi.notify(self.name, self.summary, NotificationType.ERROR)
            ok = False
        await self.push_update()
        if ok:
            # cbpi schaltet nur bei NEXT weiter; next() bricht diesen Task ab und
            # muss deshalb aus einem eigenen Task kommen
            asyncio.create_task(self.next())
        while self.running:
            await asyncio.sleep(1)


# --- Takt --------------------------------------------------------------------------

class SimulationsTakt(CBPiExtension):
    """Rechnet das Modell fort, solange cbpi laeuft, und sichert den Zustand
    (simulation_zustand.json im Konfigurationsordner), damit ein Neustart
    von cbpi den Gaertank nicht leert."""

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.pfad = cbpi.config_folder.get_file_path(DATEI)
        self.zaehler = Wasserzaehler()
        self._task = None
        cbpi.register_on_startup(self)

    @on_startup("simulation_takt", order=10)
    async def beim_start(self):
        try:
            with open(self.pfad) as f:
                WELT.laden(json.load(f))
            log.info("Simulationsstand geladen")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("Simulationsstand nicht lesbar, beginne leer: %s", e)
        self._task = asyncio.create_task(self.takt())

    def einmal(self, dt):
        item = zentrale(self.cbpi)
        props = getattr(item, "props", None) or {}
        WELT.p = parameter_aus(props)
        rollen = {feld: props.get(name) for name, feld in ROLLEN}
        WELT.schritt(dt, ausgaenge_lesen(self.cbpi, rollen, self.zaehler, WELT))

    def sichern(self):
        tmp = self.pfad + ".neu"
        with open(tmp, "w") as f:
            json.dump(WELT.zustand(), f)
        os.replace(tmp, self.pfad)

    async def takt(self):
        letzte = time.monotonic()
        gesichert = letzte
        while True:
            await asyncio.sleep(TAKT_S)
            jetzt = time.monotonic()
            try:
                self.einmal(min(jetzt - letzte, 1.0))  # nach einem Haenger nicht nachholen
                if jetzt - gesichert >= SICHERN_S:
                    gesichert = jetzt
                    self.sichern()
            except Exception:
                log.exception("Simulationstakt")
            letzte = jetzt


def setup(cbpi):
    cbpi.plugin.register("BrauSimulation", BrauSimulation)
    cbpi.plugin.register("SimFuehler", SimFuehler)
    cbpi.plugin.register("SimAktor", SimAktor)
    cbpi.plugin.register("SimEreignis", SimEreignis)
    cbpi.plugin.register("SimulationsTakt", SimulationsTakt)
