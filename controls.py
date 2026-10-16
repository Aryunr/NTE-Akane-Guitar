"""Persisted settings and debounced global hotkeys; no tkinter calls here."""
import json
import math
from pathlib import Path
import threading

from midi_player import key_vk

DEFAULTS = {"play": "f6", "pause": "f7", "stop": "f8", "delay": 3.0}
MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10}
SPECIAL = {"esc": 0x1B, "home": 0x24, "end": 0x23,
           "insert": 0x2D, "delete": 0x2E, "page_up": 0x21, "page_down": 0x22}


def recorded_hotkey(down, vk):
    """Convert physical Windows keys to the same names used by settings."""
    aliases = {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11,
               0xA4: 0x12, 0xA5: 0x12}
    normalized = {aliases.get(key, key) for key in down}
    if aliases.get(vk, vk) in MODIFIERS.values():
        return None
    if 0x70 <= vk <= 0x87:
        name = f"f{vk - 0x70 + 1}"
    else:
        names = {value: key for key, value in SPECIAL.items()}
        for key in "abcdefghijklmnopqrstuvwxyz0123456789;\'[]-=,./\\`":
            names[key_vk(key)] = key
        names.update({0x0D: "enter", 0x20: "space", 0x09: "tab"})
        name = names.get(vk)
    if name is None:
        raise ValueError("不支持此按键，请使用功能键或其他受支持的键")
    return "+".join([name for name, code in MODIFIERS.items() if code in normalized] + [name])


def parse_hotkey(text):
    parts = text.lower().replace(" ", "").split("+")
    if not parts or not parts[-1]:
        raise ValueError("热键不能为空")
    mods, final = parts[:-1], parts[-1]
    if len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        raise ValueError("组合键格式：ctrl+alt+f6，可用 ctrl、alt、shift")
    if final.startswith("f") and final[1:].isdigit() and 1 <= int(final[1:]) <= 24:
        vk = 0x70 + int(final[1:]) - 1
    elif final in SPECIAL:
        vk = SPECIAL[final]
    else:
        vk = key_vk(final)
    return frozenset(MODIFIERS[m] for m in mods), vk


def validate_settings(data, mapping):
    delay = float(data["delay"])
    if not math.isfinite(delay) or not 0 <= delay <= 3600:
        raise ValueError("倒计时必须是 0～3600 秒的有限数字")
    result = {"delay": delay}
    signatures = set()
    note_keys = {key_vk(k) for k in mapping.values()}
    for action in ("play", "pause", "stop"):
        text = data[action].lower().replace(" ", "")
        signature = parse_hotkey(text)
        if signature in signatures:
            raise ValueError("播放、暂停和停止热键不能重复")
        if signature[1] in note_keys:
            raise ValueError(f"热键 {text} 与演奏键冲突，请改用功能键等其他键")
        signatures.add(signature)
        result[action] = text
    return result


def load_settings(path, mapping):
    path = Path(path)
    return validate_settings(json.loads(path.read_text(encoding="utf-8-sig"))
                             if path.exists() else DEFAULTS, mapping)


def save_settings(path, settings):
    path = Path(path)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


class Hotkeys:
    def __init__(self, settings, callback):
        self.lock = threading.RLock()
        self.callback = callback
        self.down = set()
        self.active = set()
        self.capture = None
        self.configure(settings)

    def set_capture(self, callback):
        with self.lock:
            self.capture = callback
            self.active = self._matches()

    def configure(self, settings):
        with self.lock:
            self.bindings = {action: parse_hotkey(settings[action]) for action in ("play", "pause", "stop")}
            self.active.clear()

    @staticmethod
    def vk(key):
        value = getattr(key, "value", key)
        vk = getattr(value, "vk", None)
        return vk

    def _matches(self):
        mods = set()
        for vk in self.down:
            mods.add({0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11,
                      0xA4: 0x12, 0xA5: 0x12}.get(vk, vk))
        held_mods = mods & set(MODIFIERS.values())
        return {action for action, (required, vk) in self.bindings.items()
                if vk in self.down and held_mods == required}

    def press(self, key, injected=False):
        if injected:
            return
        with self.lock:
            vk = self.vk(key)
            if vk is None:
                return
            self.down.add(vk)
            if self.capture:
                self.active = self._matches()
                try:
                    text = recorded_hotkey(self.down, vk)
                    if text:
                        self.capture(text, None)
                except ValueError as exc:
                    self.capture(None, str(exc))
                return
            matches = self._matches()
            fresh = matches - self.active
            self.active = matches
            for action in ("stop", "pause", "play"):
                if action in fresh:
                    self.callback(action)

    def release(self, key, injected=False):
        if injected:
            return
        with self.lock:
            self.down.discard(self.vk(key))
            # Releasing a modifier must never trigger a simpler shortcut.
            self.active = self._matches()
