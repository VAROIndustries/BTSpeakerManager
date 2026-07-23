"""
BT Speaker Manager — System Tray Application

Manages Bluetooth speaker connection with automatic reconnection,
keep-alive to prevent sleep, and a system tray interface for control.

Replaces the PowerShell scripts (Reconnect-DeskSoundbar.ps1, Keep-Alive.ps1)
with a single lightweight app that can be compiled to a standalone EXE.
"""

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import wave
import winreg
import winsound
from pathlib import Path

import comtypes
from comtypes import COMMETHOD, HRESULT
import pystray
from PIL import Image, ImageDraw, ImageFont
from pycaw.pycaw import AudioUtilities


# ── Paths ────────────────────────────────────────────────────────

APP_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).parent
)
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "bt_manager.log"
SILENCE_WAV = APP_DIR / "silence.wav"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(str(LOG_FILE), encoding="utf-8")],
)
log = logging.getLogger("bt")


# ── Configuration ────────────────────────────────────────────────

DEFAULTS = {
    "speaker_name": "Desk Soundbar",
    "bt_instance_id": r"BTHENUM\DEV_5415896DE33F\7&3F54149&0&BLUETOOTHDEVICE_5415896DE33F",
    "fallback_name": "Realtek",
    "polling_enabled": True,
    "polling_interval": 120,
    "keepalive_enabled": True,
    "run_on_startup": True,
}


def load_cfg() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_cfg(c: dict):
    CONFIG_PATH.write_text(json.dumps(c, indent=2))


# ── Silence WAV generation ──────────────────────────────────────

def ensure_wav():
    """Generate a 10-second silent WAV if it doesn't exist."""
    if SILENCE_WAV.exists():
        return
    with wave.open(str(SILENCE_WAV), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(1)
        f.setframerate(8000)
        # 0x80 is silence for unsigned 8-bit PCM
        f.writeframes(bytes([0x80] * 80000))
    log.info("Generated silence.wav")


# ── COM: IPolicyConfig for setting default audio device ──────────
#
# Undocumented but stable since Vista. The vtable has 10 placeholder
# methods before SetDefaultEndpoint (matching the C# interface used
# in the original PowerShell script).

class IPolicyConfig(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat"),
        COMMETHOD([], HRESULT, "GetDeviceFormat"),
        COMMETHOD([], HRESULT, "ResetDeviceFormat"),
        COMMETHOD([], HRESULT, "SetDeviceFormat"),
        COMMETHOD([], HRESULT, "GetProcessingPeriod"),
        COMMETHOD([], HRESULT, "SetProcessingPeriod"),
        COMMETHOD([], HRESULT, "GetShareMode"),
        COMMETHOD([], HRESULT, "SetShareMode"),
        COMMETHOD([], HRESULT, "GetPropertyValue"),
        COMMETHOD([], HRESULT, "SetPropertyValue"),
        COMMETHOD(
            [],
            HRESULT,
            "SetDefaultEndpoint",
            (["in"], ctypes.c_wchar_p, "wszDeviceId"),
            (["in"], ctypes.c_int, "eRole"),
        ),
    ]


_CLSID_PolicyConfig = comtypes.GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")


def set_default_endpoint(device_id: str):
    """Set default audio device for Console and Multimedia roles."""
    policy = comtypes.CoCreateInstance(_CLSID_PolicyConfig, IPolicyConfig)
    policy.SetDefaultEndpoint(device_id, 0)  # eConsole
    policy.SetDefaultEndpoint(device_id, 1)  # eMultimedia


# ── Audio device helpers ────────────────────────────────────────

_PS_PATH = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_MMDEVAPI_PREFIX = "SWD\\MMDEVAPI\\"


def _run_ps(command: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a PowerShell command with proper path and flags."""
    return subprocess.run(
        [_PS_PATH, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def enum_devices() -> list[dict] | None:
    """Return active audio output endpoints as [{id, name}, ...].

    Returns None when the query itself failed (PowerShell timeout, non-zero
    exit, or unparseable output) so callers can distinguish "query
    unavailable" from "no devices present". A real machine always has at
    least one audio endpoint, so an empty/failed result means the query is
    unreliable — never that every device vanished. Treating that as a real
    empty list is what caused the false-disconnect + reconnect-storm bug
    (a single PS timeout across a sleep/resume flipped the app to
    "Disconnected — no devices found"). Retries once before giving up.
    """
    for attempt in range(2):
        try:
            r = _run_ps(
                "Get-PnpDevice -Class AudioEndpoint -Status OK | "
                "Select-Object InstanceId, FriendlyName | "
                "ConvertTo-Json -Compress"
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue  # transient failure — retry, then report None
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                data = [data]
            # Strip the SWD\MMDEVAPI\ prefix in Python (avoids PS regex escaping hell)
            # Lowercase IDs for case-insensitive matching with pycaw
            for d in data:
                raw_id = d.get("InstanceId", "")
                stripped = raw_id.replace(_MMDEVAPI_PREFIX, "", 1) if raw_id else ""
                d["id"] = stripped.lower()
                d["name"] = d.get("FriendlyName", "")
            return data
        except Exception as e:
            log.error("enum_devices (attempt %d): %s", attempt + 1, e)
    return None


def current_default_id() -> str | None:
    """Get the MMDevice ID of the current default render endpoint (lowercase)."""
    try:
        speakers = AudioUtilities.GetSpeakers()
        return speakers.id.lower() if speakers and speakers.id else None
    except Exception:
        return None


def find_dev(devs: list[dict], name_fragment: str) -> dict | None:
    """Find a device whose name contains the fragment (case-insensitive)."""
    frag = name_fragment.lower()
    for d in devs:
        if frag in d["name"].lower():
            return d
    return None


def bt_reconnect(instance_id: str):
    """Restart a Bluetooth device via pnputil (no admin needed)."""
    try:
        subprocess.run(
            ["pnputil", "/restart-device", instance_id],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.error("bt_reconnect: %s", e)


def detect_bt_id(speaker_name: str) -> str | None:
    """Try to auto-detect the BT instance ID for a speaker name."""
    try:
        r = _run_ps(
            f"Get-PnpDevice -Class Bluetooth -Status OK | "
            f"Where-Object {{ $_.FriendlyName -like '*{speaker_name}*' }} | "
            f"Select-Object -ExpandProperty InstanceId -First 1",
            timeout=10,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def extract_name(endpoint_name: str) -> str:
    """Extract core device name: 'Speakers (Desk Soundbar)' → 'Desk Soundbar'."""
    m = re.search(r"\((.+)\)$", endpoint_name)
    return m.group(1) if m else endpoint_name


# ── Tray icon generation ────────────────────────────────────────

def _make_icon(color: str) -> Image.Image:
    """Create a 64x64 tray icon: colored circle with 'B' letter."""
    sz = 64
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, sz - 4, sz - 4], fill=color)
    try:
        font = ImageFont.truetype("segoeui.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((sz / 2, sz / 2), "B", fill="white", font=font, anchor="mm")
    return img


ICO_CONNECTED = _make_icon("#2196F3")  # Blue
ICO_DISCONNECTED = _make_icon("#757575")  # Gray
ICO_PAUSED = _make_icon("#FF9800")  # Orange


# ── Startup registry management ─────────────────────────────────

_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "BTSpeakerManager"


def _exe_path() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Use pythonw.exe (windowless) instead of python.exe for startup
    py = sys.executable
    if py.endswith("python.exe"):
        py = py.replace("python.exe", "pythonw.exe")
    return f'"{py}" "{os.path.abspath(__file__)}"'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_READ
        ) as k:
            winreg.QueryValueEx(k, _REG_NAME)
            return True
    except FileNotFoundError:
        return False


def set_startup(enable: bool):
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE
    ) as k:
        if enable:
            winreg.SetValueEx(k, _REG_NAME, 0, winreg.REG_SZ, _exe_path())
        else:
            try:
                winreg.DeleteValue(k, _REG_NAME)
            except FileNotFoundError:
                pass


# ── Main Application ────────────────────────────────────────────

class App:
    def __init__(self):
        self.cfg = load_cfg()
        self._stop = threading.Event()
        self._connected = False
        self._ka_on = False
        self._devs: list[dict] = []
        self._icon: pystray.Icon | None = None
        self._ka_lock = threading.Lock()
        # Exponential reconnect backoff (monotonic clock, immune to sleep/resume
        # wall-clock jumps). Prevents the fixed-30s pnputil hammer loop.
        self._reconnect_attempts = 0
        self._next_reconnect = 0.0

    # ── Keep-alive (continuous silent audio loop) ────────────

    def _ka_start(self):
        with self._ka_lock:
            if self._ka_on or not self.cfg.get("keepalive_enabled"):
                return
            ensure_wav()
            try:
                winsound.PlaySound(
                    str(SILENCE_WAV),
                    winsound.SND_FILENAME
                    | winsound.SND_LOOP
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
                self._ka_on = True
                log.info("Keep-alive started")
            except Exception as e:
                log.error("Keep-alive start failed: %s", e)

    def _ka_stop(self):
        with self._ka_lock:
            if not self._ka_on:
                return
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self._ka_on = False
            log.info("Keep-alive stopped")

    # ── Polling / reconnection (state-transition based) ──────

    def _poll_once(self):
        """
        Check speaker status and react to state TRANSITIONS only.
        - connected → disconnected: fall back to Realtek
        - disconnected → connected: set speaker as default
        - still disconnected: attempt reconnection
        Never re-sets an already-correct default (fixes the audio blip).
        """
        name = self.cfg["speaker_name"]
        bt_id = self.cfg.get("bt_instance_id", "")
        fallback = self.cfg.get("fallback_name", "Realtek")

        devs = enum_devices()
        if devs is None:
            # Query failed (e.g. PS timeout across sleep/resume). "Unknown" is
            # NOT "disconnected" — keep the previous state and device list so a
            # transient hiccup never triggers a false disconnect or wipes the
            # menu to "(no devices found)".
            log.info("Device query unavailable; keeping previous state")
            self._update_icon()
            return
        self._devs = devs
        was = self._connected
        now = find_dev(devs, name) is not None

        if now and not was:
            # TRANSITION: disconnected → connected
            self._connected = True
            self._reset_backoff()
            log.info("'%s' connected", name)
            spk = find_dev(devs, name)
            if spk:
                cur = current_default_id()
                if cur != spk["id"]:
                    try:
                        set_default_endpoint(spk["id"])
                        log.info("Default → '%s'", spk["name"])
                    except Exception as e:
                        log.error("Set default failed: %s", e)
                    # Restart keep-alive on the new device
                    self._ka_stop()
                    self._ka_start()
                elif not self._ka_on:
                    self._ka_start()

        elif not now and was:
            # TRANSITION: connected → disconnected
            self._connected = False
            log.info("'%s' disconnected", name)
            self._ka_stop()
            fb = find_dev(devs, fallback)
            if fb:
                try:
                    set_default_endpoint(fb["id"])
                    log.info("Fallback → '%s'", fb["name"])
                except Exception as e:
                    log.error("Fallback failed: %s", e)

        elif not now and not was and bt_id:
            # Still disconnected — attempt reconnect with exponential backoff
            # (30s, 60s, 120s, 240s, capped at 300s). Prevents the fixed-30s
            # pnputil storm when the speaker is simply powered off.
            if time.monotonic() < self._next_reconnect:
                self._update_icon()
                return
            self._reconnect_attempts += 1
            delay = min(30 * (2 ** (self._reconnect_attempts - 1)), 300)
            self._next_reconnect = time.monotonic() + delay
            log.info(
                "Attempting reconnect '%s' (attempt %d, next in %ds)...",
                name, self._reconnect_attempts, delay,
            )
            bt_reconnect(bt_id)
            time.sleep(5)
            # Re-check after reconnect attempt
            devs = enum_devices()
            if devs is not None:
                self._devs = devs
                if find_dev(devs, name) is not None:
                    self._connected = True
                    self._reset_backoff()
                    log.info("Reconnected '%s'", name)
                    spk = find_dev(devs, name)
                    if spk:
                        try:
                            set_default_endpoint(spk["id"])
                            log.info("Default → '%s'", spk["name"])
                        except Exception as e:
                            log.error("Set default failed: %s", e)
                    self._ka_start()

        self._update_icon()

    def _reset_backoff(self):
        """Clear reconnect backoff after a successful (re)connection."""
        self._reconnect_attempts = 0
        self._next_reconnect = 0.0

    def _poll_loop(self):
        """Background thread: manage speaker connection."""
        comtypes.CoInitialize()
        try:
            # Detect initial state (avoid false transition on first poll)
            devs = enum_devices()
            name = self.cfg["speaker_name"]
            if devs is None:
                # Can't tell yet — leave state as-is; the poll loop resolves it.
                log.info("Startup: device query unavailable")
            else:
                self._devs = devs
                spk = find_dev(devs, name)
                if spk:
                    self._connected = True
                    log.info("Startup: '%s' already connected", name)
                    cur = current_default_id()
                    if cur != spk["id"]:
                        try:
                            set_default_endpoint(spk["id"])
                            log.info("Startup: default → '%s'", spk["name"])
                        except Exception as e:
                            log.error("Startup set default: %s", e)
                    self._ka_start()
                else:
                    self._connected = False
                    log.info("Startup: '%s' not connected", name)
            self._update_icon()

            # Poll loop
            while not self._stop.is_set():
                self._stop.wait(self.cfg.get("polling_interval", 120))
                if self._stop.is_set():
                    break
                if self.cfg.get("polling_enabled", True):
                    try:
                        self._poll_once()
                    except Exception as e:
                        log.error("Poll error: %s", e)
        finally:
            comtypes.CoUninitialize()

    # ── Icon / tooltip updates ───────────────────────────────

    def _update_icon(self):
        if not self._icon:
            return
        if not self.cfg.get("polling_enabled"):
            self._icon.icon = ICO_PAUSED
            self._icon.title = "BT Speaker \u2014 Paused"
        elif self._connected:
            self._icon.icon = ICO_CONNECTED
            self._icon.title = f"BT Speaker \u2014 {self.cfg['speaker_name']}"
        else:
            self._icon.icon = ICO_DISCONNECTED
            self._icon.title = "BT Speaker \u2014 Disconnected"

    # ── Dynamic tray menu ────────────────────────────────────

    def _menu_items(self):
        """Called by pystray each time the context menu opens."""
        cfg = self.cfg
        name = cfg["speaker_name"]

        # Status line
        if not cfg.get("polling_enabled"):
            status = f"{name} (Paused)"
        elif self._connected:
            status = f"{name} (Connected)"
        else:
            status = f"{name} (Disconnected)"

        # Target speaker submenu
        devs = self._devs or []
        speaker_items = []
        for d in devs:
            speaker_items.append(
                pystray.MenuItem(
                    d["name"],
                    self._action_set_target(d["name"]),
                    checked=lambda item, n=d["name"]: (
                        cfg["speaker_name"].lower() in n.lower()
                    ),
                    radio=True,
                )
            )
        if not speaker_items:
            speaker_items.append(
                pystray.MenuItem("(no devices found)", None, enabled=False)
            )

        # Polling interval submenu
        intervals = [
            ("30 seconds", 30),
            ("1 minute", 60),
            ("2 minutes", 120),
            ("5 minutes", 300),
            ("10 minutes", 600),
        ]
        interval_items = [
            pystray.MenuItem(
                label,
                self._action_set_interval(secs),
                checked=lambda item, s=secs: cfg.get("polling_interval", 120) == s,
                radio=True,
            )
            for label, secs in intervals
        ]

        return (
            pystray.MenuItem(status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Target Speaker", pystray.Menu(*speaker_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Polling",
                self._action_toggle_polling,
                checked=lambda item: cfg.get("polling_enabled", True),
            ),
            pystray.MenuItem("Polling Interval", pystray.Menu(*interval_items)),
            pystray.MenuItem(
                "Keep-Alive",
                self._action_toggle_keepalive,
                checked=lambda item: cfg.get("keepalive_enabled", True),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                self._action_toggle_startup,
                checked=lambda item: startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reconnect Now", self._action_reconnect),
            pystray.MenuItem(
                "Open Logs", lambda icon, item: os.startfile(str(LOG_DIR))
            ),
            pystray.MenuItem("Exit", self._action_quit),
        )

    # ── Menu actions ─────────────────────────────────────────

    def _threaded_poll(self):
        """Run _poll_once in a new thread with its own COM initialization."""

        def run():
            comtypes.CoInitialize()
            try:
                self._poll_once()
            except Exception as e:
                log.error("Threaded poll: %s", e)
            finally:
                comtypes.CoUninitialize()

        threading.Thread(target=run, daemon=True).start()

    def _action_set_target(self, display_name: str):
        def action(icon, item):
            name = extract_name(display_name)
            self.cfg["speaker_name"] = name
            # Auto-detect BT instance ID for reconnection
            bt_id = detect_bt_id(name)
            self.cfg["bt_instance_id"] = bt_id or ""
            save_cfg(self.cfg)
            log.info("Target → '%s' (bt_id: %s)", name, bt_id or "none")
            # Force re-evaluation on next poll
            self._connected = False
            self._reset_backoff()
            self._threaded_poll()

        return action

    def _action_set_interval(self, seconds: int):
        def action(icon, item):
            self.cfg["polling_interval"] = seconds
            save_cfg(self.cfg)
            log.info("Polling interval → %ds", seconds)

        return action

    def _action_toggle_polling(self, icon, item):
        self.cfg["polling_enabled"] = not self.cfg.get("polling_enabled", True)
        save_cfg(self.cfg)
        log.info(
            "Polling %s", "enabled" if self.cfg["polling_enabled"] else "disabled"
        )
        self._update_icon()

    def _action_toggle_keepalive(self, icon, item):
        self.cfg["keepalive_enabled"] = not self.cfg.get("keepalive_enabled", True)
        save_cfg(self.cfg)
        if self.cfg["keepalive_enabled"] and self._connected:
            self._ka_start()
        else:
            self._ka_stop()
        log.info(
            "Keep-alive %s",
            "enabled" if self.cfg["keepalive_enabled"] else "disabled",
        )

    def _action_toggle_startup(self, icon, item):
        currently_on = startup_enabled()
        set_startup(not currently_on)
        self.cfg["run_on_startup"] = not currently_on
        save_cfg(self.cfg)
        log.info("Startup %s", "disabled" if currently_on else "enabled")

    def _action_reconnect(self, icon, item):
        self._connected = False  # Force transition detection
        self._reset_backoff()  # Manual reconnect ignores backoff window
        self._threaded_poll()

    def _action_quit(self, icon, item):
        log.info("=== BT Speaker Manager exiting ===")
        self._stop.set()
        self._ka_stop()
        icon.stop()

    # ── Entry point ──────────────────────────────────────────

    def run(self):
        log.info("=== BT Speaker Manager started ===")
        ensure_wav()

        # Apply startup setting from config
        if self.cfg.get("run_on_startup") and not startup_enabled():
            set_startup(True)

        # Start background polling thread
        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()

        # Create and run system tray icon (blocks on main thread)
        self._icon = pystray.Icon(
            "BTSpeakerManager",
            ICO_DISCONNECTED,
            "BT Speaker Manager",
            menu=pystray.Menu(self._menu_items),
        )
        self._icon.run()


# ── Script entry point ──────────────────────────────────────────

if __name__ == "__main__":
    comtypes.CoInitialize()
    try:
        App().run()
    finally:
        comtypes.CoUninitialize()
