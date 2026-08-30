# Midi2Sonar — Behringer X-Touch Mini → Windows

Macht aus einem **Behringer X-Touch Mini** eine Steuerzentrale für Windows:
Medientasten, eigene Tastenkürzel (z. B. `strg+c`), Windows-Lautstärke,
**SteelSeries-Sonar**-Kanäle und Lautstärke einzelner Programme — als GUI-App
mit Tray-Icon. Unterstützt **beide Betriebsarten** des Geräts (Standard und
Mackie/MC) mit automatischer Erkennung.

## Warum dieses Tool?

Klassische MIDI-zu-Tastatur-Mapper (z. B. MidiKey2Key) scheitern an den
**Encodern** des X-Touch Mini: Die senden im Standard-Modus **absolute Werte
0–127**, keine relativen Impulse. Midi2Sonar mappt absolute Werte direkt auf
die Ziel-Lautstärke — und im MC-Modus nutzt es die relativen Encoder plus
steuerbare LED-Ringe für ein noch besseres Setup.

## Standard- vs. MC-Modus

Umschalten: Gerät mit gedrückter **MC-Taste** (Layer-A-Taste) einstecken.
Die App erkennt den Modus automatisch an den MIDI-Nachrichten.

| | Standard-Modus | MC-Modus (Mackie) |
|---|---|---|
| Layer A/B | ✔ zwei komplette Belegungen | ✖ eine Ebene |
| Buttons | 16 je Layer | 16 + die 2 Layer-Tasten = 18 |
| Encoder | absolut (0–127 → 0–100 %) | **relativ** (±x % pro Klick, einstellbar) |
| Zurückspringen nach externen Änderungen | ja (prinzipbedingt) | **nein** |
| Encoder-Ringe | zeigen nur den internen Encoder-Stand, von außen **nicht** steuerbar | **von der App gesteuert** → zeigen echten Sonar-/Windows-/App-Stand (alle 2 s abgeglichen), Anzeigestil wählbar |
| Button-LEDs | ✔ | ✔ |
| Fader | 7 Bit (128 Stufen) | **14 Bit** (Pitch-Bend, 16 384 Stufen) |

## Funktionen

- **Gezeichneter X-Touch Mini** im Fenster — Element anklicken und
  konfigurieren. Physische Tastendrücke leuchten im Bild auf, der gezeichnete
  Fader bewegt sich live mit. Ansicht (Layer A / B / MC) folgt dem Gerät
  automatisch.
- **Aktionen** für Buttons, Encoder-Push und Encoder-Hotkeys:
  - Medientasten (Play/Pause, Weiter, Zurück, Stopp, Stumm, Lauter, Leiser)
  - Tasten **F13–F24** (existieren auf keiner Tastatur → ideal zum Binden in
    Discord, OBS & Co.)
  - **Eigene Tastenkürzel**: z. B. `strg+c`, `strg+shift+esc`, `win+d`,
    `alt+f4`, `strg+alt+entf`. Modifier: `strg`/`ctrl`, `shift`/`umschalt`,
    `alt`, `win`; Tasten: Buchstaben/Ziffern/Zeichen oder Namen (`enter`,
    `esc`, `tab`, `entf`, `einfg`, `pos1`, `ende`, `bildauf`, `bildab`,
    `hoch`, `runter`, `links`, `rechts`, `druck`, `f1`–`f24` …)
- **Encoder (drehen)** — pro Encoder: Windows-Master, Sonar-Kanal (Game,
  Chat, Mikrofon, Media, Aux, Master), App-Lautstärke (z. B. `spotify.exe`)
  oder Tastenkürzel pro Klick (↻/↺ getrennt belegbar).
  Im MC-Modus zusätzlich pro Encoder: **Schrittweite** (1–25 % pro Raststufe)
  und **Ring-Anzeigestil** (Punkt, Pan, Füllstand, Spread).
- **LED jeder Taste**: Aus / Immer an / Blinken / **An, wenn Prozess läuft**
  (z. B. LED an, solange `obs64.exe` läuft).
- **Play/Pause-Schalter**: pausiert alle MIDI-Aktionen temporär; beim Start
  immer aktiv.
- **Autostart**-Checkbox — startet die App beim Windows-Start **minimiert im
  Tray** (`--minimized`).
- **Tray-Icon**: Fenster schließen = minimieren ins Tray; „Beenden" über das
  Tray-Menü. Nur eine Instanz gleichzeitig.
- **Live-Ansicht**: jedes MIDI-Event mit dem, was daraus emuliert wird.
- **Robust**: Watchdog erkennt abgezogene Geräte (Windows meldet das nicht von
  selbst!) und verbindet automatisch neu; Sonar-Port wird bei GG-Neustart neu
  ermittelt.

## Installation

### Fertige exe

`Midi2Sonar.exe` in einen beliebigen Ordner legen und starten. Die
Konfiguration liegt als `config.json` neben der exe. Autostart über die
Checkbox in der App.

### Aus dem Quellcode

```
pip install -r requirements.txt
python Midi2Sonar.py
```

**Wichtig:** MidiKey2Key oder andere MIDI-Programme dürfen **nicht
gleichzeitig** laufen — Windows erlaubt nur einem Programm den Zugriff.

## Bedienung

- Element im Gerätebild anklicken → Dialog (Aktion, LED, Lautstärke-Ziel,
  Ring-Stil, Schrittweite je nach Element/Modus).
- **MC-Tastenbelegung lernen:** Die üblichen MC-Notennummern sind vorbelegt.
  Leuchtet ein gedrückter Button im Bild nicht auf, stimmt die Nummer nicht:
  Button im Bild anklicken → „Lernen" → physische Taste drücken.
- **App-Lautstärke:** Liste zeigt Programme mit Windows-Audio-Session (App
  muss einmal Ton ausgegeben haben); manuelle Eingabe geht immer.

## config.json — Format

Wird von der GUI gepflegt; ältere Formate werden automatisch migriert.

```json
{
  "midi_port": "X-TOUCH MINI",
  "layers": {
    "A": {
      "fader": { "mode": "windows" },
      "encoders": { "1": { "mode": "sonar", "channel": "game" } },
      "buttons": { "22": { "action": "play_pause",
                           "led": { "mode": "process", "process": "obs64.exe" } } }
    },
    "B": { "fader": { "mode": "off" }, "encoders": {}, "buttons": {} }
  },
  "mc": {
    "fader": { "mode": "windows" },
    "encoders": { "1": { "mode": "sonar", "channel": "game",
                         "ring": "fan", "step": 2 },
                  "8": { "mode": "hotkey", "cw": "strg+c", "ccw": "strg+v" } },
    "buttons": { "89": { "action": "prev" } },
    "notes": { "push": [32,33,34,35,36,37,38,39],
               "top": [89,90,40,41,42,43,44,45],
               "bot": [87,88,91,92,86,93,94,95],
               "extra": [84,85] }
  }
}
```

- Encoder-/Fader-`mode`: `off`, `windows`, `sonar` (+ `channel`),
  `app` (+ `process`), `hotkey` (+ `cw`/`ccw`, nur Encoder).
- MC-Encoder zusätzlich: `ring` (`dot`, `pan`, `fan`, `spread`),
  `step` (1–25, % pro Raststufe).
- `action`/`cw`/`ccw`: vordefinierte Aktion (`f13`…`f24`, `play_pause`, `next`,
  `prev`, `stop`, `mute`, `vol_up`, `vol_down`, `none`) **oder** eigenes
  Kürzel als String (`"strg+c"`).
- LED-`mode`: `off`, `on`, `blink`, `process` (+ `process`).
- `mc.notes`: gezeichnete Position → MIDI-Note (per „Lernen" gepflegt).

## MIDI-Referenz (empirisch verifiziert)

**Standard-Modus** (Kanal 11):

| Element | Layer A | Layer B |
|---|---|---|
| Encoder drehen | CC 1–8 (absolut) | CC 11–18 |
| Encoder drücken | Noten 0–7 | Noten 24–31 |
| Obere/untere Button-Reihe | Noten 8–15 / 16–23 | Noten 32–39 / 40–47 |
| Fader | CC 9 | CC 10 |

- Button-LEDs (PC → Gerät): Note-On auf **Kanal 11** mit **Tasten-Index 0–15**
  (0–7 obere, 8–15 untere Reihe — *nicht* die Sende-Noten!),
  Velocity 0/1/2 = aus/an/blinken. Es gibt nur 16 physische LEDs; die App
  zeigt immer die Belegung des aktiven Layers.
- Encoder-Ringe: von außen **nicht** steuerbar (Vollsweep über alle Kanäle ×
  alle CCs blieb wirkungslos). Das Gerät übernimmt empfangene CC-Werte auch
  nicht als Encoder-Stand. Am Anschlag sendet es bei jedem Klick 0 bzw. 127
  erneut — so erkennt die App dort trotzdem die Drehrichtung (Hotkey-Modus).

**MC-Modus** (Kanal 1):

- Encoder drehen: CC 16–23, relativ (1–7 = rechts, 65–71 = links, Wert = Klicks)
- Encoder drücken: Noten 32–39; Buttons: MC-Noten (s. o., per „Lernen" anpassbar)
- Fader: Pitch-Bend (14 Bit)
- Ringe (PC → Gerät): CC 48–55, Wert = Stil-Bits (0x00 Punkt / 0x10 Pan /
  0x20 Füllstand / 0x30 Spread) | Position 0–11
- Button-LEDs: Note-Echo, Velocity 127 = an, 1 = blinken, 0 = aus

Windows-Eigenheit: MIDI-**Eingang** heißt z. B. `X-TOUCH MINI 0`, der
**Ausgang** `X-TOUCH MINI 1` — beide werden getrennt per Teilstring gesucht.
Abgezogene Geräte meldet Windows nicht; die App pollt deshalb nicht-blockierend
und prüft alle 2 s die Portliste (Auto-Reconnect).

## Exe selbst bauen

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Midi2Sonar --icon icon.ico --hidden-import mido.backends.rtmidi Midi2Sonar.py
```

Ergebnis: `dist\Midi2Sonar.exe` (portabel).
