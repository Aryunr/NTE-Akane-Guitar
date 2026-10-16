"""MIDI timeline and one reusable, interruptible playback worker (Python 3.11+)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time

import mido


@dataclass(frozen=True)
class NoteEvent:
    time: float
    note: int
    on: bool


@dataclass(frozen=True)
class Song:
    path: str
    duration: float
    note_count: int
    skipped: int
    events: tuple[NoteEvent, ...]


def load_mapping(path: str | Path) -> dict[int, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("键位配置必须是 JSON 对象")
    result = {}
    for number, key in data.items():
        note = int(number)
        if not 0 <= note <= 127:
            raise ValueError(f"无效 MIDI 编号：{number}")
        if key is None or key == "":
            continue
        if not isinstance(key, str):
            raise ValueError(f"音符 {note} 的键位必须是字符串")
        key = key.lower()
        key_vk(key)  # validate before playback
        result[note] = key
    if not result:
        raise ValueError("键位配置不能为空")
    return result


def key_vk(key: str) -> int:
    """Windows virtual keys; OEM punctuation avoids Unicode/VK_PACKET injection."""
    special = {"enter": 0x0D, ";": 0xBA, "'": 0xDE, "[": 0xDB,
               "]": 0xDD, "-": 0xBD, "=": 0xBB, ",": 0xBC,
               ".": 0xBE, "/": 0xBF, "\\": 0xDC, "`": 0xC0,
               "space": 0x20, "tab": 0x09}
    if key in special:
        return special[key]
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        return ord(key.upper())
    raise ValueError(f"不支持的键位 {key!r}；使用英文字母、数字、标点、enter/space/tab")


def load_song(path: str | Path, mapping: dict[int, str]) -> Song:
    midi = mido.MidiFile(str(path))
    if midi.type == 2:
        raise ValueError("暂不支持 Type 2 独立序列 MIDI，请转换为 Type 0 或 Type 1")
    # MidiFile iteration merges tracks and converts delta ticks using tempo changes.
    now = 0.0
    count = skipped = 0
    active: Counter = Counter()
    events = []
    for msg in midi:
        now += msg.time
        if msg.type in ("note_on", "note_off"):
            identity = (msg.channel, msg.note)
            on = msg.type == "note_on" and msg.velocity > 0
            if on:
                count += 1
                if msg.note not in mapping:
                    skipped += 1
                    continue
                active[identity] += 1
                events.append(NoteEvent(now, msg.note, True))
            elif active[identity]:
                active[identity] -= 1
                events.append(NoteEvent(now, msg.note, False))
        elif msg.type == "control_change" and msg.control in (120, 123):
            for (channel, note), amount in list(active.items()):
                if channel == msg.channel and amount:
                    events.extend(NoteEvent(now, note, False) for _ in range(amount))
                    active[(channel, note)] = 0
    # Repair missing note_off at end of malformed but readable files.
    for (_, note), amount in active.items():
        events.extend(NoteEvent(now, note, False) for _ in range(amount))
    return Song(str(path), now, count, skipped, tuple(events))


class KeyboardOutput:
    def __init__(self, mapping: dict[int, str]):
        from pynput.keyboard import Controller, KeyCode
        self.controller = Controller()
        self.keys = {name: KeyCode.from_vk(key_vk(name)) for name in set(mapping.values())}

    def press(self, key):
        self.controller.press(self.keys[key])

    def release(self, key):
        self.controller.release(self.keys[key])


class Player:
    def __init__(self, output, mapping: dict[int, str]):
        self.output = output
        self.mapping = mapping.copy()
        self.cv = threading.Condition()
        self.state = "stopped"
        self.song = None
        self.position = 0.0
        self.speed = 1.0
        self.anchor = time.monotonic()
        self.deadline = 0.0
        self.index = 0
        self.counts = Counter()
        self.pressed = set()
        self.error = ""
        self.closed = False
        self.worker = threading.Thread(target=self._run, name="midi-playback", daemon=True)
        self.worker.start()

    def _advance(self):
        now = time.monotonic()
        if self.state == "playing":
            self.position = min(self.song.duration, self.position + (now - self.anchor) * self.speed)
        self.anchor = now

    def _release(self):
        failures = []
        for key in tuple(self.pressed):
            try:
                self.output.release(key)
                self.pressed.discard(key)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            self.error = "松键失败：" + "; ".join(failures)

    def _press(self, key):
        # Track before sending so a partial failure still gets a release attempt.
        self.pressed.add(key)
        self.output.press(key)

    def play(self, song: Song, delay=3.0):
        with self.cv:
            self._release()
            if self.pressed:
                raise RuntimeError(self.error)
            self.song = song
            self.position = 0.0
            self.index = 0
            self.counts.clear()
            self.error = ""
            self.state = "countdown"
            self.deadline = time.monotonic() + delay
            self.cv.notify_all()

    def pause(self):
        with self.cv:
            if self.state in ("playing", "countdown"):
                self._advance()
                self.state = "paused"
                self._release()
                self.cv.notify_all()

    def resume(self, delay=3.0):
        with self.cv:
            if self.state == "paused":
                self.state = "countdown"
                self.deadline = time.monotonic() + delay
                self.cv.notify_all()

    def stop(self):
        with self.cv:
            self.state = "stopped"
            self.position = 0.0
            self.counts.clear()
            self._release()
            self.cv.notify_all()

    def set_speed(self, speed):
        if speed not in (0.5, 0.75, 1.0, 1.25):
            raise ValueError("无效速度倍率")
        with self.cv:
            self._advance()
            self.speed = speed
            self.cv.notify_all()

    def snapshot(self):
        with self.cv:
            self._advance()
            return self.state, self.position, max(0, self.deadline-time.monotonic()), self.error

    def close(self):
        with self.cv:
            self.stop()
            self.closed = True
            self.cv.notify_all()
        self.worker.join(timeout=2)
        with self.cv:
            self._release()  # retry any failed releases

    def _run(self):
        with self.cv:
            while not self.closed:
                try:
                    if self.state == "countdown":
                        remaining = self.deadline - time.monotonic()
                        if remaining > 0:
                            self.cv.wait(remaining)
                            continue
                        for key, amount in self.counts.items():
                            if amount and key not in self.pressed:
                                self._press(key)
                        self.anchor = time.monotonic()
                        self.state = "playing"
                    if self.state != "playing":
                        self.cv.wait()
                        continue
                    self._advance()
                    events = self.song.events
                    # Process one simultaneous timestamp together; let stop acquire the
                    # lock between batches, even for dense or malicious MIDI files.
                    if self.index < len(events) and events[self.index].time <= self.position:
                        timestamp = events[self.index].time
                        batch_end = self.index + 256
                        while (self.index < len(events) and self.index < batch_end
                               and events[self.index].time == timestamp):
                            event = events[self.index]
                            key = self.mapping[event.note]
                            if event.on:
                                if not self.counts[key]:
                                    self._press(key)
                                self.counts[key] += 1
                            elif self.counts[key]:
                                self.counts[key] -= 1
                                if not self.counts[key] and key in self.pressed:
                                    self.output.release(key)
                                    self.pressed.discard(key)
                            self.index += 1
                        self.cv.wait(0)  # release lock between timestamp batches
                        continue
                    if self.index == len(events) and self.position >= self.song.duration:
                        self._release()
                        self.counts.clear()
                        self.state = "finished"
                        continue
                    target = events[self.index].time if self.index < len(events) else self.song.duration
                    self.cv.wait(max(0, (target-self.position)/self.speed))
                except Exception as exc:
                    self.error = str(exc)
                    self.state = "error"
                    self._release()
                    self.counts.clear()
