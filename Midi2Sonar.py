"""
Midi2Sonar - X-Touch Mini -> Windows Medientasten / Windows-Lautstaerke /
SteelSeries Sonar / App-Lautstaerken / Tastenkuerzel

GUI mit Tray-Icon und gezeichnetem X-Touch Mini. Unterstuetzt beide
Betriebsarten des Geraets und erkennt sie automatisch an den Nachrichten:

Standard-Modus (Kanal 11):
- Layer A/B mit eigener Belegung, absolute Encoder (0-127)
- Button-LEDs: Note-On Kanal 11, Tasten-Index 0-15, Velocity 0/1/2
- Encoder-Ringe NICHT von aussen steuerbar

MC-Modus (Mackie Control, Kanal 1):
- kein Layer, dafuer 18 Buttons (inkl. der beiden Layer-Tasten)
- relative Encoder (kein Zuruespringen nach externen Aenderungen)
- Encoder-Ringe von der App gesteuert -> zeigen echten Lautstaerke-Stand
  (wird alle 2 s mit Sonar/Windows/App abgeglichen)
- Fader mit 14-Bit-Aufloesung (Pitch-Bend)
"""

import ctypes
import json
import queue
import sys
import threading
import time
import winreg
from pathlib import Path

import mido
import psutil
import requests
import urllib3
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import pystray
from PIL import Image, ImageDraw
from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities

urllib3.disable_warnings()

APP_NAME = "Midi2Sonar"

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_PATH = BASE_DIR / "config.json"
COREPROPS = Path(r"C:\ProgramData\SteelSeries\SteelSeries Engine 3\coreProps.json")

MIDI_CHANNEL = 10  # Standard-Modus: sendet auf MIDI-Kanal 11 (0-basiert 10)
LED_CHANNEL = 10   # Standard-Modus Button-LEDs: Kanal 11, Noten 0-15 = Tasten-Index
                   # (0-7 obere Reihe, 8-15 untere Reihe) - empirisch ermittelt

# Standard-Modus: zwei Layer mit getrennten CC-/Notennummern
LAYERS = {
    "A": {
        "enc_ccs": list(range(1, 9)),      # Encoder drehen (absolut)
        "fader_cc": 9,
        "push_notes": list(range(0, 8)),   # Encoder druecken
        "top_notes": list(range(8, 16)),   # obere Button-Reihe
        "bot_notes": list(range(16, 24)),  # untere Button-Reihe
    },
    "B": {
        "enc_ccs": list(range(11, 19)),
        "fader_cc": 10,
        "push_notes": list(range(24, 32)),
        "top_notes": list(range(32, 40)),
        "bot_notes": list(range(40, 48)),
    },
}

# MC-Modus (Mackie Control): Kanal 1 (0-basiert 0)
MC_CHANNEL = 0
MC_ENC_CCS = list(range(16, 24))    # relative Encoder: 1-7 = rechts, 65-71 = links
MC_RING_CCS = list(range(48, 56))   # Ring-Anzeige Encoder 1-8
REL_STEP_DEFAULT = 2                # % Lautstaerke pro Raststufe im MC-Modus
RING_STYLES = {"dot": 0x00, "pan": 0x10, "fan": 0x20, "spread": 0x30}
RING_STYLE_LABELS = {"dot": "Punkt", "pan": "Pan (von Mitte)",
                     "fan": "Füllstand", "spread": "Spread (symmetrisch)"}
RING_LABEL_TO_STYLE = {v: k for k, v in RING_STYLE_LABELS.items()}
# Uebliche MC-Notennummern des X-Touch Mini (per "Lernen" im Dialog korrigierbar)
MC_DEFAULT_NOTES = {
    "push": [32, 33, 34, 35, 36, 37, 38, 39],
    "top": [89, 90, 40, 41, 42, 43, 44, 45],
    "bot": [87, 88, 91, 92, 86, 93, 94, 95],
    "extra": [84, 85],              # die beiden Layer-Tasten
}


def layer_of_note(note: int):
    return "A" if note < 24 else ("B" if note < 48 else None)


def led_index_of_note(note: int):
    """Standard-Modus: Sende-Note eines Buttons -> physischer LED-Index 0-15."""
    layer = layer_of_note(note)
    if layer is None:
        return None
    L = LAYERS[layer]
    if note in L["top_notes"]:
        return L["top_notes"].index(note)          # 0-7
    if note in L["bot_notes"]:
        return 8 + L["bot_notes"].index(note)      # 8-15
    return None  # Encoder-Push hat keine Button-LED


def classify_cc(cc: int):
    """Standard-Modus: -> ("fader"|"enc", layer) oder None"""
    for layer, L in LAYERS.items():
        if cc == L["fader_cc"]:
            return ("fader", layer)
        if cc in L["enc_ccs"]:
            return ("enc", layer)
    return None


# ------------------------------------------------------------------ Aktionen

VK = {
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
    "mute": 0xAD, "vol_up": 0xAF, "vol_down": 0xAE,
}
for i in range(12):  # F13 (0x7C) bis F24 (0x87)
    VK[f"f{13 + i}"] = 0x7C + i

ACTION_LABELS = {
    "none": "— keine —",
    **{f"f{n}": f"Taste F{n}" for n in range(13, 25)},
    "play_pause": "Medien: Play/Pause",
    "next": "Medien: Weiter",
    "prev": "Medien: Zurück",
    "stop": "Medien: Stopp",
    "mute": "Medien: Stumm",
    "vol_up": "Medien: Lauter",
    "vol_down": "Medien: Leiser",
}
LABEL_TO_ACTION = {v: k for k, v in ACTION_LABELS.items()}

SHORT_LABELS = {
    "none": "", "play_pause": "Play/Pau", "next": "Weiter", "prev": "Zurück",
    "stop": "Stopp", "mute": "Stumm", "vol_up": "Lauter", "vol_down": "Leiser",
    **{f"f{n}": f"F{n}" for n in range(13, 25)},
}

SONAR_NAMES = {
    "game": "Game", "chatRender": "Chat", "chatCapture": "Mikrofon",
    "media": "Media", "aux": "Aux", "master": "Master",
}
SONAR_LABEL_TO_KEY = {v: k for k, v in SONAR_NAMES.items()}

LED_MODES = {"off": "Aus", "on": "Immer an", "blink": "Blinken",
             "process": "An, wenn Prozess läuft:"}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

# Eigene Tastenkuerzel wie "strg+c" oder "win+d"
MOD_VKS = {"strg": 0x11, "ctrl": 0x11, "control": 0x11,
           "shift": 0x10, "umschalt": 0x10,
           "alt": 0x12, "win": 0x5B, "windows": 0x5B}

NAMED_VKS = {
    "enter": 0x0D, "eingabe": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "leertaste": 0x20, "backspace": 0x08,
    "del": 0x2E, "delete": 0x2E, "entf": 0x2E,
    "ins": 0x2D, "insert": 0x2D, "einfg": 0x2D,
    "home": 0x24, "pos1": 0x24, "end": 0x23, "ende": 0x23,
    "pageup": 0x21, "bildauf": 0x21, "pagedown": 0x22, "bildab": 0x22,
    "up": 0x26, "hoch": 0x26, "down": 0x28, "runter": 0x28,
    "left": 0x25, "links": 0x25, "right": 0x27, "rechts": 0x27,
    "druck": 0x2C, "print": 0x2C, "pause": 0x13,
}
for _n in range(1, 25):
    NAMED_VKS[f"f{_n}"] = 0x6F + _n

EXTENDED_VKS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27}


def parse_combo(spec: str):
    """'strg+shift+c' -> (modifier_vks, key_vk) oder None bei ungueltiger Eingabe."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    key, mods = parts[-1], parts[:-1]
    mod_vks = []
    for m in mods:
        if m not in MOD_VKS:
            return None
        if MOD_VKS[m] not in mod_vks:
            mod_vks.append(MOD_VKS[m])
    if key in NAMED_VKS:
        kvk = NAMED_VKS[key]
    elif len(key) == 1:
        res = ctypes.windll.user32.VkKeyScanW(ord(key))
        if res == -1:
            return None
        kvk = res & 0xFF
        if res & 0x100 and 0x10 not in mod_vks:  # Zeichen braucht Shift
            mod_vks.append(0x10)
    else:
        return None
    return (mod_vks, kvk)


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def press_key(action: str, times: int = 1) -> bool:
    """Fuehrt eine Aktion aus: vordefiniert oder eigenes Kuerzel ('strg+c')."""
    kbd = ctypes.windll.user32.keybd_event
    vk = VK.get(action)
    if vk is not None:
        for _ in range(times):
            kbd(vk, 0, 0, 0)
            kbd(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    parsed = parse_combo(action)
    if parsed is None:
        return False
    mods, kvk = parsed
    flag = KEYEVENTF_EXTENDEDKEY if kvk in EXTENDED_VKS else 0
    for _ in range(times):
        for m in mods:
            kbd(m, 0, 0, 0)
        kbd(kvk, 0, flag, 0)
        kbd(kvk, 0, flag | KEYEVENTF_KEYUP, 0)
        for m in reversed(mods):
            kbd(m, 0, KEYEVENTF_KEYUP, 0)
    return True


# ------------------------------------------------------------------ Autostart

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    return f'"{pythonw}" "{Path(__file__).resolve()}" --minimized'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


# ------------------------------------------------------------ Lautstaerke-Ziele

class WindowsVolume:
    def __init__(self):
        self._endpoint = None

    def _get(self):
        if self._endpoint is None:
            self._endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        return self._endpoint

    def set_scalar(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        try:
            self._get().SetMasterVolumeLevelScalar(value, None)
        except Exception:
            self._endpoint = None
            self._get().SetMasterVolumeLevelScalar(value, None)

    def get_scalar(self) -> float:
        try:
            return self._get().GetMasterVolumeLevelScalar()
        except Exception:
            self._endpoint = None
            return self._get().GetMasterVolumeLevelScalar()


class Sonar:
    def __init__(self):
        self._base = None
        self._lock = threading.Lock()

    def _discover(self) -> str:
        props = json.loads(COREPROPS.read_text())
        gg = props["ggEncryptedAddress"]
        r = requests.get(f"https://{gg}/subApps", verify=False, timeout=3)
        addr = r.json()["subApps"]["sonar"]["metadata"]["webServerAddress"]
        if not addr:
            raise RuntimeError("Sonar-Adresse leer - laeuft Sonar?")
        return addr

    def _request(self, method, path):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._base is None:
                        self._base = self._discover()
                    r = requests.request(method, self._base + path, timeout=2)
                    r.raise_for_status()
                    return r
                except Exception as e:
                    self._base = None
                    if attempt == 2:
                        raise RuntimeError(f"Sonar nicht erreichbar: {e}") from e

    def set_volume(self, channel: str, value: float) -> None:
        value = max(0.0, min(1.0, value))
        self._request("PUT", f"/volumeSettings/classic/{channel}/Volume/{value:.4f}")

    def get_volumes(self) -> dict:
        """-> {kanal: volume 0..1} inkl. 'master'"""
        data = self._request("GET", "/volumeSettings/classic").json()
        out = {"master": data.get("masters", {}).get("classic", {}).get("volume", 0.0)}
        for ch, d in data.get("devices", {}).items():
            out[ch] = d.get("classic", {}).get("volume", 0.0)
        return out

    def get_volume(self, channel: str) -> float:
        return self.get_volumes().get(channel, 0.0)


class AppVolume:
    """Regelt die Lautstaerke einzelner Programme ueber deren Windows-Audio-Session."""

    TTL = 5.0

    def __init__(self):
        self._cache = {}  # exe-name (lower) -> (zeit, [SimpleAudioVolume])

    def _sessions_for(self, name: str):
        vols = []
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process and s.Process.name().lower() == name:
                    vols.append(s.SimpleAudioVolume)
            except Exception:
                pass
        return vols

    def _vols(self, process: str):
        name = process.lower()
        now = time.time()
        cached = self._cache.get(name)
        vols = cached[1] if cached and now - cached[0] < self.TTL else None
        if not vols:
            vols = self._sessions_for(name)
            self._cache[name] = (now, vols)
        if not vols:
            raise RuntimeError(f"{process}: keine Audio-Session (App muss mal Ton ausgeben)")
        return vols

    def set(self, process: str, value: float) -> None:
        value = max(0.0, min(1.0, value))
        try:
            for v in self._vols(process):
                v.SetMasterVolume(value, None)
        except RuntimeError:
            raise
        except Exception:
            self._cache.pop(process.lower(), None)
            for v in self._vols(process):
                v.SetMasterVolume(value, None)

    def get(self, process: str) -> float:
        try:
            return self._vols(process)[0].GetMasterVolume()
        except RuntimeError:
            raise
        except Exception:
            self._cache.pop(process.lower(), None)
            return self._vols(process)[0].GetMasterVolume()


def list_audio_apps() -> list:
    """Alle Programme, die aktuell eine Audio-Session haben (= regelbar sind)."""
    names = set()
    try:
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process:
                    names.add(s.Process.name())
            except Exception:
                pass
    except Exception:
        pass
    return sorted(names, key=str.lower)


def list_processes() -> list:
    names = set()
    for p in psutil.process_iter(["name"]):
        n = p.info.get("name")
        if n and n.lower().endswith(".exe"):
            names.add(n)
    return sorted(names, key=str.lower)


# ------------------------------------------- Koaleszierung schneller Drehungen

class VolumeDispatcher:
    """Arbeiter-Thread: absolute Werte werden koalesziert (letzter gewinnt),
    relative Deltas aufsummiert - so wird keine API geflutet."""

    INTERVAL = 0.04

    def __init__(self, on_error):
        self._pending = {}  # key -> (func, value, is_add)
        self._cond = threading.Condition()
        self._on_error = on_error
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, key, func, value):
        with self._cond:
            self._pending[key] = (func, value, False)
            self._cond.notify()

    def submit_add(self, key, func, delta):
        with self._cond:
            prev = self._pending.get(key)
            if prev and prev[2]:
                delta += prev[1]
            self._pending[key] = (func, delta, True)
            self._cond.notify()

    def _run(self):
        CoInitialize()
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                items, self._pending = self._pending, {}
            for func, value, _ in items.values():
                try:
                    func(value)
                except Exception as e:
                    self._on_error(str(e))
            time.sleep(self.INTERVAL)


# ------------------------------------------------------------------- Config

EMPTY_LAYER = {"fader": {"mode": "off"}, "encoders": {}, "buttons": {}}

DEFAULT_SONAR_ENCODERS = {
    "1": {"mode": "sonar", "channel": "game"},
    "2": {"mode": "sonar", "channel": "chatRender"},
    "3": {"mode": "sonar", "channel": "media"},
    "4": {"mode": "sonar", "channel": "aux"},
    "5": {"mode": "sonar", "channel": "chatCapture"},
}

DEFAULT_CONFIG = {
    "midi_port": "X-TOUCH MINI",
    "release_apps": [],
    "layers": {
        "A": {
            "fader": {"mode": "windows"},
            "encoders": json.loads(json.dumps(DEFAULT_SONAR_ENCODERS)),
            "buttons": {
                "18": {"action": "prev"}, "22": {"action": "play_pause"},
                "19": {"action": "next"}, "21": {"action": "stop"},
                "23": {"action": "mute"},
            },
        },
        "B": json.loads(json.dumps(EMPTY_LAYER)),
    },
    "mc": {
        "fader": {"mode": "windows"},
        "encoders": json.loads(json.dumps(DEFAULT_SONAR_ENCODERS)),
        "buttons": {},
        "notes": json.loads(json.dumps(MC_DEFAULT_NOTES)),
    },
}


def _migrate(cfg: dict) -> dict:
    """Aeltere Formate -> aktuelles Format mit layers + mc."""
    if "layers" not in cfg:
        layer_a = {
            "fader": cfg.pop("fader", None) or {"mode": "windows"},
            "encoders": {}, "buttons": {},
        }
        for k, v in cfg.pop("encoders", {}).items():
            layer_a["encoders"][k] = {"mode": "sonar", "channel": v} if isinstance(v, str) else v
        for k, v in cfg.pop("buttons", {}).items():
            layer_a["buttons"][k] = {"action": v} if isinstance(v, str) else v
        cfg.pop("fader_cc", None)
        cfg["layers"] = {"A": layer_a, "B": json.loads(json.dumps(EMPTY_LAYER))}
    for name in ("A", "B"):
        layer = cfg["layers"].setdefault(name, json.loads(json.dumps(EMPTY_LAYER)))
        layer.setdefault("fader", {"mode": "off"})
        layer.setdefault("encoders", {})
        layer.setdefault("buttons", {})
    mc = cfg.setdefault("mc", json.loads(json.dumps(DEFAULT_CONFIG["mc"])))
    mc.setdefault("fader", {"mode": "off"})
    mc.setdefault("encoders", {})
    mc.setdefault("buttons", {})
    notes = mc.setdefault("notes", json.loads(json.dumps(MC_DEFAULT_NOTES)))
    for k, v in MC_DEFAULT_NOTES.items():
        notes.setdefault(k, list(v))
    cfg.setdefault("release_apps", [])
    return cfg


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("midi_port", "X-TOUCH MINI")
        return _migrate(cfg)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ------------------------------------------------------------------ MIDI-Loop

class _Released(Exception):
    """Internes Signal: MIDI-Ports fuer ein anderes Programm freigeben."""


class MidiBridge(threading.Thread):
    """Hintergrund-Thread: verbindet sich (und re-connectet) mit dem Controller.
    Erkennt Standard- vs. MC-Modus automatisch am MIDI-Kanal der Nachrichten."""

    def __init__(self, cfg: dict, events: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.lock = threading.Lock()
        self.connected = False
        self.enabled = True                     # globales Play/Pause
        self.release_for = None                 # Prozessname -> Ports freigeben
        self.released = False                   # Ports sind aktuell freigegeben
        self.active_layer = "A"                 # Standard-Modus: aktiver Layer
        self.hw_mode = "standard"               # "standard" | "mc"
        self.mc_learn = False                   # naechste MC-Note geht an die GUI
        self.win_vol = WindowsVolume()
        self.sonar = Sonar()
        self.app_vol = AppVolume()
        self._out = None
        self._enc_state = {}                    # Standard-Hotkey: cc -> letzter Wert
        self.vol_cache = {}                     # MC relativ: key -> (zeit, wert)
        self.dispatcher = VolumeDispatcher(
            on_error=lambda msg: self._emit("log", "err", msg))

    # ------------------------------------------------------------- Hilfsmittel

    def _emit(self, kind, *payload):
        self.events.put((kind, payload))

    def layer_cfg(self, layer: str) -> dict:
        return self.cfg["layers"][layer]

    def mc_cfg(self) -> dict:
        return self.cfg["mc"]

    def set_led(self, note: int, state: int):
        """state: 0=aus, 1=an, 2=blinken. note = Sende-Note des Buttons."""
        if self._out is None:
            return
        try:
            if self.hw_mode == "mc":
                vel = {0: 0, 1: 127, 2: 1}.get(state, 0)  # MC: 127=an, 1=blinken
                self._out.send(mido.Message(
                    "note_on", channel=MC_CHANNEL, note=note, velocity=vel))
            else:
                idx = led_index_of_note(note)
                if idx is None:
                    return
                self._out.send(mido.Message(
                    "note_on", channel=LED_CHANNEL, note=idx, velocity=state))
        except Exception:
            pass

    def set_ring(self, idx: int, norm: float):
        """MC-Modus: Ring von Encoder idx (1-8) auf Lautstaerke 0..1 setzen."""
        if self._out is None or self.hw_mode != "mc":
            return
        with self.lock:
            style = self.mc_cfg().get("encoders", {}).get(str(idx), {}).get("ring", "fan")
        norm = max(0.0, min(1.0, norm))
        pos = 0 if norm <= 0.001 else 1 + round(norm * 10)   # 1-11
        try:
            self._out.send(mido.Message(
                "control_change", channel=MC_CHANNEL,
                control=MC_RING_CCS[idx - 1],
                value=RING_STYLES.get(style, 0x20) | pos))
        except Exception:
            pass

    def _led_state_for(self, b: dict, running_procs) -> int:
        led = b.get("led", {"mode": "off"})
        mode = led.get("mode", "off")
        if mode == "on":
            return 1
        if mode == "blink":
            return 2
        if mode == "process":
            proc = led.get("process", "").lower()
            return 1 if (running_procs is not None and proc in running_procs) else 0
        return 0

    def apply_leds(self, running_procs=None):
        """LED-Zustaende des aktiven Modus/Layers senden."""
        if self._out is None:
            return
        if self.hw_mode == "mc":
            with self.lock:
                mc = self.mc_cfg()
                notes = mc.get("notes", MC_DEFAULT_NOTES)
                buttons = dict(mc.get("buttons", {}))
            all_notes = [n for lst in notes.values() for n in lst]
            for n in all_notes:
                b = buttons.get(str(n))
                self.set_led(n, self._led_state_for(b, running_procs) if b else 0)
        else:
            with self.lock:
                layer = self.active_layer
                buttons = dict(self.layer_cfg(layer).get("buttons", {}))
            states = {}
            for note_s, b in buttons.items():
                idx = led_index_of_note(int(note_s))
                if idx is not None:
                    states[idx] = self._led_state_for(b, running_procs)
            for idx in range(16):
                try:
                    self._out.send(mido.Message(
                        "note_on", channel=LED_CHANNEL, note=idx,
                        velocity=states.get(idx, 0)))
                except Exception:
                    pass

    def _set_active_layer(self, layer: str):
        if layer != self.active_layer:
            self.active_layer = layer
            self.apply_leds()

    def _set_hw_mode(self, mode: str):
        if mode != self.hw_mode:
            self.hw_mode = mode
            name = "MC (Mackie)" if mode == "mc" else "Standard"
            self._emit("log", "info", f"Geraete-Modus erkannt: {name}")
            self._emit("mode", mode)
            self.apply_leds()

    def reset_hotkey_encoders(self):
        # Richtungs-Erkennung neu initialisieren (z. B. nach Umkonfiguration)
        self._enc_state = {}

    # ------------------------------------------------------------------- Loop

    def _close_out(self):
        if self._out is not None:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None

    def run(self):
        while True:
            # MIDI-Freigabe: solange ein eingetragenes Programm (z. B. eine DAW)
            # laeuft, bleiben die Ports geschlossen, damit es den Controller
            # selbst benutzen kann. Windows gibt ein MIDI-Geraet nur an EIN
            # Programm - deshalb reicht Ignorieren nicht, wir muessen loslassen.
            if self.release_for:
                if not self.released:
                    self.released = True
                    self._emit("log", "info",
                               f"MIDI-Gerät freigegeben für {self.release_for} – "
                               "Steuerung ruht, bis das Programm beendet ist")
                time.sleep(1)
                continue
            if self.released:
                self.released = False
                self._emit("log", "info", "MIDI-Freigabe beendet – verbinde neu …")
            try:
                port_name = self._find_port()
                try:
                    self._out = mido.open_output(self._find_out_port())
                except Exception as e:
                    self._out = None
                    self._emit("log", "err",
                               f"MIDI-Ausgang nicht gefunden – LEDs deaktiviert ({e})")
                with mido.open_input(port_name) as port:
                    self.connected = True
                    self._emit("log", "info", f"Verbunden mit: {port_name}")
                    self.apply_leds()
                    self.reset_hotkey_encoders()
                    # Nicht blockierend pollen: Windows meldet ein abgezogenes
                    # Geraet nicht - receive() wuerde ewig an der toten
                    # Verbindung haengen. Deshalb alle 2 s pruefen, ob der
                    # Port noch existiert, und sonst neu verbinden.
                    last_check = time.time()
                    while True:
                        if self.release_for:
                            raise _Released()
                        msg = port.poll()
                        if msg is None:
                            time.sleep(0.004)
                            now = time.time()
                            if now - last_check >= 2.0:
                                last_check = now
                                if port_name not in mido.get_input_names():
                                    raise RuntimeError("Gerät getrennt")
                            continue
                        try:
                            self._handle(msg)
                        except Exception as e:
                            self._emit("log", "err", str(e))
            except _Released:
                self.connected = False
                self._close_out()
            except Exception as e:
                if self.connected:
                    self._emit("log", "err", f"MIDI: {e} – neuer Versuch in 3 s")
                self.connected = False
                self._close_out()
                time.sleep(3)

    def _find_port(self) -> str:
        wanted = self.cfg.get("midi_port", "")
        for name in mido.get_input_names():
            if wanted.lower() in name.lower():
                return name
        raise RuntimeError(f"'{wanted}' nicht gefunden")

    def _find_out_port(self) -> str:
        # Windows nummeriert Ausgaenge anders als Eingaenge
        # (z. B. Eingang "X-TOUCH MINI 0", Ausgang "X-TOUCH MINI 1")
        wanted = self.cfg.get("midi_port", "")
        for name in mido.get_output_names():
            if wanted.lower() in name.lower():
                return name
        raise RuntimeError(f"Ausgang '{wanted}' nicht gefunden")

    # ------------------------------------------------------------ Verarbeitung

    def _handle(self, msg):
        if msg.type == "pitchwheel":
            self._set_hw_mode("mc")
            self._handle_mc_fader(msg.pitch)
        elif msg.type == "control_change":
            if msg.channel == MIDI_CHANNEL:
                self._set_hw_mode("standard")
                self._handle_cc(msg.control, msg.value)
            elif msg.control in MC_ENC_CCS:
                self._set_hw_mode("mc")
                self._handle_mc_enc(msg.control - MC_ENC_CCS[0] + 1, msg.value)
            else:
                self._emit("log", "unmap",
                           f"CC{msg.control} = {msg.value} (Kanal {msg.channel + 1})  →  (unbekannt)")
        elif msg.type == "note_on" and msg.velocity > 0:
            mode = "standard" if msg.channel == MIDI_CHANNEL else "mc"
            self._set_hw_mode(mode)
            self._emit("press", msg.note, mode)
            if mode == "mc":
                self._handle_mc_button(msg.note)
            else:
                self._handle_button(msg.note)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            mode = "standard" if msg.channel == MIDI_CHANNEL else "mc"
            self._emit("release", msg.note, mode)

    def _paused_log(self, text):
        self._emit("log", "unmap", f"(pausiert) {text}")

    # ----------------------------------------------------- Standard-Modus

    def _handle_cc(self, cc, value):
        norm = value / 127.0
        pct = round(norm * 100)
        kind = classify_cc(cc)
        if kind is None:
            self._emit("log", "unmap", f"CC{cc} = {value}  →  (unbekannt)")
            return
        what, layer = kind
        self._set_active_layer(layer)

        if what == "fader":
            self._emit("fader", layer, value)
            with self.lock:
                target = self.layer_cfg(layer).get("fader", {"mode": "off"})
            desc = self._volume_action(f"fader{layer}", target, norm)
            if desc is None:
                return
            self._emit("log", "map", f"[{layer}] Fader = {value}  →  {desc} {pct} %")
            return

        self._emit("enc", layer, cc)
        with self.lock:
            enc = self.layer_cfg(layer).get("encoders", {}).get(str(cc))
        if enc is None or enc.get("mode", "off") == "off":
            self._emit("log", "unmap", f"[{layer}] Encoder CC{cc} = {value}  →  (nicht belegt)")
            return

        mode = enc["mode"]
        if mode == "hotkey":
            # Absolute Werte -> Richtung aus dem Delta; am Anschlag (0/127)
            # sendet das Geraet bei jedem Klick denselben Wert erneut.
            last = self._enc_state.get(cc)
            self._enc_state[cc] = value
            if last is None or value == last:
                delta = -1 if value == 0 else (1 if value == 127 else 0)
                times = 1
            else:
                delta = value - last
                times = min(abs(delta), 5)
            if delta == 0:
                return
            action = enc.get("cw" if delta > 0 else "ccw", "none")
            direction = "↻" if delta > 0 else "↺"
            if not self.enabled:
                self._paused_log(f"[{layer}] Encoder CC{cc} {direction}")
                return
            if action == "none":
                self._emit("log", "unmap",
                           f"[{layer}] Encoder CC{cc} {direction}  →  (keine Aktion)")
            elif press_key(action, times):
                self._emit("log", "map",
                           f"[{layer}] Encoder CC{cc} {direction} ×{times}  →  "
                           f"{action_label(action)}")
            else:
                self._emit("log", "err", f"Ungültiges Tastenkürzel: {action}")
        else:
            desc = self._volume_action(f"enc{cc}", enc, norm)
            if desc is None:
                return
            self._emit("log", "map", f"[{layer}] Encoder CC{cc} = {value}  →  {desc} {pct} %")

    def _handle_button(self, note):
        layer = layer_of_note(note)
        if layer is None:
            self._emit("log", "unmap", f"Note {note}  →  (unbekannt)")
            return
        self._set_active_layer(layer)
        with self.lock:
            b = self.layer_cfg(layer).get("buttons", {}).get(str(note))
        action = (b or {}).get("action", "none")
        if not self.enabled:
            self._paused_log(f"[{layer}] Button Note {note}")
            return
        if b is None or action == "none":
            self._emit("log", "unmap", f"[{layer}] Button Note {note}  →  (nicht belegt)")
            return
        if press_key(action):
            self._emit("log", "map",
                       f"[{layer}] Button Note {note}  →  {action_label(action)}")
        else:
            self._emit("log", "err", f"Ungültiges Tastenkürzel: {action}")

    # ----------------------------------------------------------- MC-Modus

    def _resolve_target(self, target):
        """-> (cache_key, getter, setter, beschreibung) oder None"""
        mode = target.get("mode", "off")
        if mode == "windows":
            return ("win", self.win_vol.get_scalar, self.win_vol.set_scalar,
                    "Windows-Lautstärke")
        if mode == "sonar":
            ch = target.get("channel", "game")
            return (f"sonar:{ch}",
                    lambda c=ch: self.sonar.get_volume(c),
                    lambda v, c=ch: self.sonar.set_volume(c, v),
                    f"Sonar {SONAR_NAMES.get(ch, ch)}")
        if mode == "app":
            proc = target.get("process", "")
            if not proc:
                return None
            return (f"app:{proc.lower()}",
                    lambda p=proc: self.app_vol.get(p),
                    lambda v, p=proc: self.app_vol.set(p, v),
                    proc)
        return None

    def _handle_mc_enc(self, idx, value):
        # relative Kodierung: 1-7 = rechts (Anzahl Klicks), 65-71 = links
        delta = value if value < 64 else -(value - 64)
        if delta == 0:
            return
        self._emit("enc", "MC", idx)
        with self.lock:
            enc = self.mc_cfg().get("encoders", {}).get(str(idx))
        if enc is None or enc.get("mode", "off") == "off":
            self._emit("log", "unmap", f"[MC] Encoder {idx}  →  (nicht belegt)")
            return
        direction = "↻" if delta > 0 else "↺"
        if enc["mode"] == "hotkey":
            action = enc.get("cw" if delta > 0 else "ccw", "none")
            times = min(abs(delta), 5)
            if not self.enabled:
                self._paused_log(f"[MC] Encoder {idx} {direction}")
                return
            if action == "none":
                self._emit("log", "unmap", f"[MC] Encoder {idx} {direction}  →  (keine Aktion)")
            elif press_key(action, times):
                self._emit("log", "map",
                           f"[MC] Encoder {idx} {direction} ×{times}  →  "
                           f"{action_label(action)}")
            else:
                self._emit("log", "err", f"Ungültiges Tastenkürzel: {action}")
            return

        if not self.enabled:
            self._paused_log(f"[MC] Encoder {idx} {direction}")
            return
        res = self._resolve_target(enc)
        if res is None:
            return
        key, getter, setter, desc = res

        step = max(1, min(25, int(enc.get("step", REL_STEP_DEFAULT)))) / 100.0

        def apply(ticks, key=key, getter=getter, setter=setter, desc=desc,
                  idx=idx, step=step):
            now = time.time()
            c = self.vol_cache.get(key)
            cur = c[1] if c and now - c[0] < 2.0 else getter()
            new = max(0.0, min(1.0, cur + ticks * step))
            setter(new)
            self.vol_cache[key] = (time.time(), new)
            self.set_ring(idx, new)
            self._emit("log", "map", f"[MC] Encoder {idx}  →  {desc} {round(new * 100)} %")

        self.dispatcher.submit_add(key, apply, delta)

    def _handle_mc_fader(self, pitch):
        norm = (pitch + 8192) / 16383.0
        self._emit("fader", "MC", round(norm * 127))
        with self.lock:
            target = self.mc_cfg().get("fader", {"mode": "off"})
        desc = self._volume_action("faderMC", target, norm)
        if desc is None:
            return
        self._emit("log", "map", f"[MC] Fader  →  {desc} {round(norm * 100)} %")

    def _handle_mc_button(self, note):
        if self.mc_learn:
            self.mc_learn = False
            self._emit("learn", note)
            return
        with self.lock:
            b = self.mc_cfg().get("buttons", {}).get(str(note))
        action = (b or {}).get("action", "none")
        if not self.enabled:
            self._paused_log(f"[MC] Button Note {note}")
            return
        if b is None or action == "none":
            self._emit("log", "unmap", f"[MC] Button Note {note}  →  (nicht belegt)")
            return
        if press_key(action):
            self._emit("log", "map",
                       f"[MC] Button Note {note}  →  {action_label(action)}")
        else:
            self._emit("log", "err", f"Ungültiges Tastenkürzel: {action}")

    def _volume_action(self, key, target, norm):
        """Absolute Volume-Zuweisung; gibt Beschreibung zurueck (None = pausiert/aus)."""
        res = self._resolve_target(target)
        if res is None:
            return None
        if not self.enabled:
            self._paused_log("Lautstärke-Änderung")
            return None
        ckey, _getter, setter, desc = res

        def apply(v, ckey=ckey, setter=setter):
            setter(v)
            self.vol_cache[ckey] = (time.time(), v)

        self.dispatcher.submit(key, apply, norm)
        return desc


# ------------------------------------------- Prozess-/Ring-Ueberwachung

class BackgroundSync(threading.Thread):
    """Alle 2 s: Prozess-LEDs aktualisieren; im MC-Modus zusaetzlich die
    Encoder-Ringe mit dem echten Lautstaerke-Stand abgleichen."""

    def __init__(self, bridge: MidiBridge):
        super().__init__(daemon=True)
        self.bridge = bridge
        self._last = {}
        self._last_key = None  # (mode, layer)

    def forget(self):
        self._last = {}

    def run(self):
        CoInitialize()
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(2)

    def _tick(self):
        br = self.bridge
        with br.lock:
            release_apps = list(br.cfg.get("release_apps", []))
            mode = br.hw_mode
            layer = br.active_layer
            if mode == "mc":
                buttons = dict(br.mc_cfg().get("buttons", {}))
                encoders = dict(br.mc_cfg().get("encoders", {}))
            else:
                buttons = dict(br.layer_cfg(layer).get("buttons", {}))
                encoders = {}

        watched = {int(n): b["led"].get("process", "").lower()
                   for n, b in buttons.items()
                   if b.get("led", {}).get("mode") == "process"}

        running = None
        if release_apps or watched:
            running = {p.info["name"].lower()
                       for p in psutil.process_iter(["name"]) if p.info["name"]}

        # --- MIDI-Freigabe: laeuft eines der eingetragenen Programme?
        if release_apps:
            br.release_for = next(
                (a for a in release_apps if a.lower() in running), None)
        else:
            br.release_for = None

        if not br.connected:
            return

        if (mode, layer) != self._last_key:
            self._last_key = (mode, layer)
            self._last = {}

        # --- Prozess-LEDs
        if watched:
            for note, proc in watched.items():
                state = 1 if proc and proc in running else 0
                if self._last.get(note) != state:
                    br.set_led(note, state)
                    self._last[note] = state

        # --- MC: Ringe mit Ist-Werten abgleichen
        if mode != "mc":
            return
        sonar_vols = None
        for idx_s, enc in encoders.items():
            res = br._resolve_target(enc) if enc.get("mode") in ("windows", "sonar", "app") else None
            if res is None:
                continue
            key, getter, _setter, _desc = res
            try:
                if enc["mode"] == "sonar":
                    if sonar_vols is None:
                        sonar_vols = br.sonar.get_volumes()
                    val = sonar_vols.get(enc.get("channel", "game"), 0.0)
                else:
                    val = getter()
            except Exception:
                continue
            br.vol_cache[key] = (time.time(), val)
            ring_val = round(val * 100)
            if self._last.get(f"ring{idx_s}") != ring_val:
                br.set_ring(int(idx_s), val)
                self._last[f"ring{idx_s}"] = ring_val


# ------------------------------------------------------------------------ GUI

C_BG = "#1e1e1e"
C_DEV = "#2d2d30"
C_CTRL = "#454548"
C_OUTLINE = "#6a6a6e"
C_TEXT = "#d4d4d4"
C_DIM = "#8a8a8e"
C_PRESS = "#ff8c00"
C_LEDON = "#c04040"

COL_X0 = 62        # Mitte der ersten Spalte
COL_DX = 76        # Spaltenabstand
FADER_X = 672      # Fader-Position
CANVAS_W = 790
CANVAS_H = 316


class App:
    def __init__(self, start_hidden=False):
        self.cfg = load_config()
        self.events = queue.Queue()
        self.bridge = MidiBridge(self.cfg, self.events)
        self.watcher = BackgroundSync(self.bridge)
        self.view = "A"            # "A" | "B" | "MC"
        self._learn_ctx = None     # (listname, index, note_var) waehrend "Lernen"

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("820x720")
        self.root.minsize(820, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.canvas_items = {}
        self._flash_jobs = {}

        self._build_ui()
        self.bridge.start()
        self.watcher.start()
        self._make_tray()
        self.root.after(50, self._poll_events)
        if start_hidden:
            self.root.withdraw()
        if autostart_enabled():
            try:
                set_autostart(True)  # Eintrag aktuell halten (Pfad, --minimized)
            except Exception:
                pass

    # ----------------------------------------------------------------- Aufbau

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="Verbinde…")
        ttk.Label(top, textvariable=self.status_var).pack(side="left")

        self.pause_btn = tk.Button(
            top, text="▶  AKTIV", font=("Segoe UI", 10, "bold"),
            bg="#1a7f37", fg="white", activebackground="#2ea043",
            activeforeground="white", relief="flat", padx=14, pady=2,
            command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=16)

        ttk.Button(top, text="MIDI-Freigabe …",
                   command=self._open_release_dialog).pack(side="left")

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(
            top, text="Mit Windows starten", variable=self.autostart_var,
            command=self._toggle_autostart).pack(side="right")

        # --- Ansicht (Layer / MC)
        viewbar = ttk.Frame(self.root)
        viewbar.pack(fill="x", padx=8)
        ttk.Label(viewbar, text="Ansicht:").pack(side="left")
        self.view_var = tk.StringVar(value="A")
        for name, text in (("A", "Layer A"), ("B", "Layer B"), ("MC", "Mackie (MC)")):
            ttk.Radiobutton(viewbar, text=text, value=name,
                            variable=self.view_var,
                            command=self._on_view_selected).pack(side="left", padx=4)
        ttk.Label(viewbar, text="(Ansicht folgt automatisch dem Modus/Layer des Geräts)",
                  foreground="#777").pack(side="left", padx=8)

        # --- Geraete-Abbildung
        box = ttk.LabelFrame(self.root, text="X-Touch Mini  (Element anklicken zum Konfigurieren)")
        box.pack(fill="x", **pad)
        self.canvas = tk.Canvas(box, width=CANVAS_W, height=CANVAS_H, bg=C_BG,
                                highlightthickness=0)
        self.canvas.pack(padx=4, pady=4)
        self._draw_device()

        # --- Live-Ansicht
        live = ttk.LabelFrame(self.root, text="Live-Ansicht")
        live.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(live, height=8, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.tag_config("map", foreground="#1a7f37")
        self.log.tag_config("unmap", foreground="#9a6700")
        self.log.tag_config("err", foreground="#cf222e")
        self.log.tag_config("info", foreground="#0969da")

    # ------------------------------------------------------------ Geraetebild

    def _view_conf(self):
        if self.view == "MC":
            return self.cfg["mc"]
        return self.cfg["layers"][self.view]

    def _mc_notes(self):
        return self.cfg["mc"].get("notes", MC_DEFAULT_NOTES)

    def _draw_device(self):
        c = self.canvas
        c.delete("all")
        self.canvas_items = {}
        is_mc = self.view == "MC"

        # Gehaeuse
        c.create_rectangle(10, 10, 716, CANVAS_H - 10, fill=C_DEV,
                           outline="#3f3f46", width=2)
        title = "MACKIE-MODUS (MC)" if is_mc else f"LAYER {self.view}"
        c.create_text(360, 26, text=f"BEHRINGER  X-TOUCH  MINI   —   {title}",
                      fill=C_DIM, font=("Segoe UI", 8))

        if is_mc:
            notes = self._mc_notes()
            enc_keys = list(range(1, 9))                    # Index 1-8
            enc_caption = [str(i) for i in enc_keys]
            top_notes, bot_notes = notes["top"], notes["bot"]
            fader_caption = "PB (14 Bit)"
        else:
            L = LAYERS[self.view]
            enc_keys = L["enc_ccs"]
            enc_caption = [f"CC{cc}" for cc in enc_keys]
            top_notes, bot_notes = L["top_notes"], L["bot_notes"]
            fader_caption = f"CC{L['fader_cc']}"

        # Encoder-Reihe
        for i, key in enumerate(enc_keys):
            cx = COL_X0 + i * COL_DX
            cy = 78
            ring = c.create_oval(cx - 25, cy - 25, cx + 25, cy + 25,
                                 fill=C_CTRL, outline=C_OUTLINE, width=2,
                                 tags=(f"enc{key}",))
            knob = c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14,
                                 fill="#39393d", outline=C_OUTLINE,
                                 tags=(f"enc{key}",))
            label = c.create_text(cx, cy + 38, text="", fill=C_TEXT,
                                  font=("Segoe UI", 7), width=72,
                                  tags=(f"enc{key}",), justify="center")
            c.create_text(cx, cy - 33, text=enc_caption[i], fill=C_DIM,
                          font=("Segoe UI", 7))
            self.canvas_items[("enc", key)] = {"ring": ring, "knob": knob, "label": label}
            if is_mc:
                c.tag_bind(f"enc{key}", "<Button-1>",
                           lambda e, n=key: self._open_encoder_dialog_mc(n))
            else:
                c.tag_bind(f"enc{key}", "<Button-1>",
                           lambda e, n=key: self._open_encoder_dialog(n))

        # Buttons: 2 Reihen zu 8
        for row, row_notes in enumerate((top_notes, bot_notes)):
            listname = "top" if row == 0 else "bot"
            for i, note in enumerate(row_notes):
                x0 = COL_X0 + i * COL_DX - 27
                y0 = 158 + row * 64
                rect = c.create_rectangle(x0, y0, x0 + 54, y0 + 36,
                                          fill=C_CTRL, outline=C_OUTLINE, width=2,
                                          tags=(f"btn{note}",))
                label = c.create_text(x0 + 27, y0 + 18, text="", fill=C_TEXT,
                                      font=("Segoe UI", 7), width=52,
                                      tags=(f"btn{note}",), justify="center")
                c.create_text(x0 + 27, y0 + 45, text=f"N{note}", fill=C_DIM,
                              font=("Segoe UI", 6))
                self.canvas_items[("btn", note)] = {"rect": rect, "label": label}
                if is_mc:
                    c.tag_bind(f"btn{note}", "<Button-1>",
                               lambda e, ln=listname, ix=i: self._open_button_dialog_mc(ln, ix))
                else:
                    c.tag_bind(f"btn{note}", "<Button-1>",
                               lambda e, n=note: self._open_button_dialog(n))

        # MC: die beiden Layer-Tasten als Extra-Buttons rechts
        if is_mc:
            for i, note in enumerate(self._mc_notes()["extra"]):
                x0 = 726
                y0 = 158 + i * 64
                rect = c.create_rectangle(x0, y0, x0 + 54, y0 + 36,
                                          fill=C_CTRL, outline=C_OUTLINE, width=2,
                                          tags=(f"btn{note}",))
                label = c.create_text(x0 + 27, y0 + 18, text="", fill=C_TEXT,
                                      font=("Segoe UI", 7), width=52,
                                      tags=(f"btn{note}",), justify="center")
                c.create_text(x0 + 27, y0 + 45, text=f"N{note}", fill=C_DIM,
                              font=("Segoe UI", 6))
                self.canvas_items[("btn", note)] = {"rect": rect, "label": label}
                c.tag_bind(f"btn{note}", "<Button-1>",
                           lambda e, ix=i: self._open_button_dialog_mc("extra", ix))
            c.create_text(753, 140, text="Layer-\nTasten", fill=C_DIM,
                          font=("Segoe UI", 7), justify="center")
        else:
            c.create_text(754, 90, text="Klicken =\nkonfigu-\nrieren", fill=C_DIM,
                          font=("Segoe UI", 8), justify="center")

        # Fader
        fx = FADER_X
        c.create_text(fx, 40, text=fader_caption, fill=C_DIM, font=("Segoe UI", 7))
        c.create_rectangle(fx - 4, 56, fx + 4, 262, fill="#151517", outline=C_OUTLINE,
                           tags=("fader",))
        handle = c.create_rectangle(fx - 19, 150, fx + 19, 172, fill=C_CTRL,
                                    outline=C_OUTLINE, width=2, tags=("fader",))
        flabel = c.create_text(fx, 284, text="", fill=C_TEXT, font=("Segoe UI", 7),
                               width=76, tags=("fader",), justify="center")
        self.canvas_items["fader"] = {"handle": handle, "label": flabel, "x": fx}
        c.tag_bind("fader", "<Button-1>", lambda e: self._open_fader_dialog())

        self._refresh_labels()

    def _enc_short(self, e) -> str:
        mode = e.get("mode", "off")
        if mode == "windows":
            return "Windows-Vol."
        if mode == "sonar":
            return "Sonar\n" + SONAR_NAMES.get(e.get("channel", ""), "?")
        if mode == "app":
            return e.get("process", "?")
        if mode == "hotkey":
            cw = SHORT_LABELS.get(e.get("cw", "none"), e.get("cw", ""))
            ccw = SHORT_LABELS.get(e.get("ccw", "none"), e.get("ccw", ""))
            return f"↻{cw}\n↺{ccw}"
        return ""

    def _refresh_labels(self):
        c = self.canvas
        conf = self._view_conf()
        is_mc = self.view == "MC"

        if is_mc:
            notes = self._mc_notes()
            enc_keys = list(range(1, 9))
            push_notes = notes["push"]
            all_btn_notes = notes["top"] + notes["bot"] + notes["extra"]
        else:
            L = LAYERS[self.view]
            enc_keys = L["enc_ccs"]
            push_notes = L["push_notes"]
            all_btn_notes = L["top_notes"] + L["bot_notes"]

        for i, key in enumerate(enc_keys):
            e = conf.get("encoders", {}).get(str(key), {"mode": "off"})
            text = self._enc_short(e)
            push = conf.get("buttons", {}).get(str(push_notes[i]), {})
            push_a = push.get("action", "none")
            if push_a != "none":
                text += ("\n" if text else "") + "⬇" + SHORT_LABELS.get(push_a, "")
            c.itemconfigure(self.canvas_items[("enc", key)]["label"], text=text)

        for note in all_btn_notes:
            if ("btn", note) not in self.canvas_items:
                continue
            b = conf.get("buttons", {}).get(str(note), {})
            action = b.get("action", "none")
            c.itemconfigure(self.canvas_items[("btn", note)]["label"],
                            text=SHORT_LABELS.get(action, action if action != "none" else ""))
            led = b.get("led", {}).get("mode", "off")
            base = C_LEDON if led in ("on", "blink") else C_CTRL
            c.itemconfigure(self.canvas_items[("btn", note)]["rect"], fill=base)

        f = conf.get("fader", {"mode": "off"})
        self.canvas.itemconfigure(self.canvas_items["fader"]["label"],
                                  text=self._enc_short(f) or "—")

    def _on_view_selected(self):
        self.view = self.view_var.get()
        self._draw_device()

    def _ensure_view(self, name):
        if name != self.view:
            self.view = name
            self.view_var.set(name)
            self._draw_device()

    # ----------------------------------------------------- Canvas-Live-Effekte

    def _button_base_color(self, note) -> str:
        led = self._view_conf().get("buttons", {}).get(str(note), {}) \
                               .get("led", {}).get("mode", "off")
        return C_LEDON if led in ("on", "blink") else C_CTRL

    def _push_note_to_key(self, note):
        """Push-Note -> Encoder-Canvas-Key der aktuellen Ansicht."""
        if self.view == "MC":
            push = self._mc_notes()["push"]
            if note in push:
                return push.index(note) + 1
            return None
        L = LAYERS[self.view]
        if note in L["push_notes"]:
            return L["enc_ccs"][L["push_notes"].index(note)]
        return None

    def _on_press(self, note, mode):
        self._ensure_view("MC" if mode == "mc" else
                          (layer_of_note(note) or self.view if mode == "standard" else self.view))
        item = self.canvas_items.get(("btn", note))
        if item:
            self.canvas.itemconfigure(item["rect"], fill=C_PRESS)
            return
        key = self._push_note_to_key(note)
        if key is not None and ("enc", key) in self.canvas_items:
            self.canvas.itemconfigure(self.canvas_items[("enc", key)]["knob"], fill=C_PRESS)

    def _on_release(self, note, mode):
        item = self.canvas_items.get(("btn", note))
        if item:
            self.canvas.itemconfigure(item["rect"], fill=self._button_base_color(note))
            return
        key = self._push_note_to_key(note)
        if key is not None and ("enc", key) in self.canvas_items:
            self.canvas.itemconfigure(self.canvas_items[("enc", key)]["knob"], fill="#39393d")

    def _on_enc_turn(self, view, key):
        self._ensure_view(view if view == "MC" else view)
        item = self.canvas_items.get(("enc", key))
        if not item:
            return
        self.canvas.itemconfigure(item["ring"], outline=C_PRESS, width=3)
        old = self._flash_jobs.pop(key, None)
        if old:
            self.root.after_cancel(old)
        self._flash_jobs[key] = self.root.after(
            180, lambda: self.canvas.itemconfigure(
                item["ring"], outline=C_OUTLINE, width=2))

    def _on_fader_move(self, view, value):
        self._ensure_view(view)
        item = self.canvas_items["fader"]
        y = 240 - (value / 127.0) * 180  # 0 -> unten, 127 -> oben
        self.canvas.coords(item["handle"], item["x"] - 19, y, item["x"] + 19, y + 22)

    # ---------------------------------------------------------------- Dialoge

    def _dialog(self, title):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        return win

    CUSTOM_LABEL = "Eigenes Kürzel …"

    def _action_combo(self, parent, current):
        """Aktions-Auswahl inkl. eigener Kuerzel; -> (widget, getter)."""
        frame = ttk.Frame(parent)
        is_custom = current not in ACTION_LABELS
        var = tk.StringVar(value=self.CUSTOM_LABEL if is_custom
                           else ACTION_LABELS.get(current, ACTION_LABELS["none"]))
        combo = ttk.Combobox(frame, textvariable=var, width=22, state="readonly",
                             values=list(ACTION_LABELS.values()) + [self.CUSTOM_LABEL])
        combo.pack(side="left")
        custom_var = tk.StringVar(value=current if is_custom else "")
        entry = ttk.Entry(frame, textvariable=custom_var, width=14)

        def update(*_):
            if var.get() == self.CUSTOM_LABEL:
                entry.pack(side="left", padx=4)
            else:
                entry.pack_forget()
        combo.bind("<<ComboboxSelected>>", update)
        update()

        def getter():
            if var.get() == self.CUSTOM_LABEL:
                spec = custom_var.get().strip().lower()
                if not spec or parse_combo(spec) is None:
                    self._log("err", f"Ungültiges Tastenkürzel: {spec!r} – "
                                     "Beispiel: strg+c, strg+shift+esc, win+d")
                    return "none"
                return spec
            return LABEL_TO_ACTION.get(var.get(), "none")

        return frame, getter

    def _button_dialog_body(self, win, b):
        """Gemeinsamer Teil des Button-Dialogs; -> (frame, action_var, led_getter)"""
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        row0 = ttk.Frame(frame)
        row0.pack(anchor="w")
        ttk.Label(row0, text="Aktion beim Drücken:").pack(side="left")
        action_widget, action_get = self._action_combo(row0, b.get("action", "none"))
        action_widget.pack(side="left", padx=6, pady=4)
        ttk.Label(frame, text="   Eigenes Kürzel: z. B. strg+c, strg+shift+esc,"
                              " win+d, alt+f4, strg+alt+entf",
                  foreground="#777").pack(anchor="w")

        ttk.Label(frame, text="LED der Taste:").pack(anchor="w", pady=(10, 0))
        led = b.get("led", {"mode": "off"})
        led_var = tk.StringVar(value=led.get("mode", "off"))
        ledframe = ttk.Frame(frame)
        ledframe.pack(anchor="w", padx=12)
        for mode, text in LED_MODES.items():
            ttk.Radiobutton(ledframe, text=text, value=mode, variable=led_var
                            ).pack(anchor="w")
        proc_var = tk.StringVar(value=led.get("process", ""))
        proc_combo = ttk.Combobox(ledframe, textvariable=proc_var, width=28)
        proc_combo.pack(anchor="w", padx=(20, 0), pady=2)

        def fill_procs():
            proc_combo.configure(values=list_processes())
        ttk.Button(ledframe, text="Prozessliste aktualisieren", command=fill_procs
                   ).pack(anchor="w", padx=(20, 0))
        fill_procs()

        def led_result():
            mode = led_var.get()
            out = {"mode": mode}
            if mode == "process":
                out["process"] = proc_var.get().strip()
            return out

        return frame, action_get, led_result

    def _open_button_dialog(self, note):
        layer = self.view
        conf = self._view_conf()
        b = conf.get("buttons", {}).get(str(note), {})
        win = self._dialog(f"Layer {layer} – Button (Note {note})")
        frame, action_get, led_result = self._button_dialog_body(win, b)

        def ok():
            entry = {"action": action_get(), "led": led_result()}
            with self.bridge.lock:
                conf.setdefault("buttons", {})[str(note)] = entry
            save_config(self.cfg)
            self.watcher.forget()
            self.bridge.apply_leds()
            self._refresh_labels()
            self._log("info", f"[{layer}] Button Note {note} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    def _open_button_dialog_mc(self, listname, index):
        notes = self._mc_notes()
        note = notes[listname][index]
        conf = self.cfg["mc"]
        b = conf.get("buttons", {}).get(str(note), {})
        win = self._dialog(f"MC – Button (Note {note})")
        frame, action_get, led_result = self._button_dialog_body(win, b)

        noterow = ttk.Frame(frame)
        noterow.pack(anchor="w", pady=(10, 0))
        note_var = tk.StringVar(value=f"MIDI-Note: {note}")
        ttk.Label(noterow, textvariable=note_var).pack(side="left")

        def learn():
            self._learn_ctx = (listname, index, note_var, win)
            self.bridge.mc_learn = True
            note_var.set("MIDI-Note: drücke die Taste am Gerät …")
        ttk.Button(noterow, text="Lernen", command=learn).pack(side="left", padx=8)

        def ok():
            cur_note = self._mc_notes()[listname][index]
            entry = {"action": action_get(), "led": led_result()}
            with self.bridge.lock:
                conf.setdefault("buttons", {})[str(cur_note)] = entry
            save_config(self.cfg)
            self.watcher.forget()
            self.bridge.apply_leds()
            self._draw_device()
            self._log("info", f"[MC] Button Note {cur_note} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    def _volume_mode_ui(self, frame, current, with_hotkey, relative=False):
        """Baut die Modus-Auswahl fuer Encoder/Fader; gibt getter zurueck."""
        mode_var = tk.StringVar(value=current.get("mode", "off"))

        ttk.Radiobutton(frame, text="Aus", value="off", variable=mode_var
                        ).pack(anchor="w")
        ttk.Radiobutton(frame, text="Windows-Lautstärke (Master)", value="windows",
                        variable=mode_var).pack(anchor="w")

        row = ttk.Frame(frame)
        row.pack(anchor="w")
        ttk.Radiobutton(row, text="Sonar-Kanal:", value="sonar", variable=mode_var
                        ).pack(side="left")
        sonar_var = tk.StringVar(
            value=SONAR_NAMES.get(current.get("channel", "game"), "Game"))
        ttk.Combobox(row, textvariable=sonar_var, width=12, state="readonly",
                     values=list(SONAR_NAMES.values())).pack(side="left", padx=4)

        row2 = ttk.Frame(frame)
        row2.pack(anchor="w")
        ttk.Radiobutton(row2, text="App-Lautstärke:", value="app", variable=mode_var
                        ).pack(side="left")
        app_var = tk.StringVar(value=current.get("process", ""))
        app_combo = ttk.Combobox(row2, textvariable=app_var, width=22)
        app_combo.pack(side="left", padx=4)

        def fill_apps():
            app_combo.configure(values=list_audio_apps())
        ttk.Button(row2, text="↻", width=3, command=fill_apps).pack(side="left")
        fill_apps()
        hint = "   (Liste = Apps mit Audio-Session; App muss mal Ton" \
               " ausgegeben haben. Manuell: z. B. spotify.exe)"
        ttk.Label(frame, text=hint, foreground="#777").pack(anchor="w")

        ring_var = step_var = None
        if relative:
            opt = ttk.Frame(frame)
            opt.pack(anchor="w", pady=(4, 0))
            ttk.Label(opt, text="Schrittweite:").grid(row=0, column=0, sticky="w")
            step_var = tk.StringVar(value=str(current.get("step", REL_STEP_DEFAULT)))
            ttk.Spinbox(opt, from_=1, to=25, textvariable=step_var, width=4
                        ).grid(row=0, column=1, sticky="w", padx=4)
            ttk.Label(opt, text="% pro Raststufe").grid(row=0, column=2, sticky="w")
            ttk.Label(opt, text="Ring-Anzeige:").grid(row=1, column=0, sticky="w", pady=2)
            ring_var = tk.StringVar(
                value=RING_STYLE_LABELS.get(current.get("ring", "fan"), "Füllstand"))
            ttk.Combobox(opt, textvariable=ring_var, width=20, state="readonly",
                         values=list(RING_STYLE_LABELS.values())
                         ).grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=2)

        cw_get = ccw_get = None
        if with_hotkey:
            ttk.Radiobutton(frame, text="Tastenkürzel pro Klick", value="hotkey",
                            variable=mode_var).pack(anchor="w", pady=(4, 0))
            grid = ttk.Frame(frame)
            grid.pack(anchor="w", padx=20)
            ttk.Label(grid, text="↻ im Uhrzeigersinn:").grid(row=0, column=0, sticky="w")
            cw_widget, cw_get = self._action_combo(grid, current.get("cw", "none"))
            cw_widget.grid(row=0, column=1, padx=4, pady=2)
            ttk.Label(grid, text="↺ gegen Uhrzeigersinn:").grid(row=1, column=0, sticky="w")
            ccw_widget, ccw_get = self._action_combo(grid, current.get("ccw", "none"))
            ccw_widget.grid(row=1, column=1, padx=4, pady=2)
            ttk.Label(grid, text="Eigenes Kürzel: z. B. strg+c, win+d",
                      foreground="#777").grid(row=2, column=0, columnspan=2, sticky="w")

        def result():
            mode = mode_var.get()
            out = {"mode": mode}
            if mode == "sonar":
                out["channel"] = SONAR_LABEL_TO_KEY.get(sonar_var.get(), "game")
            elif mode == "app":
                out["process"] = app_var.get().strip()
                if not out["process"]:
                    out["mode"] = "off"
            elif mode == "hotkey" and cw_get is not None:
                out["cw"] = cw_get()
                out["ccw"] = ccw_get()
            if ring_var is not None:
                out["ring"] = RING_LABEL_TO_STYLE.get(ring_var.get(), "fan")
                try:
                    out["step"] = max(1, min(25, int(step_var.get())))
                except ValueError:
                    out["step"] = REL_STEP_DEFAULT
            return out

        return result

    def _encoder_dialog(self, title, conf, enc_key, push_note, relative):
        current = conf.get("encoders", {}).get(str(enc_key), {"mode": "off"})
        push = conf.get("buttons", {}).get(str(push_note), {})

        win = self._dialog(title)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Drehen:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        get_turn = self._volume_mode_ui(frame, current, with_hotkey=True, relative=relative)

        ttk.Separator(frame).pack(fill="x", pady=8)
        ttk.Label(frame, text="Drücken (Push):", font=("Segoe UI", 9, "bold")
                  ).pack(anchor="w")
        push_widget, push_get = self._action_combo(frame, push.get("action", "none"))
        push_widget.pack(anchor="w", pady=4)

        def ok():
            turn = get_turn()
            with self.bridge.lock:
                conf.setdefault("encoders", {})[str(enc_key)] = turn
                conf.setdefault("buttons", {})[str(push_note)] = {
                    "action": push_get()}
            save_config(self.cfg)
            self.bridge.reset_hotkey_encoders()
            self.watcher.forget()
            self._refresh_labels()
            self._log("info", f"{title} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    def _open_encoder_dialog(self, cc):
        layer = self.view
        L = LAYERS[layer]
        push_note = L["push_notes"][L["enc_ccs"].index(cc)]
        self._encoder_dialog(f"Layer {layer} – Encoder CC{cc}",
                             self._view_conf(), cc, push_note, relative=False)

    def _open_encoder_dialog_mc(self, idx):
        push_note = self._mc_notes()["push"][idx - 1]
        self._encoder_dialog(f"MC – Encoder {idx}",
                             self.cfg["mc"], idx, push_note, relative=True)

    def _open_fader_dialog(self):
        view = self.view
        conf = self._view_conf()
        current = conf.get("fader", {"mode": "off"})
        title = "MC – Fader" if view == "MC" else f"Layer {view} – Fader"
        win = self._dialog(title)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Fader regelt:", font=("Segoe UI", 9, "bold")
                  ).pack(anchor="w")
        get_turn = self._volume_mode_ui(frame, current, with_hotkey=False)

        def ok():
            with self.bridge.lock:
                conf["fader"] = get_turn()
            save_config(self.cfg)
            self._refresh_labels()
            self._log("info", f"{title} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    # -------------------------------------------------------------- MC-Lernen

    def _on_learn(self, note):
        if self._learn_ctx is None:
            return
        listname, index, note_var, _win = self._learn_ctx
        self._learn_ctx = None
        notes = self._mc_notes()
        old_note = notes[listname][index]
        if old_note != note:
            with self.bridge.lock:
                notes[listname][index] = note
                buttons = self.cfg["mc"].setdefault("buttons", {})
                if str(old_note) in buttons and str(note) not in buttons:
                    buttons[str(note)] = buttons.pop(str(old_note))
            save_config(self.cfg)
        note_var.set(f"MIDI-Note: {note}")
        self._log("info", f"[MC] Position neu gelernt: Note {note}")

    # --------------------------------------------------------- MIDI-Freigabe

    def _open_release_dialog(self):
        win = self._dialog("MIDI-Freigabe für andere Programme")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=(
            "Solange eines dieser Programme läuft, gibt Midi2Sonar den\n"
            "X-Touch Mini komplett frei (Windows erlaubt nur einem Programm\n"
            "den MIDI-Zugriff). Das Programm kann den Controller dann direkt\n"
            "benutzen. Wird es beendet, übernimmt Midi2Sonar automatisch wieder.\n"
            "Geprüft wird alle 2 Sekunden."),
            justify="left").pack(anchor="w", pady=(0, 8))

        listbox = tk.Listbox(frame, height=6, width=44)
        listbox.pack(anchor="w")

        def refresh_list():
            listbox.delete(0, "end")
            for a in self.cfg.get("release_apps", []):
                listbox.insert("end", a)
        refresh_list()

        addrow = ttk.Frame(frame)
        addrow.pack(anchor="w", pady=(8, 0))
        proc_var = tk.StringVar()
        proc_combo = ttk.Combobox(addrow, textvariable=proc_var, width=30)
        proc_combo.pack(side="left")

        def fill_procs():
            proc_combo.configure(values=list_processes())
        ttk.Button(addrow, text="↻", width=3, command=fill_procs).pack(side="left", padx=2)
        fill_procs()

        def add():
            name = proc_var.get().strip()
            if not name:
                return
            with self.bridge.lock:
                apps = self.cfg.setdefault("release_apps", [])
                if name.lower() not in (a.lower() for a in apps):
                    apps.append(name)
            save_config(self.cfg)
            proc_var.set("")
            refresh_list()
            self._log("info", f"MIDI-Freigabe: {name} hinzugefügt")

        def remove():
            sel = listbox.curselection()
            if not sel:
                return
            name = listbox.get(sel[0])
            with self.bridge.lock:
                self.cfg["release_apps"] = [
                    a for a in self.cfg.get("release_apps", []) if a != name]
            save_config(self.cfg)
            refresh_list()
            self._log("info", f"MIDI-Freigabe: {name} entfernt")

        ttk.Button(addrow, text="Hinzufügen", command=add).pack(side="left", padx=4)
        ttk.Button(addrow, text="Entfernen", command=remove).pack(side="left")

        ttk.Label(frame, text="(Liste über ↻ = laufende Programme; Name lässt sich"
                              " auch von Hand eintragen, z. B. ableton live 12.exe)",
                  foreground="#777").pack(anchor="w", pady=(4, 0))

        ttk.Button(frame, text="Schließen", command=win.destroy
                   ).pack(anchor="e", pady=(10, 0))

    # ------------------------------------------------------------ Play/Pause

    def _toggle_pause(self):
        self.bridge.enabled = not self.bridge.enabled
        if self.bridge.enabled:
            self.pause_btn.configure(text="▶  AKTIV", bg="#1a7f37",
                                     activebackground="#2ea043")
            self._log("info", "Aktiv – MIDI-Aktionen werden ausgeführt")
        else:
            self.pause_btn.configure(text="⏸  PAUSIERT", bg="#9a6700",
                                     activebackground="#bb8009")
            self._log("info", "Pausiert – MIDI-Aktionen werden ignoriert")

    def _toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
            state = "aktiviert" if self.autostart_var.get() else "deaktiviert"
            self._log("info", f"Autostart {state}")
        except Exception as e:
            self._log("err", f"Autostart fehlgeschlagen: {e}")

    # ------------------------------------------------------------- Ereignisse

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload[0], payload[1])
                elif kind == "press":
                    self._on_press(payload[0], payload[1])
                elif kind == "release":
                    self._on_release(payload[0], payload[1])
                elif kind == "enc":
                    self._on_enc_turn(payload[0], payload[1])
                elif kind == "fader":
                    self._on_fader_move(payload[0], payload[1])
                elif kind == "mode":
                    self._ensure_view("MC" if payload[0] == "mc"
                                      else self.bridge.active_layer)
                elif kind == "learn":
                    self._on_learn(payload[0])
        except queue.Empty:
            pass
        if self.bridge.released:
            app = self.bridge.release_for or "…"
            self.status_var.set(f"◌ MIDI freigegeben für {app}")
        elif self.bridge.connected:
            mode = "MC-Modus" if self.bridge.hw_mode == "mc" else "Standard-Modus"
            self.status_var.set(f"● Verbunden ({mode})")
        else:
            self.status_var.set("○ Controller nicht gefunden – suche…")
        self.root.after(40, self._poll_events)

    def _log(self, typ, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n", typ)
        if float(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ Tray

    def _make_tray(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(30, 110, 220, 255))
        d.rectangle([16, 22, 22, 48], fill="white")
        d.rectangle([29, 14, 35, 48], fill="white")
        d.rectangle([42, 30, 48, 48], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", self._show_window, default=True),
            pystray.MenuItem("Beenden", self._quit),
        )
        self.tray = pystray.Icon(APP_NAME, img, APP_NAME, menu)
        self.tray.run_detached()

    def hide_window(self):
        self.root.withdraw()

    def _show_window(self, *_):
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def _quit(self, *_):
        self.tray.stop()
        self.root.after(0, self.root.destroy)

    def run(self):
        self.root.mainloop()


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, f"{APP_NAME}_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    CoInitialize()
    App(start_hidden="--minimized" in sys.argv).run()


if __name__ == "__main__":
    main()
