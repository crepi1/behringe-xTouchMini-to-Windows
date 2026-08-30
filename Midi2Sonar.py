"""
Midi2Sonar - X-Touch Mini -> Windows Medientasten / Windows-Lautstaerke /
SteelSeries Sonar / App-Lautstaerken / Tastenkuerzel

GUI mit Tray-Icon und gezeichnetem X-Touch Mini:
- Layer A/B: eigene Belegung pro Layer, Ansicht folgt dem Geraet automatisch
- Klick auf ein Element im Bild -> Konfigurationsdialog
- Physischer Tastendruck laesst die Taste im Bild aufleuchten, Fader bewegt sich mit
- Play/Pause-Schalter pausiert alle Aktionen (Start immer aktiv)
- LEDs der physischen Tasten: aus / immer an / blinken / an wenn Prozess laeuft
- Encoder: Windows-Volume, Sonar-Kanal, App-Lautstaerke oder Tastenkuerzel pro Klick
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

MIDI_CHANNEL = 10  # X-Touch Mini Standard-Modus: MIDI-Kanal 11 (0-basiert 10)

# X-Touch Mini Standard-Modus: zwei Layer mit getrennten CC-/Notennummern
LAYERS = {
    "A": {
        "enc_ccs": list(range(1, 9)),      # Encoder drehen
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


def layer_of_note(note: int):
    return "A" if note < 24 else ("B" if note < 48 else None)


def classify_cc(cc: int):
    """-> ("fader"|"enc", layer) oder None"""
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


def press_key(action: str, times: int = 1) -> None:
    vk = VK.get(action)
    if vk is None:
        return
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


# ------------------------------------------------------------------ Autostart

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    return f'"{pythonw}" "{Path(__file__).resolve()}"'


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

    def set_scalar(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        try:
            if self._endpoint is None:
                self._endpoint = AudioUtilities.GetSpeakers().EndpointVolume
            self._endpoint.SetMasterVolumeLevelScalar(value, None)
        except Exception:
            self._endpoint = None
            self._endpoint = AudioUtilities.GetSpeakers().EndpointVolume
            self._endpoint.SetMasterVolumeLevelScalar(value, None)


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

    def set_volume(self, channel: str, value: float) -> None:
        value = max(0.0, min(1.0, value))
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._base is None:
                        self._base = self._discover()
                    url = f"{self._base}/volumeSettings/classic/{channel}/Volume/{value:.4f}"
                    requests.put(url, timeout=2).raise_for_status()
                    return
                except Exception as e:
                    self._base = None
                    if attempt == 2:
                        raise RuntimeError(f"Sonar nicht erreichbar: {e}") from e


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

    def set(self, process: str, value: float) -> None:
        value = max(0.0, min(1.0, value))
        name = process.lower()
        now = time.time()
        cached = self._cache.get(name)
        vols = cached[1] if cached and now - cached[0] < self.TTL else None
        if not vols:
            vols = self._sessions_for(name)
            self._cache[name] = (now, vols)
        if not vols:
            raise RuntimeError(f"{process}: keine Audio-Session (App muss mal Ton ausgeben)")
        try:
            for v in vols:
                v.SetMasterVolume(value, None)
        except Exception:
            vols = self._sessions_for(name)
            self._cache[name] = (time.time(), vols)
            for v in vols:
                v.SetMasterVolume(value, None)


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
    INTERVAL = 0.04

    def __init__(self, on_error):
        self._pending = {}
        self._cond = threading.Condition()
        self._on_error = on_error
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, key, func, value):
        with self._cond:
            self._pending[key] = (func, value)
            self._cond.notify()

    def _run(self):
        CoInitialize()
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                items, self._pending = self._pending, {}
            for func, value in items.values():
                try:
                    func(value)
                except Exception as e:
                    self._on_error(str(e))
            time.sleep(self.INTERVAL)


# ------------------------------------------------------------------- Config

EMPTY_LAYER = {"fader": {"mode": "off"}, "encoders": {}, "buttons": {}}

DEFAULT_CONFIG = {
    "midi_port": "X-TOUCH MINI",
    "layers": {
        "A": {
            "fader": {"mode": "windows"},
            "encoders": {
                "1": {"mode": "sonar", "channel": "game"},
                "2": {"mode": "sonar", "channel": "chatRender"},
                "3": {"mode": "sonar", "channel": "media"},
                "4": {"mode": "sonar", "channel": "aux"},
                "5": {"mode": "sonar", "channel": "chatCapture"},
            },
            "buttons": {
                "18": {"action": "prev"}, "22": {"action": "play_pause"},
                "19": {"action": "next"}, "21": {"action": "stop"},
                "23": {"action": "mute"},
            },
        },
        "B": json.loads(json.dumps(EMPTY_LAYER)),
    },
}


def _migrate(cfg: dict) -> dict:
    """Aeltere Formate -> aktuelles Format mit layers."""
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

class MidiBridge(threading.Thread):
    """Hintergrund-Thread: verbindet sich (und re-connectet) mit dem Controller."""

    def __init__(self, cfg: dict, events: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.lock = threading.Lock()
        self.connected = False
        self.enabled = True                     # globales Play/Pause
        self.win_vol = WindowsVolume()
        self.sonar = Sonar()
        self.app_vol = AppVolume()
        self._out = None
        self._enc_state = {}                    # cc -> letzter Wert (Hotkey-Modus)
        self.dispatcher = VolumeDispatcher(
            on_error=lambda msg: self._emit("log", "err", msg))

    # ------------------------------------------------------------- Hilfsmittel

    def _emit(self, kind, *payload):
        self.events.put((kind, payload))

    def layer_cfg(self, layer: str) -> dict:
        return self.cfg["layers"][layer]

    def set_led(self, note: int, state: int):
        """state: 0=aus, 1=an, 2=blinken"""
        if self._out is not None:
            try:
                self._out.send(mido.Message(
                    "note_on", channel=MIDI_CHANNEL, note=note, velocity=state))
            except Exception:
                pass

    def _send_cc(self, cc: int, value: int):
        if self._out is not None:
            try:
                self._out.send(mido.Message(
                    "control_change", channel=MIDI_CHANNEL, control=cc, value=value))
            except Exception:
                pass

    def apply_leds(self, running_procs=None):
        with self.lock:
            buttons = {}
            for name in ("A", "B"):
                buttons.update(self.layer_cfg(name).get("buttons", {}))
        for note_s, b in buttons.items():
            led = b.get("led", {"mode": "off"})
            mode = led.get("mode", "off")
            if mode == "on":
                state = 1
            elif mode == "blink":
                state = 2
            elif mode == "process":
                proc = led.get("process", "").lower()
                state = 1 if (running_procs is not None and proc in running_procs) else 0
            else:
                state = 0
            self.set_led(int(note_s), state)

    def reset_hotkey_encoders(self):
        with self.lock:
            pairs = []
            for name in ("A", "B"):
                for cc_s, e in self.layer_cfg(name).get("encoders", {}).items():
                    if e.get("mode") == "hotkey":
                        pairs.append(int(cc_s))
        for cc in pairs:
            self._send_cc(cc, 64)
            self._enc_state[cc] = 64

    # ------------------------------------------------------------------- Loop

    def run(self):
        while True:
            try:
                port_name = self._find_port()
                try:
                    self._out = mido.open_output(port_name)
                except Exception:
                    self._out = None
                with mido.open_input(port_name) as port:
                    self.connected = True
                    self._emit("log", "info", f"Verbunden mit: {port_name}")
                    self.apply_leds()
                    self.reset_hotkey_encoders()
                    while True:
                        msg = port.receive()
                        try:
                            self._handle(msg)
                        except Exception as e:
                            self._emit("log", "err", str(e))
            except Exception as e:
                if self.connected:
                    self._emit("log", "err", f"MIDI: {e} – neuer Versuch in 3 s")
                self.connected = False
                if self._out is not None:
                    try:
                        self._out.close()
                    except Exception:
                        pass
                    self._out = None
                time.sleep(3)

    def _find_port(self) -> str:
        wanted = self.cfg.get("midi_port", "")
        for name in mido.get_input_names():
            if wanted.lower() in name.lower():
                return name
        raise RuntimeError(f"'{wanted}' nicht gefunden")

    # ------------------------------------------------------------ Verarbeitung

    def _handle(self, msg):
        if msg.type == "control_change":
            self._handle_cc(msg.control, msg.value)
        elif msg.type == "note_on" and msg.velocity > 0:
            self._emit("press", msg.note)
            self._handle_button(msg.note)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self._emit("release", msg.note)

    def _paused_log(self, text):
        self._emit("log", "unmap", f"(pausiert) {text}")

    def _handle_cc(self, cc, value):
        norm = value / 127.0
        pct = round(norm * 100)
        kind = classify_cc(cc)
        if kind is None:
            self._emit("log", "unmap", f"CC{cc} = {value}  →  (unbekannt)")
            return
        what, layer = kind

        if what == "fader":
            self._emit("fader", layer, value)
            with self.lock:
                target = self.layer_cfg(layer).get("fader", {"mode": "off"})
            desc = self._volume_action(f"fader{layer}", target, norm)
            if desc is None:
                return
            self._emit("log", "map", f"[{layer}] Fader = {value}  →  {desc} {pct} %")
            return

        self._emit("enc", layer, cc, value)
        with self.lock:
            enc = self.layer_cfg(layer).get("encoders", {}).get(str(cc))
        if enc is None or enc.get("mode", "off") == "off":
            self._emit("log", "unmap", f"[{layer}] Encoder CC{cc} = {value}  →  (nicht belegt)")
            return

        mode = enc["mode"]
        if mode == "hotkey":
            last = self._enc_state.get(cc, 64)
            delta = value - last
            self._send_cc(cc, 64)
            self._enc_state[cc] = 64
            if delta == 0:
                return
            action = enc.get("cw" if delta > 0 else "ccw", "none")
            times = min(abs(delta), 5)
            direction = "↻" if delta > 0 else "↺"
            if not self.enabled:
                self._paused_log(f"[{layer}] Encoder CC{cc} {direction}")
                return
            if action != "none":
                press_key(action, times)
                self._emit("log", "map",
                           f"[{layer}] Encoder CC{cc} {direction} ×{times}  →  "
                           f"{ACTION_LABELS.get(action, action)}")
            else:
                self._emit("log", "unmap",
                           f"[{layer}] Encoder CC{cc} {direction}  →  (keine Aktion)")
        else:
            desc = self._volume_action(f"enc{cc}", enc, norm)
            if desc is None:
                return
            self._emit("log", "map", f"[{layer}] Encoder CC{cc} = {value}  →  {desc} {pct} %")

    def _volume_action(self, key, target, norm):
        """Fuehrt eine Volume-Zuweisung aus; gibt Beschreibung zurueck (None = pausiert/aus)."""
        mode = target.get("mode", "off")
        if mode == "off":
            return None
        if not self.enabled:
            self._paused_log("Lautstärke-Änderung")
            return None
        if mode == "windows":
            self.dispatcher.submit(key, self.win_vol.set_scalar, norm)
            return "Windows-Lautstärke"
        if mode == "sonar":
            ch = target.get("channel", "game")
            self.dispatcher.submit(key, lambda v, c=ch: self.sonar.set_volume(c, v), norm)
            return f"Sonar {SONAR_NAMES.get(ch, ch)}"
        if mode == "app":
            proc = target.get("process", "")
            if not proc:
                return None
            self.dispatcher.submit(key, lambda v, p=proc: self.app_vol.set(p, v), norm)
            return proc
        return None

    def _handle_button(self, note):
        layer = layer_of_note(note)
        if layer is None:
            self._emit("log", "unmap", f"Note {note}  →  (unbekannt)")
            return
        with self.lock:
            b = self.layer_cfg(layer).get("buttons", {}).get(str(note))
        action = (b or {}).get("action", "none")
        if not self.enabled:
            self._paused_log(f"[{layer}] Button Note {note}")
            return
        if b is None or action == "none":
            self._emit("log", "unmap", f"[{layer}] Button Note {note}  →  (nicht belegt)")
            return
        press_key(action)
        self._emit("log", "map",
                   f"[{layer}] Button Note {note}  →  {ACTION_LABELS.get(action, action)}")


# ------------------------------------------------------- Prozess-Ueberwachung

class ProcessWatcher(threading.Thread):
    """Prueft alle 2 s laufende Prozesse fuer LED-Modus 'an wenn Prozess laeuft'."""

    def __init__(self, bridge: MidiBridge):
        super().__init__(daemon=True)
        self.bridge = bridge
        self._last = {}

    def run(self):
        while True:
            try:
                with self.bridge.lock:
                    watched = {}
                    for name in ("A", "B"):
                        for n, b in self.bridge.layer_cfg(name).get("buttons", {}).items():
                            if b.get("led", {}).get("mode") == "process":
                                watched[int(n)] = b["led"].get("process", "").lower()
                if watched and self.bridge.connected:
                    running = {p.info["name"].lower()
                               for p in psutil.process_iter(["name"]) if p.info["name"]}
                    for note, proc in watched.items():
                        state = 1 if proc and proc in running else 0
                        if self._last.get(note) != state:
                            self.bridge.set_led(note, state)
                            self._last[note] = state
            except Exception:
                pass
            time.sleep(2)

    def forget(self):
        self._last = {}


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
    def __init__(self):
        self.cfg = load_config()
        self.events = queue.Queue()
        self.bridge = MidiBridge(self.cfg, self.events)
        self.watcher = ProcessWatcher(self.bridge)
        self.view_layer = "A"

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

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(
            top, text="Mit Windows starten", variable=self.autostart_var,
            command=self._toggle_autostart).pack(side="right")

        # --- Layer-Auswahl
        layerbar = ttk.Frame(self.root)
        layerbar.pack(fill="x", padx=8)
        ttk.Label(layerbar, text="Layer:").pack(side="left")
        self.layer_var = tk.StringVar(value="A")
        for name in ("A", "B"):
            ttk.Radiobutton(layerbar, text=f"Layer {name}", value=name,
                            variable=self.layer_var,
                            command=self._on_layer_selected).pack(side="left", padx=4)
        ttk.Label(layerbar, text="(Ansicht wechselt automatisch zum Layer,"
                                 " der am Gerät benutzt wird)",
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

    def _layer_def(self):
        return LAYERS[self.view_layer]

    def _layer_conf(self):
        return self.cfg["layers"][self.view_layer]

    def _draw_device(self):
        c = self.canvas
        c.delete("all")
        self.canvas_items = {}
        L = self._layer_def()

        # Gehaeuse
        c.create_rectangle(10, 10, 716, CANVAS_H - 10, fill=C_DEV,
                           outline="#3f3f46", width=2)
        c.create_text(360, 26, text=f"BEHRINGER  X-TOUCH  MINI   —   LAYER {self.view_layer}",
                      fill=C_DIM, font=("Segoe UI", 8))

        # Encoder-Reihe
        for i in range(8):
            cx = COL_X0 + i * COL_DX
            cy = 78
            cc = L["enc_ccs"][i]
            ring = c.create_oval(cx - 25, cy - 25, cx + 25, cy + 25,
                                 fill=C_CTRL, outline=C_OUTLINE, width=2,
                                 tags=(f"enc{cc}",))
            knob = c.create_oval(cx - 14, cy - 14, cx + 14, cy + 14,
                                 fill="#39393d", outline=C_OUTLINE,
                                 tags=(f"enc{cc}",))
            label = c.create_text(cx, cy + 38, text="", fill=C_TEXT,
                                  font=("Segoe UI", 7), width=72,
                                  tags=(f"enc{cc}",), justify="center")
            c.create_text(cx, cy - 33, text=f"CC{cc}", fill=C_DIM, font=("Segoe UI", 7))
            self.canvas_items[("enc", cc)] = {"ring": ring, "knob": knob, "label": label}
            c.tag_bind(f"enc{cc}", "<Button-1>", lambda e, n=cc: self._open_encoder_dialog(n))

        # Buttons: 2 Reihen zu 8
        for row, notes in enumerate((L["top_notes"], L["bot_notes"])):
            for i, note in enumerate(notes):
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
                c.tag_bind(f"btn{note}", "<Button-1>",
                           lambda e, n=note: self._open_button_dialog(n))

        # Fader
        fx = FADER_X
        c.create_text(fx, 40, text=f"CC{L['fader_cc']}", fill=C_DIM, font=("Segoe UI", 7))
        c.create_rectangle(fx - 4, 56, fx + 4, 262, fill="#151517", outline=C_OUTLINE,
                           tags=("fader",))
        handle = c.create_rectangle(fx - 19, 150, fx + 19, 172, fill=C_CTRL,
                                    outline=C_OUTLINE, width=2, tags=("fader",))
        flabel = c.create_text(fx, 284, text="", fill=C_TEXT, font=("Segoe UI", 7),
                               width=76, tags=("fader",), justify="center")
        self.canvas_items["fader"] = {"handle": handle, "label": flabel, "x": fx}
        c.tag_bind("fader", "<Button-1>", lambda e: self._open_fader_dialog())

        # Hinweis rechts
        c.create_text(754, 90, text="Klicken =\nkonfigu-\nrieren", fill=C_DIM,
                      font=("Segoe UI", 8), justify="center")
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
            cw = SHORT_LABELS.get(e.get("cw", "none"), "")
            ccw = SHORT_LABELS.get(e.get("ccw", "none"), "")
            return f"↻{cw}\n↺{ccw}"
        return ""

    def _refresh_labels(self):
        c = self.canvas
        L = self._layer_def()
        conf = self._layer_conf()
        for i, cc in enumerate(L["enc_ccs"]):
            e = conf.get("encoders", {}).get(str(cc), {"mode": "off"})
            text = self._enc_short(e)
            push_note = L["push_notes"][i]
            push = conf.get("buttons", {}).get(str(push_note), {})
            push_a = push.get("action", "none")
            if push_a != "none":
                text += ("\n" if text else "") + "⬇" + SHORT_LABELS.get(push_a, "")
            c.itemconfigure(self.canvas_items[("enc", cc)]["label"], text=text)
        for note in L["top_notes"] + L["bot_notes"]:
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

    def _on_layer_selected(self):
        self.view_layer = self.layer_var.get()
        self._draw_device()

    def _ensure_layer_visible(self, layer):
        if layer != self.view_layer:
            self.view_layer = layer
            self.layer_var.set(layer)
            self._draw_device()

    # ----------------------------------------------------- Canvas-Live-Effekte

    def _button_base_color(self, note) -> str:
        led = self._layer_conf().get("buttons", {}).get(str(note), {}) \
                                 .get("led", {}).get("mode", "off")
        return C_LEDON if led in ("on", "blink") else C_CTRL

    def _push_note_to_cc(self, note):
        L = self._layer_def()
        if note in L["push_notes"]:
            return L["enc_ccs"][L["push_notes"].index(note)]
        return None

    def _on_press(self, note):
        layer = layer_of_note(note)
        if layer is None:
            return
        self._ensure_layer_visible(layer)
        item = self.canvas_items.get(("btn", note))
        if item:
            self.canvas.itemconfigure(item["rect"], fill=C_PRESS)
            return
        cc = self._push_note_to_cc(note)
        if cc is not None:
            self.canvas.itemconfigure(self.canvas_items[("enc", cc)]["knob"], fill=C_PRESS)

    def _on_release(self, note):
        layer = layer_of_note(note)
        if layer is None or layer != self.view_layer:
            return
        item = self.canvas_items.get(("btn", note))
        if item:
            self.canvas.itemconfigure(item["rect"], fill=self._button_base_color(note))
            return
        cc = self._push_note_to_cc(note)
        if cc is not None:
            self.canvas.itemconfigure(self.canvas_items[("enc", cc)]["knob"], fill="#39393d")

    def _on_enc_turn(self, layer, cc):
        self._ensure_layer_visible(layer)
        item = self.canvas_items.get(("enc", cc))
        if not item:
            return
        self.canvas.itemconfigure(item["ring"], outline=C_PRESS, width=3)
        old = self._flash_jobs.pop(cc, None)
        if old:
            self.root.after_cancel(old)
        self._flash_jobs[cc] = self.root.after(
            180, lambda: self.canvas.itemconfigure(
                item["ring"], outline=C_OUTLINE, width=2))

    def _on_fader_move(self, layer, value):
        self._ensure_layer_visible(layer)
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

    def _action_combo(self, parent, current):
        var = tk.StringVar(value=ACTION_LABELS.get(current, ACTION_LABELS["none"]))
        combo = ttk.Combobox(parent, textvariable=var, width=24, state="readonly",
                             values=list(ACTION_LABELS.values()))
        return var, combo

    def _open_button_dialog(self, note):
        layer = self.view_layer
        conf = self._layer_conf()
        b = conf.get("buttons", {}).get(str(note), {})
        win = self._dialog(f"Layer {layer} – Button (Note {note})")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Aktion beim Drücken:").grid(row=0, column=0, sticky="w")
        action_var, combo = self._action_combo(frame, b.get("action", "none"))
        combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(frame, text="LED der Taste:").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        led = b.get("led", {"mode": "off"})
        led_var = tk.StringVar(value=led.get("mode", "off"))
        ledframe = ttk.Frame(frame)
        ledframe.grid(row=1, column=1, sticky="w", pady=(10, 0))
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

        def ok():
            entry = {"action": LABEL_TO_ACTION.get(action_var.get(), "none")}
            mode = led_var.get()
            led_cfg = {"mode": mode}
            if mode == "process":
                led_cfg["process"] = proc_var.get().strip()
            entry["led"] = led_cfg
            with self.bridge.lock:
                conf.setdefault("buttons", {})[str(note)] = entry
            save_config(self.cfg)
            self.watcher.forget()
            self.bridge.apply_leds()
            self._refresh_labels()
            self._log("info", f"[{layer}] Button Note {note} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=2, pady=(12, 0), sticky="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    def _volume_mode_ui(self, frame, current, with_hotkey):
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
        ttk.Label(frame, text="   (Liste = Apps mit Audio-Session; App muss mal Ton"
                              " ausgegeben haben. Manuell: z. B. spotify.exe)",
                  foreground="#777").pack(anchor="w")

        cw_var = ccw_var = None
        if with_hotkey:
            ttk.Radiobutton(frame, text="Tastenkürzel pro Klick", value="hotkey",
                            variable=mode_var).pack(anchor="w", pady=(4, 0))
            grid = ttk.Frame(frame)
            grid.pack(anchor="w", padx=20)
            ttk.Label(grid, text="↻ im Uhrzeigersinn:").grid(row=0, column=0, sticky="w")
            cw_var, cw_combo = self._action_combo(grid, current.get("cw", "none"))
            cw_combo.grid(row=0, column=1, padx=4, pady=2)
            ttk.Label(grid, text="↺ gegen Uhrzeigersinn:").grid(row=1, column=0, sticky="w")
            ccw_var, ccw_combo = self._action_combo(grid, current.get("ccw", "none"))
            ccw_combo.grid(row=1, column=1, padx=4, pady=2)

        def result():
            mode = mode_var.get()
            out = {"mode": mode}
            if mode == "sonar":
                out["channel"] = SONAR_LABEL_TO_KEY.get(sonar_var.get(), "game")
            elif mode == "app":
                out["process"] = app_var.get().strip()
                if not out["process"]:
                    out["mode"] = "off"
            elif mode == "hotkey" and cw_var is not None:
                out["cw"] = LABEL_TO_ACTION.get(cw_var.get(), "none")
                out["ccw"] = LABEL_TO_ACTION.get(ccw_var.get(), "none")
            return out

        return result

    def _open_encoder_dialog(self, cc):
        layer = self.view_layer
        L = self._layer_def()
        conf = self._layer_conf()
        current = conf.get("encoders", {}).get(str(cc), {"mode": "off"})
        push_note = L["push_notes"][L["enc_ccs"].index(cc)]
        push = conf.get("buttons", {}).get(str(push_note), {})

        win = self._dialog(f"Layer {layer} – Encoder CC{cc}")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Drehen:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        get_turn = self._volume_mode_ui(frame, current, with_hotkey=True)

        ttk.Separator(frame).pack(fill="x", pady=8)
        ttk.Label(frame, text="Drücken (Push):", font=("Segoe UI", 9, "bold")
                  ).pack(anchor="w")
        push_var, push_combo = self._action_combo(frame, push.get("action", "none"))
        push_combo.pack(anchor="w", pady=4)

        def ok():
            turn = get_turn()
            with self.bridge.lock:
                conf.setdefault("encoders", {})[str(cc)] = turn
                conf.setdefault("buttons", {})[str(push_note)] = {
                    "action": LABEL_TO_ACTION.get(push_var.get(), "none")}
            save_config(self.cfg)
            self.bridge.reset_hotkey_encoders()
            self._refresh_labels()
            self._log("info", f"[{layer}] Encoder CC{cc} gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

    def _open_fader_dialog(self):
        layer = self.view_layer
        conf = self._layer_conf()
        current = conf.get("fader", {"mode": "off"})
        win = self._dialog(f"Layer {layer} – Fader")
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
            self._log("info", f"[{layer}] Fader gespeichert")
            win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(pady=(12, 0), anchor="e")
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Abbrechen", command=win.destroy).pack(side="left")

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
                    self._on_press(payload[0])
                elif kind == "release":
                    self._on_release(payload[0])
                elif kind == "enc":
                    self._on_enc_turn(payload[0], payload[1])
                elif kind == "fader":
                    self._on_fader_move(payload[0], payload[1])
        except queue.Empty:
            pass
        self.status_var.set(
            "● Verbunden" if self.bridge.connected else "○ Controller nicht gefunden – suche…")
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
    App().run()


if __name__ == "__main__":
    main()
