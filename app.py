"""Lightweight NTE guitar player. Run: python app.py"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from midi_player import KeyboardOutput, Player, Song, load_mapping, load_song
from controls import DEFAULTS, Hotkeys, load_settings, save_settings, validate_settings


BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = BASE / "keymap.json"
SETTINGS = BASE / "settings.json"


def ensure_config():
    if not CONFIG.exists() and getattr(sys, "frozen", False):
        import shutil
        shutil.copyfile(Path(sys._MEIPASS) / "keymap.json", CONFIG)


def clock_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds//60:02}:{seconds%60:02}"


class App:
    def __init__(self, root):
        self.root = root
        root.title("异环 · 吉他 MIDI")
        root.geometry("660x290")
        root.minsize(640, 290)
        self.song = None
        self.player = None
        self.listener = None
        self.loading = False
        self.closing = False
        self.messages = queue.SimpleQueue()
        self.command_epoch = 0
        self.capture_name = None
        self.capture_epoch = 0
        self.hotkey_entries = {}
        self.settings = dict(DEFAULTS)
        settings_error = ""
        try:
            self.settings = load_settings(SETTINGS, load_mapping(CONFIG))
        except Exception as exc:
            settings_error = f"设置读取失败，已使用默认值：{exc}"
        self.path = tk.StringVar()
        self.info = tk.StringVar(value="请选择 MIDI 文件")
        self.status = tk.StringVar(value="就绪")
        self.error = tk.StringVar(value=settings_error)
        self.speed = tk.StringVar(value="1.0×")
        self.setting_vars = {name: tk.StringVar(value=str(value)) for name, value in self.settings.items()}
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Entry(frame, textvariable=self.path, state="readonly").grid(row=0, column=0, sticky="ew")
        self.select_button = ttk.Button(frame, text="选择文件", command=self.choose)
        self.select_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(frame, textvariable=self.info).grid(row=1, column=0, columnspan=2, sticky="w", pady=12)
        controls = ttk.Frame(frame)
        controls.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, text="速度 ").pack(side="left")
        combo = ttk.Combobox(controls, textvariable=self.speed, values=("0.5×", "0.75×", "1.0×", "1.25×"), state="readonly", width=7)
        combo.pack(side="left", padx=(0, 12))
        combo.bind("<<ComboboxSelected>>", self.change_speed)
        self.play_button = ttk.Button(controls, text="播放", command=self.play, state="disabled")
        self.play_button.pack(side="left", padx=3)
        self.pause_button = ttk.Button(controls, text="暂停", command=self.pause, state="disabled")
        self.pause_button.pack(side="left", padx=3)
        ttk.Button(controls, text="停止", command=self.stop).pack(side="left", padx=3)
        ttk.Button(controls, text="键位配置", command=self.edit_mapping).pack(side="left", padx=3)
        settings_frame = ttk.Frame(frame)
        settings_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))
        for name, label in (("play", "播放/继续"), ("pause", "暂停"), ("stop", "停止"), ("delay", "倒计时(秒)")):
            ttk.Label(settings_frame, text=label).pack(side="left", padx=(6, 3))
            entry = ttk.Entry(settings_frame, textvariable=self.setting_vars[name],
                              width=10 if name != "delay" else 6,
                              state="readonly" if name != "delay" else "normal")
            entry.pack(side="left")
            if name != "delay":
                self.hotkey_entries[name] = entry
                entry.bind("<FocusIn>", lambda event, action=name: self.begin_capture(action))
                entry.bind("<Button-1>", lambda event, widget=entry: widget.focus_set())
                entry.bind("<FocusOut>", lambda event: self.end_capture())
                entry.bind("<KeyPress>", lambda event: "break")
                entry.bind("<KeyRelease>", lambda event: "break")
        ttk.Button(settings_frame, text="保存设置", command=self.apply_settings).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(frame, maximum=1)
        self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(18, 8))
        ttk.Label(frame, textvariable=self.status).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, textvariable=self.error, foreground="#b42318", wraplength=610).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _: self.stop())
        self.root.after(100, self.tick)
        self.start_listener()
        self.status.set(f"就绪 · {self.hotkey_hint()}")

    def hotkey_hint(self):
        return " / ".join(f"{label} {self.settings[name].upper()}" for name, label in
                          (("play", "播放"), ("pause", "暂停"), ("stop", "停止")))

    def begin_capture(self, name):
        self.pause()
        self.capture_epoch += 1
        epoch = self.capture_epoch
        self.capture_name = name
        self.hotkeys.set_capture(lambda text, error: self.messages.put(
            ("capture", (epoch, name, text, error))))
        self.status.set("请按下按键或组合键，自动填入；点击其他位置结束录入，再保存设置")

    def end_capture(self):
        if self.capture_name is not None:
            self.capture_name = None
            self.capture_epoch += 1
            self.hotkeys.set_capture(None)
            self.status.set("热键录入结束，请点击保存设置生效")

    def apply_settings(self):
        self.root.focus_set()
        self.end_capture()
        self.stop()
        try:
            mapping = self.player.mapping if self.player else load_mapping(CONFIG)
            settings = validate_settings({k: v.get() for k, v in self.setting_vars.items()}, mapping)
            save_settings(SETTINGS, settings)
            self.settings = settings
            self.hotkeys.configure(settings)
            self.error.set("")
            self.status.set(f"设置已保存 · {self.hotkey_hint()}")
        except Exception as exc:
            self.error.set(f"设置未生效：{exc}")

    def hotkey_action(self, action):
        # Stop/pause act directly, without waiting for tkinter's next UI tick.
        if action in ("stop", "pause"):
            self.command_epoch += 1
            if self.player:
                getattr(self.player, action)()
        self.messages.put(("hotkey", (action, self.command_epoch)))

    def start_listener(self):
        try:
            from pynput.keyboard import Listener
            self.hotkeys = Hotkeys(self.settings, self.hotkey_action)
            self.listener = Listener(on_press=self.hotkeys.press, on_release=self.hotkeys.release)
            self.listener.start()
        except Exception as exc:
            self.error.set(f"全局热键无法启用，禁止播放：{exc}")

    def choose(self):
        if self.loading:
            return
        path = filedialog.askopenfilename(filetypes=[("MIDI", "*.mid *.midi"), ("所有文件", "*.*")])
        if not path:
            return
        self.stop()
        self.loading = True
        self.play_button.config(state="disabled")
        self.select_button.config(state="disabled")
        self.status.set("正在读取 MIDI…")
        self.error.set("")
        def read():
            try:
                mapping = load_mapping(CONFIG)
                self.messages.put(("loaded", (load_song(path, mapping), mapping)))
            except Exception as exc:
                self.messages.put(("load_error", str(exc)))
        threading.Thread(target=read, name="midi-load", daemon=True).start()

    def play(self):
        try:
            if not self.song or self.loading or self.capture_name is not None:
                return
            if not self.listener or not self.listener.is_alive():
                raise RuntimeError("全局紧急停止监听不可用，请重启程序")
            state = self.player.snapshot()[0]
            if state == "paused":
                self.player.resume(delay=self.settings["delay"])
            elif state not in ("playing", "countdown"):
                self.error.set("")
                self.player.play(self.song, delay=self.settings["delay"])
        except Exception as exc:
            self.error.set(str(exc))

    def pause(self):
        self.command_epoch += 1
        if self.player:
            self.player.pause()

    def stop(self):
        self.command_epoch += 1
        if self.player:
            self.player.stop()
        self.status.set(f"已停止 · {self.hotkey_hint()}")

    def change_speed(self, _=None):
        if self.player:
            self.player.set_speed(float(self.speed.get().rstrip("×")))

    def edit_mapping(self):
        self.stop()
        try:
            os.startfile(str(CONFIG))
            self.status.set("保存键位后，请重新选择 MIDI 文件以重新加载配置")
        except Exception as exc:
            self.error.set(f"打开配置失败：{exc}；文件：{CONFIG}")

    def tick(self):
        if self.closing:
            return
        while not self.messages.empty():
            kind, payload = self.messages.get()
            if kind in ("loaded", "load_error"):
                self.loading = False
                self.select_button.config(state="normal")
            if kind == "loaded":
                try:
                    if self.player:
                        self.player.close()
                        self.player = None
                    song, mapping = payload
                    validate_settings(self.settings, mapping)
                    self.player = Player(KeyboardOutput(mapping), mapping)
                    self.change_speed()
                    self.song = song
                    self.path.set(song.path)
                    self.info.set(f"时长 {clock_text(song.duration)}  ·  音符 {song.note_count}  ·  超范围/未映射 {song.skipped}")
                    self.progress.config(maximum=max(song.duration, 0.001))
                    self.status.set(f"就绪 · 倒计时 {self.settings['delay']:g} 秒 · {self.hotkey_hint()}")
                    if not song.events:
                        self.error.set("文件没有可演奏的音符，请检查音域或键位配置")
                except Exception as exc:
                    self.song = None
                    self.error.set(str(exc))
            elif kind == "load_error":
                self.error.set(payload)
                self.status.set("读取失败，请重新选择文件")
            elif kind == "hotkey":
                action, epoch = payload
                if epoch == self.command_epoch:
                    if action == "play":
                        self.play()
                    elif action == "stop":
                        self.status.set(f"已紧急停止 · {self.hotkey_hint()}")
            elif kind == "capture":
                epoch, name, text, error = payload
                if epoch == self.capture_epoch and name == self.capture_name:
                    if error:
                        self.error.set(error)
                    else:
                        self.setting_vars[name].set(text)
                        self.error.set("")
                        self.status.set(f"已录入 {text.upper()}，点击其他位置结束录入，再保存设置")
        if self.player:
            state, pos, countdown, error = self.player.snapshot()
            if self.listener and not self.listener.is_alive() and state in ("playing", "countdown", "paused"):
                self.player.stop()
                self.error.set("全局紧急停止监听已断开，播放已停止")
            self.progress["value"] = pos
            labels = {"playing": "播放中", "paused": "已暂停", "finished": "播放结束", "error": "播放异常"}
            if state == "countdown":
                self.status.set(f"{countdown:.1f} 秒后演奏，请切回游戏 · 停止 {self.settings['stop'].upper()}")
            elif state in labels:
                self.status.set(f"{labels[state]}  {clock_text(pos)} / {clock_text(self.song.duration)} · {self.hotkey_hint()}")
            if error:
                self.error.set(error)
            self.play_button.config(text="继续" if state == "paused" else "播放", state="normal" if not self.loading and self.song and self.song.events and state not in ("playing", "countdown") else "disabled")
            self.pause_button.config(state="normal" if state in ("playing", "countdown") else "disabled")
        self.root.after(100, self.tick)

    def close(self):
        self.closing = True
        if self.player:
            self.player.close()
        if self.listener:
            self.listener.stop()
        self.root.destroy()


def single_note(note, duration):
    """CLI test with countdown and F8; always tear down input and worker."""
    from pynput.keyboard import Key, Listener
    mapping = load_mapping(CONFIG)
    if note not in mapping:
        raise ValueError(f"音符 {note} 未映射")
    from midi_player import NoteEvent
    player = Player(KeyboardOutput(mapping), mapping)
    listener = Listener(on_press=lambda key: player.stop() if key == Key.f8 else None)
    try:
        listener.start()
        listener.wait()
        print(f"3 秒后发送 MIDI {note} → {mapping[note]!r}，持续 {duration} 秒；F8 停止", flush=True)
        player.play(Song("single-note", duration, 1, 0, (NoteEvent(0, note, True), NoteEvent(duration, note, False))))
        while player.snapshot()[0] in ("countdown", "playing"):
            if not listener.is_alive():
                raise RuntimeError("紧急停止监听已断开")
            time.sleep(0.03)
        if player.snapshot()[3]:
            raise RuntimeError(player.snapshot()[3])
    finally:
        player.close()
        listener.stop()
        listener.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description="异环吉他 MIDI 自动演奏器")
    parser.add_argument("--test-note", type=int, help="命令行单音测试，例如 61 测试分号")
    parser.add_argument("--duration", type=float, default=0.3)
    args = parser.parse_args()
    ensure_config()
    if args.test_note is not None:
        if not 0 < args.duration <= 10:
            parser.error("duration 必须大于 0 且不超过 10 秒")
        single_note(args.test_note, args.duration)
    else:
        root = tk.Tk()
        app = App(root)
        try:
            root.mainloop()
        finally:
            if app.player:
                app.player.close()
            if app.listener:
                app.listener.stop()


if __name__ == "__main__":
    main()
