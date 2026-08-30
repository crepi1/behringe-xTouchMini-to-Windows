# Midi2Sonar — Behringer X-Touch Mini → Windows

Macht aus einem **Behringer X-Touch Mini** eine Steuerzentrale für Windows:
Medientasten, Windows-Lautstärke, **SteelSeries-Sonar**-Kanäle, Lautstärke
einzelner Programme und frei belegbare Tastenkürzel — als GUI-App mit Tray-Icon.

## Warum dieses Tool?

Klassische MIDI-zu-Tastatur-Mapper (z. B. MidiKey2Key) scheitern an den
**Encodern** des X-Touch Mini: Die senden im Standard-Modus **absolute Werte
0–127**, keine relativen Impulse. Mit Tastendruck-Emulation lässt sich daraus
keine Lautstärke regeln. Midi2Sonar geht anders vor: Absolute Encoder-/Fader-Werte
werden **direkt** auf die Ziel-Lautstärke gemappt (0–127 → 0–100 %) — ohne
Tasten-Spam, verzögerungsfrei und exakt.

## Funktionen

- **Gezeichneter X-Touch Mini** im Fenster — jedes Element anklicken und
  konfigurieren. Physische Tastendrücke leuchten im Bild auf (für die Dauer
  des Drucks), der gezeichnete Fader bewegt sich live mit.
- **Layer A und B**: eigene, vollständige Belegung pro Layer. Die Ansicht
  wechselt automatisch zu dem Layer, der gerade am Gerät benutzt wird.
- **Buttons** → Medientasten (Play/Pause, Weiter, Zurück, Stopp, Stumm,
  Lauter, Leiser) oder Tasten **F13–F24**. F13–F24 existieren auf keiner
  normalen Tastatur — sie sind garantiert nirgends belegt und ideal, um sie
  in Discord, OBS & Co. als Hotkey zu registrieren.
- **LED jeder Taste** einstellbar:
  - Aus / Immer an / Blinken
  - **An, wenn Prozess läuft** — z. B. LED an, solange `obs64.exe` läuft.
    So sieht man sofort, ob der per Button gestartete Programm-Shortcut
    funktioniert hat.
- **Encoder (drehen)** — pro Encoder wählbar:
  - **Windows-Master-Lautstärke**
  - **Sonar-Kanal**: Game, Chat, Mikrofon, Media, Aux oder Master
  - **App-Lautstärke**: ein einzelnes Programm über den Windows-Lautstärkemixer
    (z. B. `spotify.exe`)
  - **Tastenkürzel pro Klick**: im Uhrzeigersinn Aktion X, gegen den
    Uhrzeigersinn Aktion Y — z. B. F17/F18 pro Raststufe
- **Encoder (drücken)**: eigene Aktion je Encoder-Push.
- **Fader** → Windows-, Sonar- oder App-Lautstärke (pro Layer).
- **Play/Pause-Schalter**: pausiert alle MIDI-Aktionen temporär; beim Start
  der App immer aktiv. Events werden in der Live-Ansicht weiterhin angezeigt.
- **Autostart**-Checkbox (legt einen Registry-Run-Eintrag an/ab).
- **Tray-Icon**: Fenster schließen = minimieren ins Tray; „Beenden" über das
  Tray-Menü. Nur eine Instanz gleichzeitig möglich.
- **Live-Ansicht**: jedes MIDI-Event mit dem, was daraus emuliert wird
  (grün = ausgeführt, gelb = unbelegt/pausiert, rot = Fehler).
- **Robust**: automatischer Reconnect bei ab-/angestecktem Controller und
  bei Neustart von SteelSeries GG (Sonar-Port wird neu ermittelt).

## Installation

### Fertige exe

1. `Midi2Sonar.exe` (aus einem Release oder selbst gebaut, s. u.) in einen
   beliebigen Ordner legen.
2. Starten. Die Konfiguration wird als `config.json` **neben der exe**
   angelegt und gepflegt.
3. Optional: Häkchen bei „Mit Windows starten".

### Aus dem Quellcode

```
pip install -r requirements.txt
python Midi2Sonar.py
```

Voraussetzungen: Python 3.11+ unter Windows, SteelSeries GG mit Sonar
(nur nötig, wenn Sonar-Kanäle verwendet werden).

**Wichtig:** MidiKey2Key oder andere MIDI-Programme dürfen **nicht
gleichzeitig** laufen — Windows erlaubt nur einem Programm den Zugriff auf
ein MIDI-Gerät.

## Bedienung

| Element im Fenster | Klick öffnet |
|---|---|
| Encoder (Kreis) | Dialog: Drehen (Lautstärke-Ziel oder Tastenkürzel) + Drücken (Aktion) |
| Button (Rechteck) | Dialog: Aktion + LED-Modus |
| Fader | Dialog: Lautstärke-Ziel |

- **Layer-Umschalter** über der Abbildung: zeigt/bearbeitet Layer A oder B.
  Die Ansicht folgt automatisch dem am Gerät aktiven Layer (erkannt an den
  gesendeten CC-/Notennummern).
- **App-Lautstärke**: Die Auswahlliste zeigt alle Programme mit einer
  Windows-Audio-Session. Eine App taucht dort auf, sobald sie einmal Ton
  ausgegeben hat. Manuelle Eingabe (z. B. `firefox.exe`) geht immer.
- **LED „an wenn Prozess läuft"**: Prozessname wählen (Liste über Button
  aktualisierbar) — wird alle 2 Sekunden geprüft.

## config.json — Format

Liegt neben der exe (bzw. neben `Midi2Sonar.py`). Wird von der GUI gepflegt,
kann aber auch von Hand editiert werden (App danach neu starten). Älteres
Format wird automatisch migriert.

```json
{
  "midi_port": "X-TOUCH MINI",
  "layers": {
    "A": {
      "fader": { "mode": "windows" },
      "encoders": {
        "1": { "mode": "sonar", "channel": "game" },
        "6": { "mode": "app", "process": "spotify.exe" },
        "8": { "mode": "hotkey", "cw": "f17", "ccw": "f18" }
      },
      "buttons": {
        "18": { "action": "prev" },
        "22": { "action": "play_pause",
                "led": { "mode": "process", "process": "obs64.exe" } }
      }
    },
    "B": { "fader": { "mode": "off" }, "encoders": {}, "buttons": {} }
  }
}
```

- `midi_port`: Teilstring des MIDI-Portnamens (Groß-/Kleinschreibung egal).
- Encoder-/Fader-`mode`: `off`, `windows`, `sonar` (+ `channel`),
  `app` (+ `process`), `hotkey` (+ `cw`/`ccw`, nur Encoder).
- Sonar-`channel`: `game`, `chatRender` (Chat), `chatCapture` (Mikrofon),
  `media`, `aux`, `master`.
- Button-`action` / Hotkey-Aktionen: `f13` … `f24`, `play_pause`, `next`,
  `prev`, `stop`, `mute`, `vol_up`, `vol_down`, `none`.
- LED-`mode`: `off`, `on`, `blink`, `process` (+ `process`).

## MIDI-Referenz (X-Touch Mini, Standard-Modus, MIDI-Kanal 11)

| Element | Layer A | Layer B |
|---|---|---|
| Encoder drehen | CC 1–8 (absolut 0–127) | CC 11–18 |
| Encoder drücken | Noten 0–7 | Noten 24–31 |
| Obere Button-Reihe | Noten 8–15 | Noten 32–39 |
| Untere Button-Reihe | Noten 16–23 | Noten 40–47 |
| Fader | CC 9 | CC 10 |

LED-Steuerung (Richtung PC → Gerät): Note-On auf **Kanal 11** mit dem
**Tasten-Index 0–15** als Note (0–7 = obere Reihe, 8–15 = untere Reihe —
**nicht** die Sende-Notennummern!), Velocity **0** = aus, **1** = an,
**2** = blinken. Da es nur 16 physische LEDs gibt, zeigt die App immer die
LED-Belegung des zuletzt am Gerät benutzten Layers. Achtung: Unter Windows
heißt der MIDI-Eingang z. B. `X-TOUCH MINI 0`, der Ausgang aber
`X-TOUCH MINI 1` — die App sucht beide getrennt per Teilstring.

## Technische Details

- **SteelSeries-Sonar-API**: Die App liest
  `C:\ProgramData\SteelSeries\SteelSeries Engine 3\coreProps.json`
  (`ggEncryptedAddress`), fragt `https://{addr}/subApps` ab (self-signed
  Zertifikat) und erhält daraus die Sonar-Webserver-Adresse. Lautstärke setzen:
  `PUT {base}/volumeSettings/classic/{channel}/Volume/{0.0–1.0}`.
  Der Port ändert sich bei jedem GG-Start und wird bei Verbindungsfehlern
  automatisch neu ermittelt.
- **Windows-Lautstärke**: `pycaw` (`AudioUtilities.GetSpeakers().EndpointVolume`,
  `SetMasterVolumeLevelScalar`). App-Lautstärken über die Audio-Sessions
  (`AudioUtilities.GetAllSessions()` → `SimpleAudioVolume`).
- **Tastenemulation**: `keybd_event` mit Virtual-Key-Codes (Medientasten
  `0xAD–0xB3`, F13–F24 `0x7C–0x87`).
- **Hotkey-Encoder**: Da die Encoder absolute Werte senden, wird die Richtung
  aus dem Delta zum letzten Wert bestimmt (±1…n pro Ereignis = Anzahl der
  Tastendrücke). Am Anschlag sendet der X-Touch Mini bei jeder weiteren
  Raststufe denselben Wert erneut — jede Wiederholung von **0** zählt als
  Klick gegen den, jede Wiederholung von **127** als Klick im Uhrzeigersinn.
  Dadurch ist der Modus endlos drehbar. (Empfangene CC-Werte übernimmt das
  Gerät übrigens *nicht* als neuen Encoder-Stand — ein „Reset per Rückkanal"
  funktioniert nicht.)
- **Drossel**: Schnelle Dreh-/Faderbewegungen werden koalesziert (nur der
  jeweils letzte Wert wird gesetzt, max. alle 40 ms pro Ziel), damit weder die
  Sonar-HTTP-API noch die Audio-API geflutet wird.
- Ereignisfluss: MIDI-Thread → Queue → Tkinter-Poll (40 ms) für Log,
  Aufleuchten und Fader-Animation.

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| „Controller nicht gefunden" | Läuft MidiKey2Key o. Ä. noch? Nur ein Programm bekommt das MIDI-Gerät. USB neu anstecken — die App verbindet sich alle 3 s neu. |
| Sonar reagiert nicht | Läuft SteelSeries GG mit Sonar? Fehlermeldung erscheint in der Live-Ansicht; die App sucht die Adresse bei jedem Fehler neu. |
| App-Lautstärke „keine Audio-Session" | Das Zielprogramm muss seit dem Windows-Start mindestens einmal Ton ausgegeben haben (dann erscheint es im Lautstärkemixer). |
| Buttons senden andere Noten | Die Live-Ansicht zeigt jede eingehende Note → Element im Bild entsprechend belegen. Falls das Gerät umkonfiguriert wurde: mit dem Behringer X-Touch-Editor auf Werkszustand (Standard-Modus) zurücksetzen. |
| LEDs bleiben dunkel | LEDs funktionieren nur im Standard-Modus (nicht im MC-Modus) und nur solange die App mit dem Gerät verbunden ist. |

## Exe selbst bauen

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Midi2Sonar --icon icon.ico --hidden-import mido.backends.rtmidi Midi2Sonar.py
```

Ergebnis: `dist\Midi2Sonar.exe` — portabel, keine Installation nötig.
