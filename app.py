"""소리와 팝업을 고르고, Perplexity 완료 알림을 감시한다."""

from __future__ import annotations

import argparse
import ctypes
import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import i18n
import inspect_ui
import monitor
import notify
import paths

_MUTEX = None


def already_running() -> bool:
    global _MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\PerplexityDoorBell.v1")
    if not handle:
        return False
    if kernel32.GetLastError() == 183:
        return True
    _MUTEX = handle
    return False


def _font_family(code: str) -> str:
    have = set(tkfont.families())
    for name in i18n.FONT_CANDIDATES.get(i18n.normalize(code), ("Segoe UI",)):
        if name in have:
            return name
    return "Segoe UI"


def _list_font_family() -> str:
    have = set(tkfont.families())
    for name in i18n.LIST_FONT_CANDIDATES:
        if name in have:
            return name
    return "Segoe UI"


def _status_text(obs: dict, result: dict, language: str) -> str:
    status = obs.get("status")
    if status == "window_missing":
        return i18n.text(language, "window_missing")
    if status == "ambiguous_window":
        return i18n.text(language, "ambiguous")
    if status != "ok":
        return i18n.text(language, "read_failed")
    phase = {
        "IDLE": i18n.text(language, "phase_idle"),
        "GENERATING": i18n.text(language, "phase_generating"),
        "UNKNOWN": i18n.text(language, "phase_unknown"),
    }.get(result.get("phase") or "", result.get("phase") or "")
    window = obs.get("window") or {}
    hwnd = window.get("hwnd_hex") or "-"
    if window.get("is_iconic"):
        return i18n.text(language, "minimized", hwnd=hwnd)
    place = i18n.text(language, "place_front" if window.get("is_foreground") else "place_covered")
    text = f"{phase} · {place} · {hwnd}"
    if "interrupted" in (result.get("events") or []):
        text += " · " + i18n.text(language, "interrupted")
    return text


class App:
    def __init__(self, auto_start: bool = True, config_path: Path | None = None):
        paths.ensure_install_files()
        notify.ensure_default_sound(notify.DEFAULT_SOUND)
        self.path = config_path or monitor.CONFIG_PATH
        self.config = monitor.load_config(self.path)
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.tracker: monitor.Tracker | None = None
        self.root = tk.Tk()
        self.root.geometry("520x470")
        self.root.minsize(480, 430)
        self.status_var = tk.StringVar()
        self.hint_var = tk.StringVar()
        self.sound_var = tk.StringVar(value=str(self.config.get("sound_file") or ""))
        self.popup_var = tk.BooleanVar(value=bool(self.config.get("popup_enabled")))
        self.language_var = tk.StringVar(value=i18n.label_for(self.config.get("language")))
        self._build()
        self._apply_language()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain)
        if auto_start:
            self.start()

    def _lang(self) -> str:
        return i18n.normalize(self.config.get("language"))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        self.title_label = ttk.Label(outer)
        self.title_label.pack(anchor="w")
        self.intro_label = ttk.Label(outer, wraplength=480)
        self.intro_label.pack(anchor="w", pady=(6, 12))
        self.language_heading = ttk.Label(outer)
        self.language_heading.pack(anchor="w")
        self.language_box = ttk.Combobox(
            outer,
            textvariable=self.language_var,
            values=[label for _code, label in i18n.LANGUAGES],
            state="readonly",
            width=18,
        )
        self.language_box.pack(anchor="w", pady=(2, 10))
        self.language_box.bind("<<ComboboxSelected>>", self._on_language)
        self._style_language_list()
        self.status_heading = ttk.Label(outer)
        self.status_heading.pack(anchor="w")
        self.status_label = ttk.Label(outer, textvariable=self.status_var, wraplength=480)
        self.status_label.pack(anchor="w", pady=(2, 10))
        buttons = ttk.Frame(outer)
        buttons.pack(anchor="w", pady=(0, 14))
        self.start_button = ttk.Button(buttons, command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, command=self.stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.sound_heading = ttk.Label(outer)
        self.sound_heading.pack(anchor="w")
        ttk.Label(outer, textvariable=self.sound_var, wraplength=480).pack(anchor="w", pady=(2, 6))
        sound_buttons = ttk.Frame(outer)
        sound_buttons.pack(anchor="w")
        self.browse_button = ttk.Button(sound_buttons, command=self.browse)
        self.browse_button.pack(side="left")
        self.preview_button = ttk.Button(sound_buttons, command=self.preview)
        self.preview_button.pack(side="left", padx=(8, 0))
        self.popup_check = ttk.Checkbutton(outer, variable=self.popup_var, command=self._save_popup)
        self.popup_check.pack(anchor="w", pady=(12, 8))
        self.hint_label = ttk.Label(outer, textvariable=self.hint_var, wraplength=480)
        self.hint_label.pack(anchor="w")
        self._sync_buttons()

    def _apply_language(self) -> None:
        lang = self._lang()
        family = _font_family(lang)
        body = (family, 10)
        title = (family, 16, "bold")
        heading = (family, 10, "bold")
        self.root.title(i18n.text(lang, "app_title"))
        self._style_language_list()
        self.title_label.configure(text=i18n.text(lang, "app_title"), font=title)
        self.intro_label.configure(text=i18n.text(lang, "intro"), font=body)
        self.language_heading.configure(text=i18n.text(lang, "language_heading"), font=heading)
        self.status_heading.configure(text=i18n.text(lang, "status_heading"), font=heading)
        self.status_label.configure(font=body)
        self.sound_heading.configure(text=i18n.text(lang, "sound_heading"), font=heading)
        self.start_button.configure(text=i18n.text(lang, "start"))
        self.stop_button.configure(text=i18n.text(lang, "stop"))
        self.browse_button.configure(text=i18n.text(lang, "browse"))
        self.preview_button.configure(text=i18n.text(lang, "preview"))
        self.popup_check.configure(text=i18n.text(lang, "popup_check"))
        self.hint_label.configure(font=body)
        if not self.status_var.get():
            self.status_var.set(i18n.text(lang, "preparing"))
        if not self.hint_var.get():
            self.hint_var.set(i18n.text(lang, "hint_saved"))

    def _style_language_list(self) -> None:
        family = _list_font_family()
        style = ttk.Style(self.root)
        style.configure("Lang.TCombobox", font=(family, 11))
        self.language_box.configure(style="Lang.TCombobox")
        popdown = self.language_box.tk.call("ttk::combobox::PopdownWindow", self.language_box)
        listbox = f"{popdown}.f.l"
        self.language_box.tk.call(
            listbox,
            "configure",
            "-font",
            (family, 11),
            "-foreground",
            "black",
            "-background",
            "white",
            "-selectforeground",
            "black",
            "-selectbackground",
            "#d6e8f8",
        )

    def _on_language(self, _event=None) -> None:
        self._write_settings(
            str(self.config.get("sound_file") or ""),
            bool(self.popup_var.get()),
            i18n.code_from_label(self.language_var.get()),
        )
        self._apply_language()
        self.hint_var.set(i18n.text(self._lang(), "language_saved"))

    def _sync_buttons(self) -> None:
        running = self.thread is not None and self.thread.is_alive()
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.tracker = monitor.Tracker(self.config, self._enqueue_alert)
        self.thread = threading.Thread(target=self._loop, name="doorbell-watch", daemon=True)
        self.thread.start()
        self.status_var.set(i18n.text(self._lang(), "watching"))
        self._sync_buttons()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=3)
        self.status_var.set(i18n.text(self._lang(), "stopped"))
        self._sync_buttons()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def browse(self) -> None:
        lang = self._lang()
        selected = filedialog.askopenfilename(
            parent=self.root,
            title=i18n.text(lang, "file_dialog"),
            filetypes=[(i18n.text(lang, "wav_filter"), "*.wav")],
        )
        if not selected:
            return
        path = Path(selected)
        error = notify.validate_wav(path)
        if error:
            self.hint_var.set(i18n.text(lang, error))
            return
        stored = _stored_sound(path)
        self._write_settings(stored, bool(self.popup_var.get()), self._lang())
        self.sound_var.set(stored)
        self.hint_var.set(i18n.text(self._lang(), "sound_saved"))

    def preview(self) -> None:
        lang = self._lang()
        error = notify.play_wav(notify.resolve_sound_file(str(self.config.get("sound_file") or "")))
        self.hint_var.set(i18n.text(lang, error) if error else i18n.text(lang, "preview_ok"))

    def _save_popup(self) -> None:
        self._write_settings(str(self.config.get("sound_file") or ""), bool(self.popup_var.get()), self._lang())
        key = "popup_on" if self.popup_var.get() else "popup_off"
        self.hint_var.set(i18n.text(self._lang(), key))

    def _write_settings(self, sound_file: str, popup_enabled: bool, language: str | None = None) -> None:
        chosen = self._lang() if language is None else i18n.normalize(language)
        with monitor.ALERT_LOCK:
            monitor.save_alert_settings(self.path, sound_file, popup_enabled, chosen)
            self.config["sound_file"] = sound_file
            self.config["popup_enabled"] = popup_enabled
            self.config["language"] = chosen

    def _enqueue_alert(self, token: str) -> None:
        with monitor.ALERT_LOCK:
            popup = bool(self.config.get("popup_enabled"))
            current = str(self.config.get("sound_file") or token)
            language = str(self.config.get("language") or "ko")
        self.queue.put(("alert", current, popup, language))

    def _loop(self) -> None:
        if not inspect_ui.init_uia():
            self.queue.put(("status", i18n.text(self._lang(), "uia_failed")))
            return
        cached = None
        seen = None
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                with monitor.ALERT_LOCK:
                    monitor.refresh_alert_settings(self.path, self.config)
                    language = str(self.config.get("language") or "ko")
                obs, cached = monitor.follow_window(self.config, None, cached, monitor.select_window)
            except Exception:
                language = str(self.config.get("language") or "ko")
                obs = {"status": "read_failed", "buttons": [], "window": {}}
            tracker = self.tracker
            if tracker is None:
                return
            result = tracker.update(obs, started)
            hwnd = (obs.get("window") or {}).get("hwnd")
            text = _status_text(obs, result, language)
            if obs.get("status") == "ok" and not (obs.get("window") or {}).get("is_iconic"):
                if seen not in (None, hwnd) and hwnd:
                    text += " · " + i18n.text(language, "rebound")
            if obs.get("status") == "ok" and not (obs.get("window") or {}).get("is_iconic"):
                seen = hwnd
            elif obs.get("status") in {"window_missing", "ambiguous_window"}:
                seen = None
            self.queue.put(("status", text))
            remaining = float(self.config.get("poll_interval_sec") or 0.5) - (time.monotonic() - started)
            deadline = time.monotonic() + max(0.0, remaining)
            while time.monotonic() < deadline:
                if self.stop_event.is_set():
                    return
                time.sleep(0.05)

    def _drain(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_var.set(item[1])
                elif kind == "alert":
                    _token, sound_file, popup, language = item
                    error = notify.play_wav(notify.resolve_sound_file(sound_file))
                    if popup:
                        try:
                            notify.open_popup(self.root, language=language, font_family=_font_family(language))
                        except tk.TclError:
                            error = error or "popup_failed"
                    if error:
                        self.hint_var.set(i18n.text(language, error))
                    else:
                        self.hint_var.set(i18n.text(language, "alert_sent"))
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._drain)
        except tk.TclError:
            pass


def _stored_sound(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(paths.install_dir().resolve())
    except ValueError:
        return str(path.resolve())
    return str(relative)


def run_check() -> int:
    app = App(auto_start=True)
    found: dict = {}

    def grab() -> None:
        texts: list[str] = []

        def walk(widget) -> None:
            try:
                text = str(widget.cget("text"))
            except tk.TclError:
                text = ""
            if text:
                texts.append(text)
            for child in widget.winfo_children():
                walk(child)

        walk(app.root)
        found["widgets"] = texts
        found["language"] = app.language_var.get()
        print("WIDGETS " + " | ".join(texts))
        print("LANGUAGE " + found["language"])

    def finish() -> None:
        found["status"] = app.status_var.get()
        print("STATUS " + found["status"])
        app.stop_event.set()
        if app.thread is not None:
            app.thread.join(timeout=5)
        app.close()

    app.root.after(400, grab)
    app.root.after(30000, finish)
    app.root.mainloop()
    widgets = " ".join(found.get("widgets") or [])
    status = found.get("status") or ""
    recognized = (
        "찾지 못했다" in status
        or "대기" in status
        or "생성 중" in status
        or "가려" in status
        or "최소화" in status
        or "읽지 못했다" in status
        or "둘 이상" in status
    )
    if "들어보기" not in widgets or "끝났을 때 팝업" not in widgets or found.get("language") != "한국어" or not recognized:
        print("CHECK FAIL")
        return 1
    print("CHECK OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:
        log = paths.install_dir() / "doorbell-error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def _main(argv: list[str] | None = None) -> int:
    inspect_ui.configure_stdio()
    parser = argparse.ArgumentParser(description="Perplexity 완료 알림")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return run_check()
    if already_running():
        root = tk.Tk()
        root.withdraw()
        language = "ko"
        try:
            paths.ensure_install_files()
            language = monitor.load_config(monitor.CONFIG_PATH).get("language") or "ko"
        except (OSError, ValueError):
            language = "ko"
        messagebox.showinfo(i18n.text(language, "app_title"), i18n.text(language, "already_running"))
        root.destroy()
        return 0
    app = App(auto_start=True)
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
