"""Tests ohne Hardware:  pip install cbpi4 pytest && python -m pytest tests/ -q

Die Kopplung an die echten Plugins (brausicher, gaerdruck, ventil) wird
geprueft, wenn sie installiert sind oder als Repos daneben liegen
(~/Claude/cbpi4-brausicher usw.)."""
import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

HIER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HIER))
for nachbar in (HIER.parent / "cbpi4-brausicher", HIER.parent / "cbpi4-gaerdruck", HIER.parent / "cbpi4-ventil"):
    if nachbar.exists():
        sys.path.append(str(nachbar))

from cbpi4_simulation import modell as M  # noqa: E402
from cbpi4_simulation.modell import Ausgaenge, Parameter, Welt  # noqa: E402


def welt(**p):
    p.setdefault("rauschen", 0)
    p.setdefault("zeitraffer_brauen", 1)
    p.setdefault("zeitraffer_gaerung", 1)
    return Welt(Parameter(**p))


def laufen(w, sekunden, a=None, dt=1.0, regel=None):
    for _ in range(round(sekunden / dt)):
        if regel:
            regel(w, a)
        w.schritt(dt, a)
    return w


# --- Kessel -----------------------------------------------------------------------

def test_aufheizen_28_liter_mit_3500_watt():
    w = welt(raum_c=20, leitungswasser_c=12)
    w.wasser_einfuellen(28)
    assert w.z.k_temp == pytest.approx(12.2, abs=0.1)  # Topf (3000 J/K) hatte Raumtemperatur
    t = 0
    while w.z.k_temp < 57:
        w.schritt(10, Ausgaenge(heizung=1))
        t += 10
    assert 22 * 60 < t < 30 * 60  # 28 L * 4186 * 44 K / 3500 W = 24 min plus Verluste


def test_fuehler_ist_traege():
    w = welt()
    w.wasser_einfuellen(20)
    laufen(w, 60, Ausgaenge(heizung=1))
    assert w.messwert("Kesseltemperatur") < w.z.k_temp


def test_sieden_begrenzt_und_verdampft():
    w = welt()
    w.wasser_einfuellen(25, temp_c=99)
    laufen(w, 3600, Ausgaenge(heizung=1), dt=5)
    assert w.z.k_temp == pytest.approx(100.0)
    # 3500 W eine Stunde: knapp 5,6 L abzueglich Verluste
    assert 4.5 < 25 - w.z.k_vol_l < 5.6


def test_einmaischen_kuehlt_und_maische_loest_extrakt():
    w = welt(schuettung_kg=5.0)
    w.wasser_einfuellen(28, temp_c=60)
    w.einmaischen()
    assert 56 < w.z.k_temp < 58  # Malz mit Raumtemperatur
    a = Ausgaenge()

    def halten(w, a, soll=65):
        a.heizung = 1.0 if w.z.k_temp < soll else 0.0
    laufen(w, 90 * 60, a, dt=5, regel=halten)
    assert w.z.k_extrakt_pot_kg < 0.05
    assert 10 < w.kessel_plato() < 13
    vorher = w.z.k_vol_l
    w.treber_ziehen()
    assert w.z.k_vol_l == pytest.approx(vorher - 4.0)
    assert w.z.k_malz_kg == 0


def test_wuerzekuehler():
    w = welt()
    w.wasser_einfuellen(23, temp_c=100)
    t = 0
    while w.z.k_temp > 20:
        w.schritt(10, Ausgaenge(kuehlwasser=1))
        t += 10
    assert 20 * 60 < t < 45 * 60


def test_kessel_trocken_wird_ueber_105_grad_heiss():
    w = welt()
    w.wasser_einfuellen(20, temp_c=95)
    w.stoerung("kessel_trocken")
    laufen(w, 300, Ausgaenge(heizung=1))
    assert w.messwert("Kesseltemperatur") > 105


def test_heizungsrelais_klebt():
    w = welt()
    w.wasser_einfuellen(20, temp_c=50)
    w.stoerung("heizung_klebt")
    laufen(w, 600, Ausgaenge(heizung=0))
    assert w.z.k_temp > 60


# --- Wasser -----------------------------------------------------------------------

def test_wasser_ueber_eigenen_durchfluss_und_kein_wasser():
    w = welt(durchfluss_lpm=12)
    laufen(w, 30, Ausgaenge(wasser_offen=True), dt=0.1)
    assert w.messwert("Wassermenge") == pytest.approx(6.0, abs=0.05)
    assert w.messwert("Durchfluss") == pytest.approx(12.0)
    w.stoerung("kein_wasser")
    laufen(w, 10, Ausgaenge(wasser_offen=True), dt=0.1)
    assert w.messwert("Durchfluss") == 0 and w.messwert("Wassermenge") == pytest.approx(6.0, abs=0.05)


def test_wasser_vom_dosierer_gezaehlt():
    w = welt()
    w.schritt(1.0, Ausgaenge(wasser_offen=True, wasser_l=0.2))
    assert w.z.k_vol_l == pytest.approx(0.2) and w.messwert("Durchfluss") == pytest.approx(12.0)


# --- Gaertank ---------------------------------------------------------------------

def angestellt(**p):
    w = welt(**p)
    w.anstellen()
    return w


def halte_temp(soll):
    def regel(w, a):
        a.kuehlung = 1.0 if w.z.g_temp > soll + 0.2 else (0.0 if w.z.g_temp < soll else a.kuehlung)
    return regel


def test_anstellen_ohne_brautag_nimmt_vorgaben():
    w = angestellt(wuerze_l=23, stammwuerze_p=12)
    assert w.z.g_bier_l == 23 and w.messwert("Dichte") == pytest.approx(1.0484, abs=0.0005)
    assert w.kopf_l() == pytest.approx(12.0)
    assert w.messwert("Tankdruck") == pytest.approx(0.0, abs=1e-6)


def test_anstellen_nach_brautag_uebernimmt_wuerze():
    w = welt()
    w.wasser_einfuellen(24, temp_c=20)
    w.z.k_extrakt_kg = 3.0
    w.anstellen()
    assert w.z.g_bier_l == pytest.approx(23.0) and w.z.g_og_p == pytest.approx(11.11, abs=0.01)
    assert w.z.k_vol_l == 0


def test_gaerung_senkt_dichte_und_baut_druck_auf():
    w = angestellt(stammwuerze_p=12, vergaerungsgrad=0.78)
    a = Ausgaenge()
    laufen(w, 6 * 86400, a, dt=60, regel=halte_temp(18))
    assert w.messwert("Dichte") == pytest.approx(M.plato_zu_sg(12 * 0.22), abs=0.002)
    assert w.messwert("Tankdruck") > 2.4  # geschlossen: bis das Ueberdruckventil abblaest
    assert w.messwert("Tankdruck") < 2.6
    assert w.messwert("Gärtage") == pytest.approx(6.0, abs=0.01)


def test_kalt_gaert_es_kaum():
    warm, kalt = angestellt(raum_c=18), angestellt(raum_c=3)
    kalt.z.g_temp = kalt.z.g_luft = 3.0
    warm.z.g_temp = warm.z.g_luft = 18.0
    for w in (warm, kalt):
        laufen(w, 2 * 86400, Ausgaenge(), dt=60)
    assert kalt.z.g_rest > 0.99 and warm.z.g_rest < 0.6


def test_gaerwaerme_ohne_kuehlung_und_kuehlung_defekt():
    w = angestellt(raum_c=18)
    w.z.g_temp = w.z.g_luft = 18.0
    w.stoerung("kuehlung_defekt")
    laufen(w, 2 * 86400, Ausgaenge(kuehlung=1), dt=60)
    assert w.z.g_temp > 18.5


def test_spundventil_senkt_und_co2_ventil_hebt_druck():
    w = angestellt()
    laufen(w, 60, Ausgaenge(co2=True), dt=0.1)
    p = w.messwert("Tankdruck")
    assert 1.45 < p < 1.51  # bis zum Vordruck
    assert w.messwert("CO₂-Flasche") < 2000
    laufen(w, 0.5, Ausgaenge(spund=True), dt=0.1)
    assert 0.03 < p - w.messwert("Tankdruck") < 0.12  # ein Spundpuls: wenige Hundertstel bar


def test_co2_flasche_leer():
    w = angestellt()
    w.stoerung("co2_leer")
    assert w.messwert("CO₂-Vordruck") == 0
    laufen(w, 10, Ausgaenge(co2=True), dt=0.1)
    assert w.messwert("Tankdruck") == pytest.approx(0.0, abs=1e-6)
    assert w.messwert("CO₂-Flasche") == 0  # leere Flasche: kein Druck
    w.stoerung("co2_leer", False)
    # Flasche getauscht: wieder voll, also steht der Dampfdruck an
    assert w.messwert("CO₂-Flasche") == pytest.approx(M.co2_dampfdruck_bar(w.p.raum_c) - M.P_ATM, abs=0.2)


def test_gaspfosten_verstopft():
    w = angestellt()
    laufen(w, 30, Ausgaenge(co2=True), dt=0.1)
    w.stoerung("gaspfosten_zu")
    p_tank = w.kopf_bar_abs()
    laufen(w, 0.5, Ausgaenge(spund=True), dt=0.1)
    assert w.messwert("Tankdruck") < 0.1  # nur die kleine Leitung entleert sich
    assert w.kopf_bar_abs() == pytest.approx(p_tank, abs=0.005)
    laufen(w, 0.3, Ausgaenge(co2=True), dt=0.1)
    assert w.messwert("Tankdruck") > 1.3  # und ist sofort wieder voll
    w.stoerung("gaspfosten_zu", False)
    assert w.messwert("Tankdruck") == pytest.approx(p_tank - M.P_ATM, abs=0.02)


def test_ueberdruckventil_blaest_auch_bei_verstopftem_pfosten():
    w = angestellt()
    w.stoerung("gaspfosten_zu")
    w.z.g_kopf_mol = (M.P_ATM + 3.0) * w.kopf_l() / (M.R * (w.z.g_temp + M.KELVIN))
    laufen(w, 30, Ausgaenge(), dt=0.1)
    assert w.kopf_bar_abs() - M.P_ATM == pytest.approx(2.4, abs=0.05)


def test_kaltcrash_zieht_co2_ins_bier():
    w = angestellt()
    w.z.g_temp = w.z.g_luft = 18.0
    w.z.g_rest, w.z.g_hefe = 0.0, 1.0  # ausgegoren
    laufen(w, 60, Ausgaenge(co2=True), dt=0.1)  # auf 1,5 bar
    w.z.g_co2_gl = M.co2_loeslichkeit(18) * w.kopf_bar_abs()  # gesaettigt
    vorher = w.messwert("Tankdruck")
    laufen(w, 2 * 86400, Ausgaenge(kuehlung=1), dt=60)
    assert w.z.g_temp < 3
    assert w.messwert("Tankdruck") < vorher - 0.5


def test_zeitraffer_spart_echtzeit():
    langsam, schnell = welt(zeitraffer_brauen=1), welt(zeitraffer_brauen=60)
    for w in (langsam, schnell):
        w.wasser_einfuellen(20, temp_c=20)
    laufen(langsam, 600, Ausgaenge(heizung=1), dt=1)
    laufen(schnell, 10, Ausgaenge(heizung=1), dt=0.1)
    assert schnell.z.k_temp == pytest.approx(langsam.z.k_temp, abs=0.05)


def test_zustand_sichern_und_laden():
    w = angestellt()
    w.stoerung("gaspfosten_zu")
    laufen(w, 3600, Ausgaenge(), dt=10)
    neu = welt()
    neu.laden(w.zustand())
    assert neu.zustand() == w.zustand()
    neu.laden({"g_bier_l": 5, "unbekannt": 1})  # alte Datei, fremde Felder
    assert neu.z.g_bier_l == 5


def test_messwerte_haben_einheit_und_sind_zahlen():
    w = welt(rauschen=1)
    for name in M.MESSGROESSEN:
        assert isinstance(w.messwert(name), float)


# --- Zusammenspiel mit den echten Plugins (ohne cbpi) ----------------------------

def test_gaerdruckregler_haelt_soll_und_erkennt_stoerfaelle():
    T = pytest.importorskip("cbpi4_gaerdruck.technik")
    w = angestellt(zeitraffer_gaerung=60)
    w.z.g_temp = w.z.g_luft = 18.0
    regler = T.Druckregler(grenzdruck=2.0)
    meldungen = set()

    def durchlauf(sekunden):
        t = 0.0
        while t < sekunden:
            e = regler.entscheiden(1.0, w.messwert("Tankdruck"), 1.0, vordruck=w.messwert("CO₂-Vordruck"),
                                   vordruck_alter=1.0, vordruck_da=True)
            meldungen.update(x for x in (e.stoerung, e.hinweis) if x)
            puls = 0.5 if e.aktion == T.SPUNDEN else (0.3 if e.aktion == T.DRUECKEN else 0)
            a = Ausgaenge(spund=e.aktion in (T.SPUNDEN, T.NOTABLASS), co2=e.aktion == T.DRUECKEN)
            if puls:
                laufen(w, puls, a, dt=0.05)
                laufen(w, 1, Ausgaenge(), dt=0.1)  # Pause, wie einrichten.py sie fuer den Zeitraffer setzt
                t += puls + 1
            else:
                laufen(w, 1, a, dt=0.1)
                t += 1
    durchlauf(300)  # Nachdruecken von 0 auf Soll; die frische Wuerze nimmt CO2 auf
    assert 0.85 < w.messwert("Tankdruck") < 1.1
    durchlauf(1800)  # Gaerung am Anfang (Zeitraffer 60: 30 h), gespundet
    assert 0.85 < w.messwert("Tankdruck") < 1.1 and w.z.g_rest < 0.9
    assert not meldungen

    w.stoerung("gaspfosten_zu")
    durchlauf(60)
    assert any("verstopft" in m for m in meldungen)
    w.stoerung("gaspfosten_zu", False)

    meldungen.clear()
    regler = T.Druckregler(grenzdruck=2.0)
    w.stoerung("co2_leer")
    w.z.g_kopf_mol *= 0.5  # Druck faellt (z. B. Kaltcrash)
    durchlauf(30)
    assert any("Vordruck" in m for m in meldungen)


class Props(dict):
    def __getattr__(self, k):
        return self.get(k)


class FakeCbpi:
    def __init__(self, tmp_path):
        self.notes, self.registered, self.startup = [], {}, []
        self.log = types.SimpleNamespace(log_data=lambda id, v: None)
        self.ws = types.SimpleNamespace(send=lambda d, s=False: None)
        self.plugin = types.SimpleNamespace(register=lambda n, c: self.registered.__setitem__(n, c))
        self.actor = types.SimpleNamespace(data=[], actor_update=self._noop, find_by_id=lambda i: next(
            (s for s in self.actor.data if s.id == i), None))
        self.sensor = types.SimpleNamespace(data=[], find_by_id=lambda i: next(
            (s for s in self.sensor.data if s.id == i), None))
        self.config_folder = types.SimpleNamespace(get_file_path=lambda f: str(tmp_path / f))

    async def _noop(self, *a):
        return True

    def push_update(self, *a, **k):
        pass

    def notify(self, title, message, type=None, action=[]):
        self.notes.append((title, message, type))

    def register_on_startup(self, obj):
        self.startup.append(obj)


@pytest.fixture
def sim(tmp_path, monkeypatch):
    import cbpi4_simulation as plugin
    monkeypatch.setattr(plugin, "WELT", welt())
    return plugin, FakeCbpi(tmp_path)


def eintragen(c, art, id, name, inst, props=None, typ=None):
    getattr(c, art).data.append(types.SimpleNamespace(id=id, name=name, instance=inst, props=Props(props or {}),
                                                      type=typ))


def test_setup_registriert(sim):
    plugin, c = sim
    plugin.setup(c)
    assert set(c.registered) == {"BrauSimulation", "SimFuehler", "SimAktor", "SimEreignis", "SimulationsTakt"}


def test_parameter_aus_props_mit_prozent():
    from cbpi4_simulation import parameter_aus
    p = parameter_aus({"Ausbeute %": 70, "Zeitraffer Brauen": "30", "Heizleistung W": ""})
    assert p.ausbeute == pytest.approx(0.70) and p.zeitraffer_brauen == 30 and p.heizleistung_w == 3500


def test_simfuehler_liefert_und_faellt_aus(sim):
    plugin, c = sim
    f = plugin.SimFuehler(c, "s1", Props({"Messgröße": "Biertemperatur"}))
    assert f.messen() and f.value == 20.0 and f.alter() < 1
    assert f.get_unit() == "°C"
    asyncio.run(f.ausfall_umschalten())
    zeit = f.zeit
    assert not f.messen() and f.zeit == zeit and f.value == 20.0  # alter Wert bleibt, Alter waechst
    asyncio.run(f.ausfall_umschalten())
    assert f.messen()


def test_heizschutz_des_sicheraktors_greift_mit_simfuehler(sim, monkeypatch):
    brausicher = pytest.importorskip("cbpi4_brausicher")
    plugin, c = sim
    f = plugin.SimFuehler(c, "f1", Props({"Messgröße": "Kesseltemperatur"}))
    assert isinstance(f, brausicher.SicherFuehler)
    eintragen(c, "sensor", "f1", "Kesseltemperatur", f)
    p = {"GPIO": 5, "Art": "Heizung", "Fühler": "f1", "Simulation": "Ja", "Max Temperatur": 105}
    a = brausicher.SicherAktor(c, "a1", Props(p))
    eintragen(c, "actor", "a1", "Heizelement", a, p)
    zentrale = plugin.BrauSimulation(c, "z1", Props({"Heizung Kessel": "a1"}))
    eintragen(c, "sensor", "z1", "Brausimulation", zentrale, {"Heizung Kessel": "a1", "Zeitraffer Brauen": 60,
                                                             "Rauschen": 0}, "BrauSimulation")
    takt = plugin.SimulationsTakt(c)
    plugin.WELT.wasser_einfuellen(20, temp_c=95)

    async def go():
        await a.start()
        await a.on(100)
        a.running = True
        lauf = asyncio.create_task(a.run())
        an = []
        for _ in range(40):  # 4 s
            f.messen()
            takt.einmal(0.1)
            an.append(plugin.ausgang(c, "a1"))
            await asyncio.sleep(0.1)
        plugin.WELT.stoerung("kessel_trocken")
        for _ in range(60):
            f.messen()
            takt.einmal(0.1)
            await asyncio.sleep(0.1)
        a.running = False
        await lauf
        ergebnis = (max(an), plugin.ausgang(c, "a1"), a.schutz.verriegelt)
        await a.stop()
        return ergebnis
    geheizt, zuletzt, verriegelt = asyncio.run(go())
    assert geheizt == 1.0  # erst heizt er wirklich
    assert verriegelt and zuletzt == 0.0  # dann sperrt der echte Heizschutz
    assert any("Übertemperatur" in n[1] and "105" in n[1] for n in c.notes)


def test_fuehlerausfall_sperrt_heizung(sim):
    brausicher = pytest.importorskip("cbpi4_brausicher")
    plugin, c = sim
    f = plugin.SimFuehler(c, "f1", Props({"Messgröße": "Kesseltemperatur"}))
    eintragen(c, "sensor", "f1", "Kesseltemperatur", f)
    f.messen()
    plugin.WELT.stoerung("ausfall:Kesseltemperatur")
    f.messen()
    f.zeit -= 30  # 30 s ohne Wert
    p = {"GPIO": 5, "Art": "Heizung", "Fühler": "f1", "Simulation": "Ja"}
    a = brausicher.SicherAktor(c, "a1", Props(p))
    eintragen(c, "actor", "a1", "Heizelement", a, p)

    async def go():
        await a.start()
        await a.on(100)
        a.running = True
        lauf = asyncio.create_task(a.run())
        await asyncio.sleep(0.8)
        a.running = False
        await lauf
        an = plugin.ausgang(c, "a1")
        await a.stop()
        return an
    assert asyncio.run(go()) == 0.0


def test_sicherhysterese_laesst_sich_nach_stop_wieder_starten(sim):
    """Gefunden im simulierten Brautag: cbpi stoppt die Kessellogik am Ende jeder
    Rast (kettle.stop setzt nur state); blieb running stehen, startete die
    naechste Rast die Regelung nicht mehr ("already running")."""
    brausicher = pytest.importorskip("cbpi4_brausicher")
    plugin, c = sim
    k = brausicher.SicherHysterese(c, "k1", Props({}))
    k.get_kettle = lambda i: None

    async def go():
        await k.start()
        k.running = True
        k.task = asyncio.create_task(k._run())
        await asyncio.sleep(0.05)
        await k.stop()
        return k.running, k.state
    assert asyncio.run(go()) == (False, False)


def test_ausgang_simaktor_und_unbekannt(sim):
    plugin, c = sim
    s = plugin.SimAktor(c, "x", Props({}))
    asyncio.run(s.start())
    eintragen(c, "actor", "x", "Kühlwasser", s)
    assert plugin.ausgang(c, "x") == 0.0
    asyncio.run(s.on(40))
    assert plugin.ausgang(c, "x") == pytest.approx(0.4) and s.get_state() is True
    assert plugin.ausgang(c, "fehlt") == 0.0 and plugin.ausgang(c, None) == 0.0


def test_wasserzaehler_liest_ventildosierer_simulation(sim):
    regler = pytest.importorskip("cbpi4_ventil.regler")
    plugin, c = sim
    uhr = [0.0]
    backend = regler.SimBackend(387, uhr=lambda: uhr[0])
    inst = types.SimpleNamespace(regler=types.SimpleNamespace(b=backend), state=True)
    eintragen(c, "actor", "w", "Wasserventil", inst)
    z = plugin.Wasserzaehler()
    assert z.liter(c, "w", plugin.WELT) == (0.0, False)
    backend.ventil(True)
    uhr[0] = 10.0
    backend.impulse()  # der Regelthread zaehlt
    liter, offen = z.liter(c, "w", plugin.WELT)
    assert liter == pytest.approx(2.0, abs=0.01) and offen  # 12 L/min aus den Parametern
    plugin.WELT.stoerung("kein_wasser")
    z.liter(c, "w", plugin.WELT)
    assert backend.fluss_lpm == 0.0


def test_ereignis_schritt_anstellen_startet_gaerplan(sim):
    plugin, c = sim
    gestartet = []

    async def start(fid):
        gestartet.append(fid)
    c.fermenter = types.SimpleNamespace(start=start)
    step = plugin.SimEreignis.__new__(plugin.SimEreignis)
    step.cbpi, step.name, step.running, step.summary = c, "Anstellen", False, ""
    step.props = Props({"Ereignis": "Anstellen", "Gärtank": "t1", "Gärplan starten": "Ja"})
    weiter = []

    async def next_():
        weiter.append(True)

    async def push():
        pass
    step.next, step.push_update = next_, push

    async def go():
        await step.run()
        await asyncio.sleep(0)
    asyncio.run(go())
    assert gestartet == ["t1"] and weiter and plugin.WELT.z.angestellt


def test_stoerfall_aktion_meldet_erwartung(sim):
    plugin, c = sim
    z = plugin.BrauSimulation(c, "z1", Props({}))
    asyncio.run(z.stoerfall_ausloesen(**{"Störfall": "CO₂-Flasche leer"}))
    assert plugin.WELT.aktiv("co2_leer") and "Erwartet" in c.notes[-1][1]
    asyncio.run(z.stoerfaelle_aufheben())
    assert not plugin.WELT.z.stoerungen


def test_takt_sichert_und_laedt(sim, tmp_path):
    plugin, c = sim
    takt = plugin.SimulationsTakt(c)
    plugin.WELT.wasser_einfuellen(12)
    takt.sichern()
    plugin.WELT.zuruecksetzen()

    async def go():
        await takt.beim_start()
        takt._task.cancel()
    asyncio.run(go())
    assert plugin.WELT.z.k_vol_l == pytest.approx(12)


def test_aktion_anlegen_ruft_einrichtung_mit_eigenen_einstellungen(sim, monkeypatch):
    plugin, c = sim
    c.static_config = {"port": 8123}
    aufrufe = []

    def einrichten(url, zb, zg, nr, plaene, zentrale_id, ausgabe, zentrale_props):
        aufrufe.append((url, zb, zg, nr, plaene, zentrale_id, zentrale_props))
        ausgabe("  angelegt:  Kesseltemperatur")
        ausgabe("HINWEIS: cbpi4-ventil fehlt")
    monkeypatch.setattr(plugin.einrichtung, "einrichten", einrichten)
    z = plugin.BrauSimulation(c, "z1", Props({"Zeitraffer Brauen": 30, "Zeitraffer Gärung": 90}))
    asyncio.run(z.anlegen(**{"Maisch- und Gärplan schreiben": "Nein", "Dashboard": 2}))
    assert aufrufe == [("http://127.0.0.1:8123", 30, 90, 2, False, "z1", {"Beim Speichern anlegen": "Nein"})]
    assert any("HINWEIS" in n[1] for n in c.notes) and "angelegt" in c.notes[-1][1]


def test_aktion_anlegen_meldet_fehler(sim, monkeypatch):
    plugin, c = sim

    def einrichten(*a):
        raise plugin.einrichtung.EinrichtungsFehler("Aktor „Spundventil“ gibt es schon und ist NICHT simuliert")
    monkeypatch.setattr(plugin.einrichtung, "einrichten", einrichten)
    asyncio.run(plugin.BrauSimulation(c, "z1", Props({})).anlegen())
    assert "NICHT simuliert" in c.notes[-1][1]


def test_einrichtung_zeiten_und_gaerplan():
    from cbpi4_simulation import einrichtung as E
    assert E.echt_min(60, 60) == 1 and E.echt_min(10, 60) == 1 and E.echt_min(4 * 1440, 120) == 48
    plan = E.gaerplan({"Biertemperatur": "b"}, 120)
    assert [s[2]["TimerM"] for s in plan] == [48, 12, 24] and plan[2][2]["Temp"] == 2


def ids_fuer_plan():
    from cbpi4_simulation import einrichtung as E
    ids = {name: f"a-{name}" for name in E.AKTOREN}
    ids.update({"kessel": "k", "Kesseltemperatur": "s", "tank": "t",
                "Wasserventil": "a-Wasserventil", "Kühlwasser": "a-Kühlwasser"})
    return ids


def test_ruehrwerk_laeuft_ueber_die_ganze_maische():
    from cbpi4_simulation import einrichtung as E
    plan = E.maischplan(ids_fuer_plan(), 60, mit_dosierer=True)
    namen = [s[0] for s in plan]
    ein, aus = namen.index("Rührwerk ein"), namen.index("Rührwerk aus")
    # Einschalten vor dem ersten Heizschritt, Ausschalten nach der letzten Rast
    assert ein < namen.index("Aufheizen zum Einmaischen")
    assert aus == namen.index(E.RASTEN[-1][0]) + 1
    # Alle Rasten liegen dazwischen
    for name, _, _ in E.RASTEN:
        assert ein < namen.index(name) < aus
    schalter = {s[0]: s[2] for s in plan if s[1] == "ToggleStep"}
    assert schalter["Rührwerk ein"] == {"toggle_type": "On", "Actor": "a-Rührwerk"}
    assert schalter["Rührwerk aus"] == {"toggle_type": "Off", "Actor": "a-Rührwerk"}


def test_dampfkondensator_ab_80_grad_bis_nach_dem_kochen():
    from cbpi4_simulation import einrichtung as E
    plan = E.maischplan(ids_fuer_plan(), 60, mit_dosierer=True)
    namen = [s[0] for s in plan]
    aufheizen = namen.index(f"Aufheizen auf {E.DAMPF_AB_C} °C")
    ein, kochen, aus = (namen.index("Dampfkondensator ein"), namen.index("Kochen"),
                        namen.index("Dampfkondensator aus"))
    assert aufheizen + 1 == ein and ein + 1 == kochen and kochen + 1 == aus
    # Der Aufheizschritt endet genau bei der Schwelle: Zieltemperatur, kein Timer
    heizschritt = plan[aufheizen][2]
    assert heizschritt["Temp"] == E.DAMPF_AB_C and heizschritt["Timer"] == 0
    # Waehrend der Maische wird die Schwelle nie erreicht
    assert max(temp for _, temp, _ in E.RASTEN) < E.DAMPF_AB_C
    schalter = {s[0]: s[2] for s in plan if s[1] == "ToggleStep"}
    assert schalter["Dampfkondensator ein"]["Actor"] == "a-Dampfkondensator"
    assert schalter["Dampfkondensator aus"] == {"toggle_type": "Off", "Actor": "a-Dampfkondensator"}


def test_simulierte_gpios_bleiben_frei_und_eindeutig():
    from cbpi4_simulation import einrichtung as E
    # Belegt am HAT (Biersteuerung, Zweig ventile-druck)
    hat = {2, 3, 4, 6, 9, 10, 11, 13, 16, 17, 18, 19, 20, 21, 22, 26, 27}
    belegt = [g for g, _ in E.AKTOREN.values()] + [E.WASSERVENTIL[1], E.WASSERVENTIL[2]]
    assert not set(belegt) & hat, "simulierter Aktor liegt auf einem Pin des HAT"
    assert len(set(belegt)) == len(belegt), "GPIO doppelt vergeben"


# --- Flaschendruck -----------------------------------------------------------------

def test_co2_dampfdruckkurve_trifft_tabellenwerte():
    # Span/Wagner gegen die uebliche Tabelle (absolut, bar)
    for t_c, soll in ((0, 34.85), (10, 44.95), (15, 50.87), (20, 57.29), (25, 64.3), (30, 72.1)):
        assert M.co2_dampfdruck_bar(t_c) == pytest.approx(soll, abs=0.1), f"{t_c} °C"
    # Ueber dem kritischen Punkt (31,0 °C) gibt es keine Fluessigkeit mehr
    assert M.co2_dampfdruck_bar(35) == pytest.approx(73.773, abs=0.01)


def test_flaschendruck_haengt_nicht_am_fuellstand():
    w = M.Welt()
    w.p.raum_c = 20.0
    voll = _flaschendruck(w, 2000)
    # Von voll bis auf einen Rest bleibt der Druck gleich: es ist fluessiges CO2 da
    for g in (1500, 1000, 500):
        assert _flaschendruck(w, g) == pytest.approx(voll, abs=0.01), f"{g} g"
    assert voll == pytest.approx(57.29 - M.P_ATM, abs=0.1)  # Ueberdruck am Manometer


def test_flaschendruck_faellt_erst_wenn_nichts_fluessiges_mehr_da_ist():
    w = M.Welt()
    w.p.raum_c = 20.0
    voll = _flaschendruck(w, 2000)
    assert _flaschendruck(w, 200) < voll - 10      # alles gasfoermig, Druck faellt
    assert _flaschendruck(w, 20) < _flaschendruck(w, 50)
    assert _flaschendruck(w, 0) == 0.0
    # monoton: weniger CO2 ergibt nie mehr Druck
    werte = [_flaschendruck(w, g) for g in (2000, 1000, 400, 200, 100, 50, 10, 0)]
    assert werte == sorted(werte, reverse=True)


def test_flaschendruck_steigt_mit_der_temperatur():
    w = M.Welt()
    kalt, warm = _flaschendruck(w, 2000, raum_c=10), _flaschendruck(w, 2000, raum_c=25)
    assert kalt < warm
    # Timms Flaschen zeigen rund 50 bar -- das passt zu etwa 15 °C
    assert _flaschendruck(w, 2000, raum_c=15) == pytest.approx(50, abs=1.0)


def test_flasche_wird_als_druck_gemessen():
    w = M.Welt()
    w.p.rauschen = 0
    assert M.MESSGROESSEN["CO₂-Flasche"] == "bar"
    w.z.g_flasche_g = 2000.0
    w.p.raum_c = 20.0
    assert w.messwert("CO₂-Flasche") == pytest.approx(w.flaschendruck_bar(), abs=0.001)
    # Leere Flasche: 0 bar, nie None
    w.z.g_flasche_g = 0.0
    assert w.messwert("CO₂-Flasche") == 0.0


def test_stoerfall_co2_leer_zieht_den_flaschendruck_auf_null():
    w = M.Welt()
    w.p.rauschen = 0
    w.stoerung("co2_leer", True)
    assert w.messwert("CO₂-Flasche") == 0.0 and w.vordruck_bar() == 0.0


def _flaschendruck(w, gramm, raum_c=None):
    if raum_c is not None:
        w.p.raum_c = raum_c
    w.z.g_flasche_g = float(gramm)
    return w.flaschendruck_bar()
