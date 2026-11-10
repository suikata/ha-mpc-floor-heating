# MPC Fußbodenheizung für Home Assistant

🇬🇧 [English version](README.md)

Modellprädiktive Regelung (MPC) der **Vorlauftemperatur** eines Fußbodenheizkreises.

Ein Estrichboden reagiert erst Stunden später auf eine Änderung der Vorlauftemperatur. Eine
Heizkurve schaut nur auf die Außentemperatur von jetzt: An sonnigen Tagen überheizt der Raum, bei
Kälteeinbrüchen hinkt er hinterher. Diese Integration

1. **lernt ein Modell deines Gebäudes** aus den Recorder-Statistiken, die du schon hast
   (Raumtemperaturen, gemessener Vorlauf, Außentemperatur),
2. **schaut 36 Stunden voraus** mit der stündlichen Wettervorhersage (optional auch mit einer
   Strompreis-Prognose),
3. **berechnet den Vorlauf-Fahrplan**, der jeden Raum auf den Sollwert seines eigenen Thermostats
   bringt, mit möglichst wenig und möglichst ruhigem Heizen,
4. und **gibt jede Stunde die erste Stunde dieses Plans** an deine Heizungssteuerung weiter.

Eingerichtet und eingestellt wird alles in der Oberfläche. Kein YAML, keine App, keine Cloud.

## Wie es funktioniert

Das Modell hat zwei Wärmespeicher, den Estrich und die Raumluft:

```
Estrich:  xf[k+1] = λ·xf[k] + (1-λ)·u[k-d]                λ = exp(-1/τ)
Raum:     y[k+1]  = y[k] + a·(xf[k]-y[k]) + b·(o[k]-y[k]) + c
```

`u` Vorlauftemperatur, `o` Außentemperatur, `y` mittlere Raumtemperatur, `d` Totzeit,
`τ` Zeitkonstante des Estrichs. `d` und `τ` werden per Rastersuche bestimmt, `a`, `b`, `c` per
Methode der kleinsten Quadrate. Gewählt wird der Kandidat mit dem kleinsten
**12-Stunden-Prognosefehler**, denn genau darauf verlässt sich der Regler. Das Modell wird jede
Nacht um 03:17 aus den letzten 30 Tagen neu gelernt.

Das Modell beschreibt den Mittelwert aller Räume. Jeder Raum folgt diesem Mittelwert mit seinem
aktuellen Abstand (ein Raum, der 0,6 K unter dem Mittel liegt, bleibt etwa 0,6 K darunter). Der
Regler minimiert über den Prognosehorizont

* für jeden Raum die quadrierte Unterschreitung von *eigenem Sollwert − Toleranz* (starkes
  Gewicht; der kälteste Raum zählt am meisten),
* einen Mittelwert über *mittlerem Sollwert + Toleranz* (Räume über ihrem Sollwert drosseln mit
  ihren eigenen Ventilen, daher wird nach oben nur der Mittelwert begrenzt),
* den Abstand zu dem Niveau, bei dem der bedürftigste Raum gerade seinen Sollwert erreicht
  (schwaches Gewicht),
* Änderungen der Vorlauftemperatur („Gewicht Ruhe“),
* optional `Preis × Vorlauftemperatur` („Gewicht Energie/Preis“),

unter Einhaltung von Vorlauf-Minimum und -Maximum. Das Problem ist konvex und wird in reinem
numpy (beschleunigter projizierter Gradient) in wenigen Millisekunden gelöst.

Die Raumtemperaturen werden vor der Verwendung über die letzten 2 Stunden gemittelt. Lüften oder eine offene Tür verändern so die Vorlauftemperatur eines Bodens, der erst nach Stunden reagiert, nicht. Nach einem Neustart von Home Assistant wartet der Regler etwa 3 Minuten, bis Thermostate und Sensoren gemeldet haben, statt mit der Hälfte der Räume zu planen.

## Installation

**HACS:** HACS → ⋮ → Benutzerdefinierte Repositories → dieses Repository als Typ *Integration*
hinzufügen → *MPC Floor Heating* herunterladen → Home Assistant neu starten.

**Manuell:** `custom_components/mpc_floor_heating` nach `/config/custom_components/` kopieren und
neu starten.

Danach: **Einstellungen → Geräte & Dienste → Integration hinzufügen → MPC Floor Heating**.

### Voraussetzungen

* Stündliche **Langzeitstatistiken** für Raum-, Vorlauf- und Außensensoren (Sensoren mit
  `state_class: measurement`; sonst wird der Zustandsverlauf genutzt, der meist nur 10 Tage
  reicht). Mindestens 3 Tage Daten, besser 2 bis 4 Wochen.
* Eine gemessene **Vorlauftemperatur** des Heizkreises (nach dem Mischer).
* Eine Möglichkeit, den Vorlauf-Sollwert vorzugeben (siehe Ausgabearten).
* Optional: eine `weather`-Entität mit **stündlicher** Vorhersage (nur solche werden angeboten),
  ein Strompreis-Sensor, z. B. aus der [EPEX-Spot-Integration](https://github.com/mampfes/ha_epex_spot).
* Temperaturen in °C.

## Einrichtung

**Schritt 1, Messwerte:** die Räume des Heizkreises, gemessener Vorlauf, Außentemperatur (ideal:
Median mehrerer Sensoren), Wettervorhersage, Strompreis-Sensor.

Räume sind vorzugsweise **Raumthermostate** (Climate-Entitäten): Verwendet werden ihre Ist- und
ihre Solltemperatur, auch wenn ein Wochenprogramm umschaltet. Alle Thermostate sind
vorausgewählt; entferne die Räume, die dieser Kreis nicht beheizt. Ausgeschaltete Räume (Sollwert
unter 10 °C) werden ignoriert. Zum Lernen nimmt die Integration den Temperatursensor desselben
Geräts, weil Climate-Entitäten keine Langzeitstatistik haben. Räume mit nur einem Thermometer
lassen sich ebenfalls hinzufügen; sie verwenden den *Sollwert für Räume ohne Thermostat*.

Der Strompreis-Sensor braucht eine Liste stündlicher Preise in einem Attribut. Automatisch erkannt
werden EPEX Spot (`data`: `start_time`/`price_per_kwh`), Nord Pool (`raw_today`/`raw_tomorrow`:
`start`/`value`), Tibber-artige Formate (`startsAt`/`total`) und ähnliche.

**Schritt 2, Sollwert-Ausgabe.** Drei Wege; das nächste Formular fragt nur, was der gewählte Weg
braucht:

| Ausgabeart | Was passiert | Nächstes Formular |
|---|---|---|
| **Eigener Sollwert-Sensor** (Standard) | Die Integration legt den Sensor *Vorlauf Sollwert Ausgang* an. Deine Heizungssteuerung liest ihn, z. B. mit einem ESPHome-`homeassistant`-Sensor. Er hat nur einen Wert, solange die Regelung aktiv und das Modell glaubwürdig ist; sonst ist er `unavailable` und die Steuerung bleibt bei ihrer eigenen Heizkurve. | keines |
| **Climate-Entität** | Der Sollwert wird jede Stunde als Solltemperatur einer Climate-Entität gesetzt. | Climate-Entität |
| **Zahlen-Entität** | Der Sollwert wird jede Stunde in eine `number` / `input_number` geschrieben. Entitäten, deren Maximum unter dem *Vorlauf Minimum* liegt (z. B. ein Offset), werden verweigert. | Zahlen-Entität |

Die Integration bringt ihren eigenen Schalter **Regelung aktiv** mit; dafür muss nichts angelegt
werden. Er startet ausgeschaltet: Der Plan wird berechnet und angezeigt, aber nichts ausgegeben.
Vergleiche ihn ein paar Tage mit deiner Heizkurve und schalte dann ein.

In den Optionen kannst du zusätzlich eine *Heizbetrieb-Bedingung* wählen (z. B. die Betriebsart
der Heizung): Solange sie `off`/`cool` ist, pausiert die Regelung automatisch.

### Einlernen

Ist schon Verlauf im Recorder, ist das Modell direkt nach der Einrichtung einsatzbereit. Ohne
genug Verlauf zeigt der Status *Modell wird eingelernt* und der Sensor *Lernfortschritt* zählt bis
100 % hoch (7 Tage nutzbare Stundenwerte und ein 12-h-Prognosefehler unter 0,5 K). Bis dahin wird
nichts ausgegeben, und das Modell wird stündlich statt nächtlich neu gelernt.

## Entitäten

| Entität | Bedeutung |
|---|---|
| Vorlauf Sollwert Ausgang | Nur bei „Eigener Sollwert-Sensor“: der Sollwert für die Heizungssteuerung, `unavailable` solange die Regelung aus ist. |
| Vorlauf Sollwert berechnet | Immer: der optimale Sollwert für diese Stunde. Attribut `plan`: stündlicher Plan aus Vorlauf, vorhergesagter Raumtemperatur, Außentemperatur und Preis über den ganzen Horizont. |
| Raumtemperatur Mittel | Aktueller Mittelwert der Räume. |
| Größte Unterschreitung des Sollwerts | Kleinster Wert von *Raum − Sollwert* der aktiven Räume. Attribut `rooms`: jeder Raum mit Temperatur, Sollwert und Abweichung. |
| Raumtemperatur Prognose 6 h | Vorhergesagter Raummittelwert (Attribute: in 12 h, Min/Max der nächsten 24 h, größte Unterschreitung in 6 h und über 24 h). |
| Status | Modell wird eingelernt · Bereit, Regelung aus · Regelung aktiv · Pausiert, kein Heizbetrieb · Messwert fehlt · Übertragung fehlgeschlagen |
| Lernfortschritt | 0 bis 100 %, siehe oben. |
| Toleranz um den Raum-Sollwert | Wie weit ein Raum unter seinen Sollwert fallen darf, bevor der Regler stark reagiert (Standard 0,5 K). |
| Sollwert für Räume ohne Thermostat | Für Räume, die nur ein Thermometer haben (Standard 22 °C). |
| Vorlauf Minimum / Maximum | Grenzen des Vorlauf-Sollwerts. |
| Max. Änderung pro Stunde | Begrenzung der Änderungsrate. |
| Gewicht Ruhe | Höher = ruhigerer Vorlauf, langsamere Reaktion. |
| Gewicht Energie/Preis | 0 = nur Komfort. Höher = Toleranz zum Sparen nutzen und Wärme in günstige Stunden verschieben. Starte mit 0,2 bis 1. |
| Regelung aktiv | Schaltet die Ausgabe ein und aus. |
| Modell neu lernen / Jetzt berechnen | Tasten. |
| Modell Totzeit, Zeitkonstante Estrich / Raum, Modell Fehler, Modell gelernt | Diagnose. Ein Modellfehler (12-h-RMSE) unter 0,3 K ist gut. |

Die Integration rechnet stündlich um hh:07 und nach jeder Änderung einer Einstellung.
Die Diagnose (Geräteseite → ⋮ → Diagnose herunterladen) enthält das Modell und den letzten Plan.

## Sollwert in ESPHome lesen (Sensor-Modus)

```yaml
sensor:
  - platform: homeassistant
    id: mpc_supply_setpoint
    entity_id: sensor.fbh_mpc_vorlauf_sollwert_ausgang   # an deine Entitäts-ID anpassen
```

Dort, wo deine Firmware den Vorlauf-Sollwert aus der Heizkurve berechnet:

```cpp
float setpoint = curve_setpoint;               // deine bestehende Heizkurve
if (global_api_server->is_connected() && !std::isnan(id(mpc_supply_setpoint).state)) {
  setpoint = id(mpc_supply_setpoint).state;    // MPC übernimmt, solange er einen Wert liefert
}
```

Fällt Home Assistant aus, trennt sich die API-Verbindung; ist die Regelung aus oder das Modell
nicht glaubwürdig, ist der Sensor `unavailable` (NaN). In beiden Fällen gilt wieder die Heizkurve.

Bei Climate- und Zahlen-Entität wird der Sollwert jede Stunde geschrieben, auch wenn er sich nicht
ändert. Die Heizungssteuerung kann das als Lebenszeichen nutzen und nach ~3 Stunden Stille auf
ihre eigene Kurve zurückfallen.

## Dashboard

Das Attribut `plan` lässt sich mit der
[ApexCharts-Card](https://github.com/RomRider/apexcharts-card) darstellen:

```yaml
type: custom:apexcharts-card
header: { show: true, title: Vorlauf-Plan }
graph_span: 36h
span: { start: hour }
yaxis:
  - id: supply
  - id: room
    opposite: true
series:
  - entity: sensor.fbh_mpc_vorlauf_sollwert_berechnet
    name: Vorlauf-Plan
    yaxis_id: supply
    data_generator: |
      return entity.attributes.plan.map(p => [new Date(p.time).getTime(), p.supply]);
  - entity: sensor.fbh_mpc_vorlauf_sollwert_berechnet
    name: Raumprognose
    yaxis_id: room
    data_generator: |
      return entity.attributes.plan.map(p => [new Date(p.time).getTime() + 3600000, p.room]);
```

Passe die Entitäts-ID an deine an.

## Grenzen und Ideen

* Ein Heizkreis pro Eintrag (für mehrere Kreise die Integration mehrfach hinzufügen).
* Daten aus dem normalen Betrieb mit Heizkurve enthalten wenig unabhängige Schwankung des
  Vorlaufs. Ein paar Tage mit manuellen Offset-Sprüngen von ±2 K machen das Modell genauer.
* Die Sonne ist noch keine Eingangsgröße und erscheint als Modellfehler. Geplant: Solarprognose.
* Der Energieterm nutzt die Vorlauftemperatur als Ersatz für die Wärmemenge; COP und
  Pufferspeicher werden nicht modelliert.

## Entwicklung

```
pip install numpy pytest
cd tests && python make_test_data.py && pytest
```

Der numpy-Kern (`model.py`, `mpc.py`, `timeseries.py`) hat keine Home-Assistant-Importe und wird
direkt getestet; `test_coordinator_stubbed.py` lässt den Coordinator gegen ein minimales
nachgebautes Home Assistant laufen.

## Lizenz

MIT
