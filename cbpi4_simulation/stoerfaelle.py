"""Loest jeden Stoerfall der Brausimulation aus und prueft, ob die echten
Plugins (brausicher, gaerdruck, ventil) richtig reagieren.

Nur Standardbibliothek. Gegen ein cbpi mit eingerichteter Simulation, in
dem gerade NICHT gebraut wird (Kessel, Gaertank und Simulation werden
benutzt und zurueckgesetzt). Aufruf im Browser (Aktion "Störfälle prüfen"
am Fuehler BrauSimulation) oder mit stoerfaelle.py auf der Kommandozeile.

Mit `log` werden zusaetzlich die Meldungen im Log von cbpi gesucht
(Log-Stufe WARNING genuegt). Ergebnis: eine Zeile je Pruefung.
"""
import json
import time
import urllib.request


def laeuft(eintrag):
    st = eintrag.get("state")
    return bool(st.get("running") if isinstance(st, dict) else st)


class Cbpi:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def rufen(self, pfad, daten=None, methode=None):
        body = None if daten is None else json.dumps(daten).encode()
        req = urllib.request.Request(self.url + pfad, data=body, method=methode or ("POST" if body is not None
                                                                                     else "GET"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode()
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            return text

    def liste(self, art):
        return self.rufen(f"/{art}/")["data"]


class Pruefung:
    def __init__(self, url, log=None, ausgabe=print):
        self.c = Cbpi(url)
        self.log = log
        self.aus = ausgabe
        self.fehler = 0
        self.s = {x["name"]: x["id"] for x in self.c.liste("sensor")}
        self.a = {x["name"]: x["id"] for x in self.c.liste("actor")}
        self.kessel = self.c.liste("kettle")[0]
        self.tank = self.c.liste("fermenter")[0]
        self.log_start = self._log_laenge()

    # --- Hilfen ---

    def _log_laenge(self):
        if not self.log:
            return 0
        with open(self.log, encoding="utf-8", errors="replace") as f:
            return len(f.read())

    def log_seit(self, start):
        if not self.log:
            return ""
        with open(self.log, encoding="utf-8", errors="replace") as f:
            return f.read()[start:]

    def wert(self, name):
        return self.c.rufen(f"/sensor/{self.s[name]}")["value"]

    def an(self, name):
        st = next(x for x in self.c.liste("actor") if x["id"] == self.a[name])["state"]
        return bool(st.get("state") if isinstance(st, dict) else st)

    def aktion(self, art, id, name, parameter=None):
        self.c.rufen(f"/{art}/{id}/action", dict(action=name, parameter=parameter or {}))

    def sim(self, name, **parameter):
        self.aktion("sensor", self.s["Brausimulation"], name, parameter)

    def stoerfall(self, name):
        self.sim("stoerfall_ausloesen", **{"Störfall": name})

    def aufheben(self):
        self.sim("stoerfaelle_aufheben")

    def warten(self, bedingung, sekunden, takt=0.5):
        ende = time.time() + sekunden
        while time.time() < ende:
            if bedingung():
                return True
            time.sleep(takt)
        return bedingung()

    def beobachten(self, sekunden, aktoren):
        """Wie oft war jeder Aktor an (Abfrage alle 0,1 s)?"""
        zaehler = {n: 0 for n in aktoren}
        ende = time.time() + sekunden
        while time.time() < ende:
            daten = {x["id"]: x["state"] for x in self.c.liste("actor")}
            for n in aktoren:
                st = daten[self.a[n]]
                if st.get("state") if isinstance(st, dict) else st:
                    zaehler[n] += 1
            time.sleep(0.1)
        return zaehler

    def ergebnis(self, name, ok, detail=""):
        self.aus(f"{'OK    ' if ok else 'FEHLER'} {name}{': ' + detail if detail else ''}")
        if not ok:
            self.fehler += 1

    def im_log(self, start, *texte):
        if not self.log:
            return True, "(ohne --log nicht geprüft)"
        # Meldungen kommen aus den Regelschleifen der Plugins, also etwas spaeter
        self.warten(lambda: all(t in self.log_seit(start) for t in texte), 10)
        neu = self.log_seit(start)
        fehlt = [t for t in texte if t not in neu]
        return not fehlt, ("Log: " + ", ".join(texte)) if not fehlt else f"im Log fehlt: {', '.join(fehlt)}"

    # --- Kessel ---

    def kessel_an(self, soll):
        self.c.rufen(f"/kettle/{self.kessel['id']}/target_temp", dict(temp=soll))
        k = next(x for x in self.c.liste("kettle") if x["id"] == self.kessel["id"])
        if not laeuft(k):
            self.c.rufen(f"/kettle/{self.kessel['id']}/toggle", {})

    def kessel_aus(self):
        # /kettle/{id}/off antwortet in cbpi 4.7.7 mit HTTP 500 (Methode fehlt im Controller)
        k = next(x for x in self.c.liste("kettle") if x["id"] == self.kessel["id"])
        if laeuft(k):
            self.c.rufen(f"/kettle/{self.kessel['id']}/toggle", {})

    def pruefe_kessel(self):
        self.aus("== Kessel")
        self.sim("zuruecksetzen")
        self.sim("wasser", Liter=20)
        self.kessel_an(99)  # heizt die ganze Pruefung ueber, Soll wird nicht erreicht
        heizt = self.warten(lambda: self.wert("Heizleistung") > 0, 10)
        self.ergebnis("Kessellogik heizt (Ausgangslage)", heizt, f"{self.wert('Heizleistung'):.0f} W")

        start = self._log_laenge()
        self.stoerfall("Fühlerausfall Kesseltemperatur")
        aus = self.warten(lambda: self.wert("Heizleistung") == 0, 20)
        ok, text = self.im_log(start, "kein gültiger Wert", "älter")
        self.ergebnis("Fühlerausfall Kessel: SicherAktor sperrt die Heizung", aus and ok, text)
        self.aufheben()
        wieder = self.warten(lambda: self.wert("Heizleistung") > 0, 15)
        self.ergebnis("Fühler wieder da: Heizung läuft weiter", wieder)

        self.warten(lambda: self.wert("Heizleistung") > 0, 10)
        start = self._log_laenge()
        self.stoerfall("Übertemperatur (Kessel trocken)")
        aus = self.warten(lambda: self.wert("Heizleistung") == 0, 20)
        temp = self.wert("Kesseltemperatur")
        ok, text = self.im_log(start, "Übertemperatur")
        self.ergebnis("Übertemperatur: SicherAktor sperrt", aus and ok, f"Fühler {temp:.0f} °C, {text}")
        self.aufheben()
        self.sim("wasser", Liter=20)
        bleibt = not self.warten(lambda: self.wert("Heizleistung") > 0, 6)
        self.ergebnis("Sperre bleibt nach dem Abkühlen bestehen (verriegelt)", bleibt)
        self.aktion("actor", self.a["Heizelement"], "schutz_zuruecksetzen")
        wieder = self.warten(lambda: self.wert("Heizleistung") > 0, 10)
        self.ergebnis("„Schutz zurücksetzen“ gibt die Heizung frei", wieder)

        self.kessel_aus()
        self.warten(lambda: self.wert("Heizleistung") == 0, 10)
        self.stoerfall("Heizungsrelais klebt")
        time.sleep(3)
        klebt = self.wert("Heizleistung") > 0 and not self.an("Heizelement")
        self.ergebnis("Relais klebt: heizt trotz Aus (Grenze der Software, Hardware-Schutz nötig)", klebt)
        self.aufheben()

    # --- Wasser ---

    def pruefe_wasser(self):
        self.aus("== Wasser")
        vorher = self.wert("Wassermenge")
        self.stoerfall("Kein Wasser (Hahn zu)")
        self.c.rufen(f"/actor/{self.a['Wasserventil']}/on", {})
        offen = self.warten(lambda: self.an("Wasserventil"), 3)
        zu = self.warten(lambda: not self.an("Wasserventil"), 20)
        self.ergebnis("Kein Wasser: VentilDosierer schließt nach der Anlaufzeit", offen and zu,
                      f"Menge {self.wert('Wassermenge') - vorher:.2f} L")
        self.aufheben()

    # --- Gaertank ---

    def tank_an(self, temp, druck):
        t = self.tank["id"]
        self.c.rufen(f"/fermenter/{t}/target_temp", dict(temp=temp))
        self.c.rufen(f"/fermenter/{t}/target_pressure", dict(pressure=druck))
        f = next(x for x in self.c.liste("fermenter") if x["id"] == t)
        if not laeuft(f):
            self.c.rufen(f"/fermenter/{t}/toggle", {})

    def tank_neu_starten(self):
        t = self.tank["id"]
        self.c.rufen(f"/fermenter/{t}/toggle", {})
        time.sleep(1.5)
        self.c.rufen(f"/fermenter/{t}/toggle", {})

    def pruefe_gaertank(self):
        self.aus("== Gärtank")
        t = self.tank["id"]
        if laeuft(next(x for x in self.c.liste("fermenter") if x["id"] == t)):
            self.c.rufen(f"/fermenter/{t}/toggle", {})  # frisch starten: Sperren der Regelung zuruecksetzen
            time.sleep(1.5)
        self.sim("zuruecksetzen")
        self.sim("anstellen")
        # Erst nach einer Fuehlerrunde starten: Sonst sieht die Wirkungspruefung des Reglers
        # noch den Druck von vor dem Zuruecksetzen als Ausgangswert und sperrt das Nachdruecken
        time.sleep(2)
        self.tank_an(18, 1.0)
        # Nachgedrueckt wird bis zur Bandgrenze Soll - 0,10 bar
        da = self.warten(lambda: self.wert("Tankdruck") > 0.85, 180, takt=2)
        f = next(x for x in self.c.liste("fermenter") if x["id"] == t)
        self.ergebnis("Nachdrücken auf 1,0 bar (Ausgangslage)", da,
                      f"{self.wert('Tankdruck'):.2f} bar" + ("" if da else
                      f" (Regelung {'an' if laeuft(f) else 'AUS'}, Soll {f.get('target_pressure')} bar, "
                      f"Vordruck {self.wert('CO₂-Vordruck')} bar, Flasche {self.wert('CO₂-Flasche')} g)"))

        start = self._log_laenge()
        self.stoerfall("Fühlerausfall Tankdruck")
        time.sleep(11)
        z = self.beobachten(10, ["Spundventil", "CO₂-Ventil"])
        ok, text = self.im_log(start, "Tankdruck unbekannt")
        self.ergebnis("Fühlerausfall Tankdruck: Spundventil pulst, kein Nachdrücken",
                      z["Spundventil"] > 0 and z["CO₂-Ventil"] == 0 and ok, f"{z}, {text}")
        self.aufheben()
        self.tank_neu_starten()
        self.warten(lambda: self.wert("Tankdruck") > 0.85, 120, takt=2)

        start = self._log_laenge()
        self.stoerfall("CO₂-Flasche leer")
        time.sleep(2)  # der Vordruckfuehler meldet einmal je Sekunde
        self.tank_an(18, 1.5)
        z = self.beobachten(10, ["CO₂-Ventil"])
        ok, text = self.im_log(start, "CO₂-Vordruck")
        self.ergebnis("CO₂ leer: kein Nachdrücken, Meldung Vordruck", z["CO₂-Ventil"] == 0 and ok, f"{z}, {text}")
        self.aufheben()
        self.tank_an(18, 1.0)
        self.warten(lambda: abs(self.wert("Tankdruck") - 1.0) < 0.12, 120, takt=2)

        start = self._log_laenge()
        self.stoerfall("Gaspfosten verstopft")
        self.tank_an(18, 0.5)
        self.beobachten(15, ["Spundventil"])
        ok, text = self.im_log(start, "verstopft")
        self.ergebnis("Gaspfosten verstopft: Meldung „Gasweg zum Tank verstopft“", ok, text)
        self.aufheben()
        self.tank_an(18, 1.0)

        start = self._log_laenge()
        self.stoerfall("Fühlerausfall Biertemperatur")
        self.tank_an(2, 1.0)  # Kuehlung soll laufen
        an = self.warten(lambda: self.an("Kühlschrank"), 5)
        aus = self.warten(lambda: not self.an("Kühlschrank"), 45)
        self.ergebnis("Fühlerausfall Bier: GaerDruckLogik schaltet die Kühlung ab", aus,
                      "Kühlung war zuvor an" if an else "Kühlung war schon aus")
        self.aufheben()

        self.stoerfall("Kühlung defekt")
        t0, b0 = self.wert("Kühlschrank Luft"), self.wert("Biertemperatur")
        time.sleep(20)
        t1, b1 = self.wert("Kühlschrank Luft"), self.wert("Biertemperatur")
        self.ergebnis("Kühlung defekt: Kühlschrankluft wird trotz laufender Kühlung wärmer",
                      self.an("Kühlschrank") and t1 > t0 + 0.5,
                      f"Luft {t0:.1f} -> {t1:.1f} °C, Bier {b0:.1f} -> {b1:.1f} °C (keine Meldung der Gärtanklogik)")
        self.aufheben()
        self.c.rufen(f"/fermenter/{self.tank['id']}/toggle", {})


def pruefen(url, log=None, ausgabe=print):
    """Alle Pruefungen; gibt die Zahl der Fehlschlaege zurueck."""
    p = Pruefung(url, log, ausgabe)
    try:
        p.pruefe_kessel()
        p.pruefe_wasser()
        p.pruefe_gaertank()
    finally:
        p.aufheben()
        p.sim("zuruecksetzen")
    ausgabe(f"{p.fehler} Fehlschläge")
    return p.fehler
