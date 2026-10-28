"""Physikalisches Modell des Brautags: Kessel, Wuerzekuehler, Gaertank mit
Kuehlschrank, Gaerung, CO2 im Kopfraum und in der Leitung zum Gaspfosten.

Ohne cbpi, damit es sich testen laesst. Zwei Zeitmassstaebe:

* Langsame Vorgaenge (Waerme, Verdampfen, Maische, Gaerung, CO2 loest sich
  im Bier) laufen im Zeitraffer (`zeitraffer_brauen`, `zeitraffer_gaerung`).
* Schnelle Vorgaenge (Wasser durch das Ventil, Gas durch Spund- und
  CO2-Ventil) laufen in Echtzeit. Die echten Plugins messen Durchfluss und
  Ventilpulse in Echtzeit; im Zeitraffer wuerde ein 0,3-s-Puls zu Minuten.
"""
import math
import random
from dataclasses import asdict, dataclass, field, fields

CP_WASSER = 4186.0  # J/(kg K)
CP_MALZ = 1700.0
VERDAMPFUNG = 2.26e6  # J/kg
R = 0.08314  # L bar / (mol K)
M_CO2 = 44.01  # g/mol
P_ATM = 1.013  # bar absolut
KELVIN = 273.15
LEITUNG_L = 0.05  # 8-mm-Leitung Gaspfosten -> T -> Ventile
# CO2 am kritischen Punkt und Koeffizienten der Dampfdruckkurve (Span/Wagner 1996).
# Ueber 31,0 C gibt es keine Fluessigkeit mehr, dort endet die Kurve.
CO2_TK_K = 304.1282
CO2_PK_BAR = 73.773
CO2_DAMPF_A = (-7.0602087, 1.9391218, -1.6463597, -3.2995634)
# Leitwerte der Ventile in mol/(s bar), Normliter: Magnetventil mit etwa 2,5 mm Bohrung
SPUND_LEITWERT = 1.5 / 22.4
CO2_LEITWERT = 1.0 / 22.4
PRV_LEITWERT = 5.0 / 22.4
VERSTOPFT_LEITWERT = 0.002 / 22.4  # Kraeusen im Gaspfosten: fast dicht
KESSEL_TRAEGHEIT_S = 20.0  # Fuehler im Tauchrohr
KESSEL_TROCKEN_J_K = 8000.0  # Topf und Heizstab ohne Wasser darueber
GAERTANK_J_K = 2000.0
KUEHLSCHRANK_J_K = 15000.0  # Luft, Waende, Verdampfer
BIER_LUFT_W_K = 8.0
VERDAMPFER_MIN_C = -5.0
TRUB_L = 1.0  # bleibt beim Anstellen im Kessel

# Stoerfaelle: Schluessel -> Anzeigename
STOERFAELLE = {
    "co2_leer": "CO₂-Flasche leer",
    "gaspfosten_zu": "Gaspfosten verstopft",
    "kessel_trocken": "Übertemperatur (Kessel trocken)",
    "heizung_klebt": "Heizungsrelais klebt",
    "kuehlung_defekt": "Kühlung defekt",
    "kein_wasser": "Kein Wasser (Hahn zu)",
}

# Messgroessen: Name -> Einheit
MESSGROESSEN = {
    "Kesseltemperatur": "°C",     # Maischepfanne
    "Kesselvolumen": "L",
    "Würze Kessel": "SG",
    "Kochertemperatur": "°C",     # Wuerzekocher
    "Kochervolumen": "L",
    "Würze Kocher": "SG",
    "Durchfluss": "L/min",
    "Heizleistung": "W",
    "Wassermenge": "L",
    "Biertemperatur": "°C",
    "Kühlschrank": "°C",
    "Tankdruck": "bar",
    "CO₂-Vordruck": "bar",
    "Dichte": "SG",
    "CO₂-Flasche": "bar",
    "Gärtage": "d",
}
RAUSCHEN = {"Kesseltemperatur": 0.05, "Kochertemperatur": 0.05, "Biertemperatur": 0.03, "Kühlschrank": 0.1, "Tankdruck": 0.003,
            "CO₂-Vordruck": 0.005, "Dichte": 0.0002, "CO₂-Flasche": 0.05}
NACHKOMMA = {"SG": 4, "bar": 3, "g": 0, "L": 2, "L/min": 2, "d": 2, "°C": 2}


def co2_dampfdruck_bar(t_c):
    """Dampfdruck von CO2 ueber der Fluessigkeit, absolut in bar.

    Solange fluessiges CO2 in der Flasche ist, steht genau dieser Druck an --
    unabhaengig davon, wie viel noch drin ist. Ueber dem kritischen Punkt
    (31,0 C) gibt es keine Fluessigkeit mehr, dort gilt der kritische Druck."""
    t_k = t_c + KELVIN
    if t_k >= CO2_TK_K:
        return CO2_PK_BAR
    if t_k <= 0:
        return 0.0
    tau = 1.0 - t_k / CO2_TK_K
    a1, a2, a3, a4 = CO2_DAMPF_A
    s = a1 * tau + a2 * tau ** 1.5 + a3 * tau ** 2 + a4 * tau ** 4
    return CO2_PK_BAR * math.exp(CO2_TK_K / t_k * s)


@dataclass
class Parameter:
    zeitraffer_brauen: float = 60.0
    zeitraffer_gaerung: float = 120.0
    raum_c: float = 20.0
    leitungswasser_c: float = 12.0
    durchfluss_lpm: float = 12.0
    heizleistung_w: float = 3500.0
    kessel_verlust_w_k: float = 6.0
    kuehler_w_k: float = 120.0
    kuehlwasser_c: float = 12.0
    siedepunkt_c: float = 100.0
    schuettung_kg: float = 5.5
    ausbeute: float = 0.72  # kg Extrakt je kg Malz
    treber_l_kg: float = 0.8  # Wasser, das im Treber bleibt
    gaertank_l: float = 35.0  # FermZilla 30 L All Rounder, Gesamtvolumen
    wuerze_l: float = 23.0  # fuer "Gärtank füllen" ohne Brautag
    stammwuerze_p: float = 12.0
    vergaerungsgrad: float = 0.78
    gaerrate: float = 1.0
    kaelteleistung_w: float = 120.0
    kuehlschrank_verlust_w_k: float = 1.5
    gaertank_heizung_w: float = 40.0
    co2_vordruck_bar: float = 1.5  # Druckminderer
    co2_flasche_g: float = 2000.0
    co2_flasche_l: float = 2.7  # Innenvolumen einer 2-kg-Flasche
    pumpe_lpm: float = 12.0     # Foerderleistung der Braupumpe
    ueberdruckventil_bar: float = 2.4  # mechanisches PRV am Deckel
    rauschen: float = 1.0  # 0 = ohne Messrauschen


@dataclass
class Ausgaenge:
    """Was die Aktoren gerade tun, 0..1 (Takt und Sperren schon eingerechnet)."""
    heizung: float = 0.0          # Maischepfanne
    heizung_kocher: float = 0.0   # Wuerzekocher
    pumpe: bool = False           # Relais J16 / GPIO 22
    weg_maische: bool = False     # Wegeventil zurueck in den Maischebottich
    weg_kocher: bool = False      # Wegeventil in den Wuerzekocher
    kuehlwasser: float = 0.0
    kuehlung: float = 0.0
    gaertank_heizung: float = 0.0
    spund: bool = False
    co2: bool = False
    wasser_offen: bool = False
    wasser_l: float = None  # gezaehlte Liter seit dem letzten Schritt (VentilDosierer); None = selbst rechnen


def plato_zu_sg(p):
    return 1.0 + p / (258.6 - (p / 258.2) * 227.1)


def co2_loeslichkeit(temp_c):
    """g CO2 je L Bier und bar CO2-Partialdruck (Henry), 0 °C 3,35 / 20 °C 1,7."""
    return 3.35 * math.exp(-0.0336 * temp_c)


def gaer_faktor(temp_c):
    """Hefeaktivitaet relativ zu 18 °C; unter 4 °C steht die Gaerung."""
    return max(0.0, min(1.6, (temp_c - 4.0) / 14.0))


@dataclass
class Zustand:
    # Kessel
    k_vol_l: float = 0.0
    k_temp: float = 20.0
    k_fuehler: float = 20.0
    k_malz_kg: float = 0.0
    k_extrakt_pot_kg: float = 0.0  # noch zu loesen
    k_extrakt_kg: float = 0.0  # geloest
    # Wuerzekocher (zweites Gefaess; beim BIAB bleibt er leer)
    kk_vol_l: float = 0.0
    kk_temp: float = 20.0
    kk_fuehler: float = 20.0
    kk_extrakt_kg: float = 0.0
    wasser_gesamt_l: float = 0.0
    durchfluss_lpm: float = 0.0
    heizleistung_w: float = 0.0  # was gerade wirklich heizt
    # Gaertank
    g_bier_l: float = 0.0
    g_temp: float = 20.0
    g_luft: float = 20.0
    g_og_p: float = 0.0
    g_rest: float = 1.0  # vergaerbarer Rest 1..0
    g_hefe: float = 0.0  # Aktivitaet 0..1
    g_co2_gl: float = 0.0  # geloest, g/L
    g_kopf_mol: float = 0.0
    ltg_mol: float = 0.0
    g_flasche_g: float = 2000.0
    g_tage: float = 0.0
    angestellt: bool = False
    stoerungen: list = field(default_factory=list)


class Welt:
    def __init__(self, parameter=None, zufall=None):
        self.p = parameter or Parameter()
        self.z = Zustand(k_temp=self.p.raum_c, k_fuehler=self.p.raum_c,
                         kk_temp=self.p.raum_c, kk_fuehler=self.p.raum_c, g_temp=self.p.raum_c,
                         g_luft=self.p.raum_c, g_flasche_g=self.p.co2_flasche_g)
        self.zufall = zufall or random.Random()
        self._kopf_leer()

    # --- Zustand sichern ---------------------------------------------------------

    def zustand(self):
        return asdict(self.z)

    def laden(self, daten):
        bekannt = {f.name for f in fields(Zustand)}
        self.z = Zustand(**{k: v for k, v in (daten or {}).items() if k in bekannt})

    def zuruecksetzen(self):
        self.__init__(self.p, self.zufall)

    # --- Stoerfaelle -------------------------------------------------------------

    def stoerung(self, name, an=True):
        aktiv = set(self.z.stoerungen)
        if an and name not in aktiv:
            if name == "gaspfosten_zu":
                self.ltg_mol_aus_kopf()
            if name == "co2_leer":
                self.z.g_flasche_g = 0.0
            aktiv.add(name)
        elif not an and name in aktiv:
            aktiv.discard(name)
            if name == "gaspfosten_zu":
                self.z.g_kopf_mol += self.z.ltg_mol  # Leitung und Kopfraum wieder eins
                self.z.ltg_mol = 0.0
            if name == "co2_leer":
                self.z.g_flasche_g = self.p.co2_flasche_g  # Flasche getauscht
        self.z.stoerungen = sorted(aktiv)

    def aktiv(self, name):
        return name in self.z.stoerungen

    def ausgefallen(self, messgroesse):
        return self.aktiv("ausfall:" + messgroesse)

    # --- Ereignisse aus dem Brauablauf --------------------------------------------

    def wasser_einfuellen(self, liter, temp_c=None):
        self._mischen(liter, self.p.leitungswasser_c if temp_c is None else temp_c)
        self.z.wasser_gesamt_l += liter

    def einmaischen(self, kg=None):
        z, kg = self.z, self.p.schuettung_kg if kg is None else kg
        c = self._kessel_kap()
        z.k_temp = (c * z.k_temp + kg * CP_MALZ * self.p.raum_c) / (c + kg * CP_MALZ)
        z.k_malz_kg += kg
        z.k_extrakt_pot_kg += kg * self.p.ausbeute

    def treber_ziehen(self):
        z = self.z
        if z.k_malz_kg <= 0:
            return
        weg = min(z.k_vol_l, z.k_malz_kg * self.p.treber_l_kg)
        if z.k_vol_l > 0:
            z.k_extrakt_kg *= (z.k_vol_l - weg) / z.k_vol_l
        z.k_vol_l -= weg
        z.k_malz_kg = 0.0
        z.k_extrakt_pot_kg = 0.0

    def kessel_leeren(self):
        """Maischepfanne und Wuerzekocher leeren -- Gaertank und Stoerfaelle bleiben.

        Fuer den naechsten Versuch am Brautag, ohne die laufende Gaerung
        wegzuwerfen. Inhalt, Malz und Extrakt gehen auf null, der Kessel nimmt
        Raumtemperatur an; der Wasserzaehler faengt wieder bei null an, weil er
        zaehlt, was in diesen Sud gelaufen ist."""
        z = self.z
        z.k_vol_l = 0.0
        z.k_malz_kg = 0.0
        z.k_extrakt_pot_kg = 0.0
        z.k_extrakt_kg = 0.0
        z.k_temp = z.k_fuehler = self.p.raum_c
        z.kk_vol_l = 0.0
        z.kk_extrakt_kg = 0.0
        z.kk_temp = z.kk_fuehler = self.p.raum_c
        z.wasser_gesamt_l = 0.0
        z.durchfluss_lpm = 0.0
        z.heizleistung_w = 0.0

    def umpumpen(self):
        """Wuerze aus der Maischepfanne in den Wuerzekocher.

        Zwei-Geraete-Aufbau: Nach dem Abmaischen und Treberziehen wandert alles
        hinueber. Beim BIAB bleibt der Schritt aus, dann kocht die Pfanne selbst."""
        z = self.z
        if z.k_vol_l <= 0:
            return
        c_alt = (z.kk_vol_l + z.kk_extrakt_kg) * CP_WASSER + 3000.0
        c_neu = (z.k_vol_l + z.k_extrakt_kg) * CP_WASSER
        z.kk_temp = (c_alt * z.kk_temp + c_neu * z.k_temp) / (c_alt + c_neu)
        z.kk_vol_l += z.k_vol_l
        z.kk_extrakt_kg += z.k_extrakt_kg
        z.k_vol_l = 0.0
        z.k_extrakt_kg = 0.0
        z.k_extrakt_pot_kg = 0.0
        z.k_malz_kg = 0.0

    def anstellen(self):
        """Wuerze vom Kessel in den Gaertank, Hefe dazu. Ohne Wuerze im Kessel
        (kein Brautag) mit den Vorgaben `wuerze_l` und `stammwuerze_p`."""
        z, p = self.z, self.p
        if z.kk_vol_l - TRUB_L >= 5:        # zwei Geraete: die Wuerze steht im Kocher
            menge, plato, temp = z.kk_vol_l - TRUB_L, self.kocher_plato(), z.kk_temp
        elif z.k_vol_l - TRUB_L >= 5:       # BIAB: alles in der Pfanne
            menge, plato, temp = z.k_vol_l - TRUB_L, self.kessel_plato(), z.k_temp
        else:
            menge, plato, temp = p.wuerze_l, p.stammwuerze_p, z.g_temp
        z.g_bier_l = min(menge, p.gaertank_l - 2.0)
        z.g_temp, z.g_og_p = temp, plato
        z.g_rest, z.g_hefe, z.g_co2_gl, z.g_tage = 1.0, 0.01, 0.0, 0.0
        z.angestellt = True
        z.k_vol_l, z.k_extrakt_kg, z.k_extrakt_pot_kg, z.k_malz_kg = 0.0, 0.0, 0.0, 0.0
        z.kk_vol_l, z.kk_extrakt_kg = 0.0, 0.0
        self._kopf_leer()

    # --- Abgeleitete Groessen ------------------------------------------------------

    def kocher_plato(self):
        z = self.z
        masse = z.kk_vol_l + z.kk_extrakt_kg
        return 100.0 * z.kk_extrakt_kg / masse if masse > 0 else 0.0

    def kessel_plato(self):
        z = self.z
        masse = z.k_vol_l + z.k_extrakt_kg
        return 100.0 * z.k_extrakt_kg / masse if masse > 0 else 0.0

    def plato_ende(self):
        return self.z.g_og_p * (1.0 - self.p.vergaerungsgrad)

    def bier_plato(self):
        return self.plato_ende() + (self.z.g_og_p - self.plato_ende()) * self.z.g_rest

    def co2_gesamt_gl(self):
        """CO2 aus der ganzen Gaerung in g/L (Alkohol nach (OG-FG)*131,25)."""
        abv = (plato_zu_sg(self.z.g_og_p) - plato_zu_sg(self.plato_ende())) * 131.25
        return abv * 7.89 * 0.955

    def kopf_l(self):
        return max(0.5, self.p.gaertank_l - self.z.g_bier_l)

    def _t_gas(self):
        return self.z.g_temp + KELVIN

    def kopf_bar_abs(self):
        return self.z.g_kopf_mol * R * self._t_gas() / self.kopf_l()

    def leitung_bar_abs(self):
        if self.aktiv("gaspfosten_zu"):
            return self.z.ltg_mol * R * self._t_gas() / LEITUNG_L
        return self.kopf_bar_abs()

    def vordruck_bar(self):
        return self.p.co2_vordruck_bar if self.z.g_flasche_g > 0 else 0.0

    def flaschendruck_bar(self):
        """Was das Manometer vor dem Druckminderer zeigt (Ueberdruck in bar).

        Solange fluessiges CO2 da ist, steht der Dampfdruck an. Der haengt nur
        an der Temperatur, nicht an der Menge -- eine fast volle und eine fast
        leere Flasche zeigen dasselbe. Erst wenn die letzte Fluessigkeit
        verdampft ist, faellt der Druck mit der Restmenge (ideales Gas).
        Der Geber ist also eine Warnung "gleich leer", keine Fuellstandsanzeige."""
        mol = self.z.g_flasche_g / M_CO2
        if mol <= 0:
            return 0.0
        t_c = self.p.raum_c
        # Druck, wenn die ganze Menge gasfoermig waere. Liegt er ueber dem
        # Dampfdruck, muss ein Teil fluessig sein -- dann gilt der Dampfdruck.
        p_gas = mol * R * (t_c + KELVIN) / max(self.p.co2_flasche_l, 0.1)
        return max(0.0, min(p_gas, co2_dampfdruck_bar(t_c)) - P_ATM)

    def ltg_mol_aus_kopf(self):
        """Beim Verstopfen behaelt die Leitung den Druck, den sie gerade hat."""
        z = self.z
        anteil = LEITUNG_L / (self.kopf_l() + LEITUNG_L)
        z.ltg_mol = z.g_kopf_mol * anteil
        z.g_kopf_mol -= z.ltg_mol

    def _kopf_leer(self):
        self.z.g_kopf_mol = P_ATM * self.kopf_l() / (R * self._t_gas())
        self.z.ltg_mol = 0.0

    def _kessel_kap(self):
        z = self.z
        if self.aktiv("kessel_trocken"):
            return KESSEL_TROCKEN_J_K
        return (z.k_vol_l + z.k_extrakt_kg) * CP_WASSER + z.k_malz_kg * CP_MALZ + 3000.0

    def _mischen(self, liter, temp_c):
        if liter <= 0:
            return
        c = self._kessel_kap()
        z = self.z
        z.k_temp = (c * z.k_temp + liter * CP_WASSER * temp_c) / (c + liter * CP_WASSER)
        z.k_vol_l += liter

    # --- Messwerte -------------------------------------------------------------------

    def messwert(self, name):
        z = self.z
        wert = {
            "Kesseltemperatur": z.k_fuehler,
            "Kesselvolumen": z.k_vol_l,
            "Würze Kessel": plato_zu_sg(self.kessel_plato()),
            "Kochertemperatur": z.kk_fuehler,
            "Kochervolumen": z.kk_vol_l,
            "Würze Kocher": plato_zu_sg(self.kocher_plato()),
            "Durchfluss": z.durchfluss_lpm,
            "Heizleistung": z.heizleistung_w,
            "Wassermenge": z.wasser_gesamt_l,
            "Biertemperatur": z.g_temp,
            "Kühlschrank": z.g_luft,
            "Tankdruck": max(0.0, self.leitung_bar_abs() - P_ATM),
            "CO₂-Vordruck": self.vordruck_bar(),
            "Dichte": plato_zu_sg(self.bier_plato()) if z.angestellt else 1.0,
            "CO₂-Flasche": self.flaschendruck_bar(),
            "Gärtage": z.g_tage,
        }[name]
        streuung = RAUSCHEN.get(name, 0.0) * self.p.rauschen
        if streuung:
            wert += self.zufall.gauss(0.0, streuung)
        return round(wert, NACHKOMMA.get(MESSGROESSEN[name], 2))

    # --- Zeitschritt -------------------------------------------------------------------

    def schritt(self, dt_real, a=None):
        """Echtzeit `dt_real` (s) fortschreiben. Ausgaenge `a` aus cbpi."""
        a = a or Ausgaenge()
        if dt_real <= 0:
            return
        self._wasser(dt_real, a)
        self._pumpe(dt_real, a)
        self._gaswege(dt_real, a)
        for teil, faktor in ((self._kessel, self.p.zeitraffer_brauen),
                             (self._kocher, self.p.zeitraffer_brauen),
                             (self._gaertank, self.p.zeitraffer_gaerung)):
            rest = dt_real * max(faktor, 0.0)
            while rest > 1e-9:  # Teilschritte hoechstens 5 s Simulationszeit
                dt = min(rest, 5.0)
                teil(dt, a)
                rest -= dt

    def _pumpe(self, dt, a):
        """Pumpe und die zwei Wegeventile. Echtzeit, nicht im Zeitraffer -- wie
        Wasser und Gas, weil hier Mengen in Minuten fliessen, nicht in Stunden.

        Ventil Richtung Kocher offen: die Wuerze wandert hinueber.
        Ventil Richtung Maischebottich offen: sie laeuft im Kreis (Laeutern),
        die Mengen aendern sich nicht.
        Beide zu: die Pumpe foerdert gegen geschlossene Ventile, nichts fliesst."""
        z = self.z
        if not a.pumpe or not a.weg_kocher or z.k_vol_l <= 0:
            return
        menge = min(z.k_vol_l, self.p.pumpe_lpm / 60.0 * dt)
        anteil = menge / z.k_vol_l
        extrakt = z.k_extrakt_kg * anteil
        c_alt = (z.kk_vol_l + z.kk_extrakt_kg) * CP_WASSER + 3000.0
        c_neu = (menge + extrakt) * CP_WASSER
        if c_alt + c_neu > 0:
            z.kk_temp = (c_alt * z.kk_temp + c_neu * z.k_temp) / (c_alt + c_neu)
        z.kk_vol_l += menge
        z.kk_extrakt_kg += extrakt
        z.k_vol_l -= menge
        z.k_extrakt_kg -= extrakt

    def _wasser(self, dt, a):
        z = self.z
        if a.wasser_l is not None:
            liter = max(0.0, a.wasser_l)
            lpm = liter / dt * 60.0 if a.wasser_offen else 0.0
            # gezaehlte Impulse je Takt springen: wie ein Durchflussmesser ueber 1 s mitteln
            z.durchfluss_lpm = lpm if not a.wasser_offen else z.durchfluss_lpm + (lpm - z.durchfluss_lpm) * min(1.0, dt)
        else:
            lpm = self.p.durchfluss_lpm if (a.wasser_offen and not self.aktiv("kein_wasser")) else 0.0
            liter, z.durchfluss_lpm = lpm / 60.0 * dt, lpm
        if liter > 0:
            self.wasser_einfuellen(liter)

    def _gaswege(self, dt, a):
        """Echtzeit: Ventile an der Leitung, Ueberdruckventil am Deckel.
        Exakte Loesung der linearen Gleichung, damit auch die kleine Leitung
        (verstopfter Pfosten) bei grossen Schritten stabil bleibt."""
        z = self.z
        verstopft = self.aktiv("gaspfosten_zu")
        t = self._t_gas()
        vol = LEITUNG_L if verstopft else self.kopf_l() + LEITUNG_L
        n = z.ltg_mol if verstopft else z.g_kopf_mol
        p = n * R * t / vol
        vordruck = self.vordruck_bar() + P_ATM
        c_spund = SPUND_LEITWERT if a.spund else 0.0
        c_co2 = CO2_LEITWERT if (a.co2 and vordruck > p and z.g_flasche_g > 0) else 0.0
        c_prv = PRV_LEITWERT if (not verstopft and p - P_ATM > self.p.ueberdruckventil_bar) else 0.0
        summe = c_spund + c_co2 + c_prv
        if summe > 0:
            ziel = (c_spund * P_ATM + c_co2 * vordruck + c_prv * (P_ATM + self.p.ueberdruckventil_bar)) / summe
            p_neu = ziel + (p - ziel) * math.exp(-summe * R * t / vol * dt)
            if c_co2:
                rein_mol = c_co2 * (vordruck - (p + p_neu) / 2) * dt
                z.g_flasche_g = max(0.0, z.g_flasche_g - rein_mol * M_CO2)
            n = p_neu * vol / (R * t)
        if verstopft:
            # Kraeusen lassen etwas durch
            p_tank = self.kopf_bar_abs()
            fluss = VERSTOPFT_LEITWERT * (p_tank - n * R * t / vol) * dt
            z.ltg_mol, z.g_kopf_mol = n + fluss, z.g_kopf_mol - fluss
            if p_tank - P_ATM > self.p.ueberdruckventil_bar:
                ziel = P_ATM + self.p.ueberdruckventil_bar
                z.g_kopf_mol = ziel * self.kopf_l() / (R * t) + \
                    (z.g_kopf_mol - ziel * self.kopf_l() / (R * t)) * math.exp(-PRV_LEITWERT * R * t / self.kopf_l() * dt)
        else:
            z.g_kopf_mol = n

    def _kessel(self, dt, a):
        z, p = self.z, self.p
        heiz = 1.0 if self.aktiv("heizung_klebt") else max(0.0, min(1.0, a.heizung))
        trocken = self.aktiv("kessel_trocken")
        c = self._kessel_kap()
        z.heizleistung_w = heiz * p.heizleistung_w
        leistung = heiz * p.heizleistung_w - p.kessel_verlust_w_k * (z.k_temp - p.raum_c)
        # (die Leistung des Kochers kommt in _kocher dazu)
        # Der Wuerzekuehler haengt am Gefaess mit der Wuerze: beim Zwei-Geraete-Aufbau
        # am Kocher, beim BIAB an der Pfanne.
        if a.kuehlwasser > 0 and z.k_vol_l > 0 and z.kk_vol_l <= 0:
            leistung -= a.kuehlwasser * p.kuehler_w_k * (z.k_temp - p.kuehlwasser_c)
        z.k_temp += leistung * dt / c
        if not trocken and z.k_vol_l > 0 and z.k_temp > p.siedepunkt_c:
            kg = (z.k_temp - p.siedepunkt_c) * c / VERDAMPFUNG
            z.k_vol_l = max(0.0, z.k_vol_l - kg)
            z.k_temp = p.siedepunkt_c
        # Maische: Enzyme loesen Extrakt zwischen 50 und 80 °C, am besten 62-72 °C
        if z.k_extrakt_pot_kg > 0 and 50.0 <= z.k_temp <= 80.0:
            rate = 1.0 / 1200.0 * (1.0 if 62.0 <= z.k_temp <= 72.0 else 0.4)
            menge = z.k_extrakt_pot_kg * (1.0 - math.exp(-rate * dt))
            z.k_extrakt_pot_kg -= menge
            z.k_extrakt_kg += menge
        z.k_fuehler += (z.k_temp - z.k_fuehler) * (1.0 - math.exp(-dt / KESSEL_TRAEGHEIT_S))

    def _kocher(self, dt, a):
        """Wuerzekocher: wie die Pfanne, aber ohne Maische. Heizt, kocht, verdampft
        und nimmt den Wuerzekuehler, sobald Wuerze drin steht."""
        z, p = self.z, self.p
        heiz = 1.0 if self.aktiv("heizung_klebt") else max(0.0, min(1.0, a.heizung_kocher))
        c = (z.kk_vol_l + z.kk_extrakt_kg) * CP_WASSER + 3000.0
        leistung = heiz * p.heizleistung_w - p.kessel_verlust_w_k * (z.kk_temp - p.raum_c)
        if a.kuehlwasser > 0 and z.kk_vol_l > 0:
            leistung -= a.kuehlwasser * p.kuehler_w_k * (z.kk_temp - p.kuehlwasser_c)
        z.kk_temp += leistung * dt / c
        if z.kk_vol_l > 0 and z.kk_temp > p.siedepunkt_c:
            kg = (z.kk_temp - p.siedepunkt_c) * c / VERDAMPFUNG
            z.kk_vol_l = max(0.0, z.kk_vol_l - kg)
            z.kk_temp = p.siedepunkt_c
        z.kk_fuehler += (z.kk_temp - z.kk_fuehler) * (1.0 - math.exp(-dt / KESSEL_TRAEGHEIT_S))
        z.heizleistung_w += heiz * p.heizleistung_w

    def _gaertank(self, dt, a):
        z, p = self.z, self.p
        std = dt / 3600.0
        # Gaerung: Hefe waechst logistisch, Zucker wird verbraucht
        erzeugt_gl = 0.0
        if z.angestellt and z.g_bier_l > 0:
            f = gaer_faktor(z.g_temp) * p.gaerrate
            z.g_hefe += 0.25 * f * z.g_hefe * (1.0 - z.g_hefe) * std
            verbrauch = min(z.g_rest, 0.03 * f * z.g_hefe * z.g_rest / (z.g_rest + 0.05) * std)
            z.g_rest -= verbrauch
            erzeugt_gl = verbrauch * self.co2_gesamt_gl()
            z.g_co2_gl += erzeugt_gl
            z.g_tage += dt / 86400.0
        # CO2 zwischen Bier und Kopfraum; aktive Gaerung durchmischt
        if z.g_bier_l > 0:
            p_abs = self.kopf_bar_abs()
            gleich = co2_loeslichkeit(z.g_temp) * p_abs
            # Ruhendes Bier saettigt unter Kopfdruck in etwa einer Woche (0,03/h).
            # Gaerblasen entstehen erst bei Uebersaettigung: Sie treiben CO2 aus,
            # nehmen aber keins aus dem Kopfraum auf
            kla = 0.03 + (3.0 * z.g_hefe * z.g_rest if z.g_co2_gl > gleich else 0.0)  # 1/h
            menge_gl = (z.g_co2_gl - gleich) * (1.0 - math.exp(-kla * std))
            z.g_co2_gl -= menge_gl
            z.g_kopf_mol = max(1e-6, z.g_kopf_mol + menge_gl * z.g_bier_l / M_CO2)
        # Waerme: Bier <-> Kuehlschrankluft <-> Raum
        kap_bier = z.g_bier_l * CP_WASSER + GAERTANK_J_K
        gaerwaerme = erzeugt_gl * z.g_bier_l * 1200.0 / dt if dt > 0 else 0.0  # 1,2 kJ je g CO2
        kaelte = 0.0 if self.aktiv("kuehlung_defekt") else max(0.0, min(1.0, a.kuehlung)) * p.kaelteleistung_w
        kaelte *= max(0.0, min(1.0, (z.g_luft - VERDAMPFER_MIN_C) / 10.0))
        heiz = max(0.0, min(1.0, a.gaertank_heizung)) * p.gaertank_heizung_w
        # Gas im Kopfraum folgt der Temperatur von selbst (p = nRT/V)
        zum_bier = BIER_LUFT_W_K * (z.g_luft - z.g_temp)
        z.g_temp += (zum_bier + gaerwaerme + heiz) * dt / kap_bier
        z.g_luft += (-zum_bier + p.kuehlschrank_verlust_w_k * (p.raum_c - z.g_luft) - kaelte) * dt / KUEHLSCHRANK_J_K
