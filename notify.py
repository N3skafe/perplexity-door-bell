"""알림 소리와 팝업.

Perplexity 창은 활성화하지 않는다. 팝업은 이 프로그램의 창이다.
"""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
import struct
import time
import wave
from pathlib import Path

import i18n
import paths

DEFAULT_SOUND = paths.install_dir() / "sounds" / "done.wav"
MAX_WAV_BYTES = 5_000_000


def resolve_sound_file(token: str, base: Path | None = None) -> Path:
    path = Path(token) if token else Path()
    if token and not path.is_absolute():
        path = (base or paths.install_dir()) / path
    return path


def validate_wav(path: Path) -> str | None:
    if path.suffix.lower() != ".wav":
        return "not_wav"
    if not path.is_file():
        return "missing"
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    if size < 44 or size > MAX_WAV_BYTES:
        return "bad_size"
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return "unreadable"
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return "bad_header"
    return None


def play_wav(path: Path) -> str | None:
    error = validate_wav(path)
    if error:
        return error
    import winsound

    try:
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except RuntimeError:
        return "play_failed"
    return None


def ensure_default_sound(path: Path | None = None) -> Path:
    target = path or DEFAULT_SOUND
    if validate_wav(target) is None:
        return target
    _write_chime(target)
    return target


def _write_chime(path: Path) -> None:
    framerate = 44100

    def tone(frequency: float, milliseconds: int) -> list[int]:
        count = int(framerate * milliseconds / 1000)
        fade = max(1, int(framerate * 0.012))
        samples = []
        for index in range(count):
            envelope = 1.0
            if index < fade:
                envelope = index / fade
            elif index > count - fade:
                envelope = max(0.0, (count - index) / fade)
            envelope *= 1.0 - 0.82 * (index / count)
            value = 0.32 * envelope * math.sin(2 * math.pi * frequency * index / framerate)
            samples.append(int(max(-1.0, min(1.0, value)) * 32767))
        return samples

    gap = [0] * int(framerate * 0.045)
    samples = tone(523.25, 150) + gap + tone(659.25, 170) + gap + tone(783.99, 320)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def _work_area() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        return 0, 0, width, height
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _place_bottom_right(win) -> None:
    hwnd = toplevel_hwnd(win)
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return
    left, top, right, bottom = _work_area()
    x = max(left, right - width - 16)
    y = max(top, bottom - height - 16)
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, 0, 0, 0x0001 | 0x0040)


def toplevel_hwnd(widget) -> int:
    user32 = ctypes.windll.user32
    raw = int(widget.winfo_id())
    ancestor = int(user32.GetAncestor(raw, 2) or 0)
    return ancestor or raw


def open_popup(root, auto_close_ms: int = 8000, language: str = "ko", font_family: str = "Segoe UI"):
    """이 프로그램의 알림 창을 띄운다. 다른 앱의 창 번호는 받지 않는다."""
    import tkinter as tk
    from tkinter import ttk

    existing = getattr(root, "_doorbell_popup", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.destroy()
        except tk.TclError:
            pass
    win = tk.Toplevel(root)
    root._doorbell_popup = win
    win.title(i18n.text(language, "popup_title"))
    win.resizable(False, False)
    win.attributes("-topmost", True)
    frame = ttk.Frame(win, padding=16)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text=i18n.text(language, "popup_message"), font=(font_family, 14, "bold")).grid(sticky="w")
    ttk.Label(frame, text=i18n.text(language, "popup_detail"), font=(font_family, 10), wraplength=300).grid(
        sticky="w", pady=(8, 14)
    )

    def close() -> None:
        if getattr(root, "_doorbell_popup", None) is win:
            root._doorbell_popup = None
        try:
            win.destroy()
        except tk.TclError:
            pass

    button = ttk.Button(frame, text=i18n.text(language, "popup_close"), command=close)
    button.grid(sticky="e")
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", lambda _event: close())
    win.bind("<Return>", lambda _event: close())
    win.update_idletasks()
    width = max(win.winfo_width(), 320)
    height = max(win.winfo_height(), 140)
    win.geometry(f"{width}x{height}")
    win.update_idletasks()
    _place_bottom_right(win)
    button.focus_set()
    if auto_close_ms > 0:
        win.after(auto_close_ms, close)
    return win


def popup_facts(widget) -> dict:
    user32 = ctypes.windll.user32
    hwnd = toplevel_hwnd(widget)
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    left, top, right, bottom = _work_area()
    return {
        "hwnd": hwnd,
        "title": title.value,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "topmost": bool(int(user32.GetWindowLongW(hwnd, -20)) & 0x00000008),
        "inside_work_area": (
            rect.left >= left - 8
            and rect.top >= top - 8
            and rect.right <= right + 8
            and rect.bottom <= bottom + 8
        ),
    }


class PopupService:
    """감시 루프와 다른 스레드에서 팝업을 띄운다."""

    def __init__(self):
        import queue

        self.queue = queue.Queue()
        self.thread = None

    def show(self, auto_close_ms: int = 8000, language: str = "ko") -> None:
        self._ensure()
        self.queue.put((int(auto_close_ms), i18n.normalize(language)))

    def _ensure(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        import threading

        self.thread = threading.Thread(target=self._run, name="doorbell-popup", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import queue
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()

        def pump() -> None:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                root.after(50, pump)
                return
            if item is None:
                root.quit()
                return
            auto_close_ms, language = item
            open_popup(root, auto_close_ms=int(auto_close_ms), language=language)
            root.after(50, pump)

        root.after(50, pump)
        root.mainloop()
        try:
            root.destroy()
        except tk.TclError:
            pass


def exercise_popup(auto_close_ms: int = 1200) -> dict:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    win = open_popup(root, auto_close_ms=auto_close_ms)
    root.update()
    facts = popup_facts(win)
    deadline = time.monotonic() + (auto_close_ms / 1000) + 2
    while time.monotonic() < deadline:
        root.update()
        try:
            if not win.winfo_exists():
                break
        except tk.TclError:
            break
        time.sleep(0.05)
    try:
        facts["closed"] = not win.winfo_exists()
    except tk.TclError:
        facts["closed"] = True
    root.destroy()
    return facts
