"""개발 폴더와 exe 옆 폴더를 구분한다."""

from __future__ import annotations

import sys
from pathlib import Path


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", install_dir()))
    return Path(__file__).resolve().parent


def ensure_install_files() -> None:
    root = install_dir()
    bundled = bundle_dir()
    config = root / "config.json"
    bundled_config = bundled / "config.json"
    if not config.exists() and bundled_config.exists() and bundled_config.resolve() != config.resolve():
        config.write_bytes(bundled_config.read_bytes())
    sound = root / "sounds" / "done.wav"
    bundled_sound = bundled / "sounds" / "done.wav"
    if not sound.exists() and bundled_sound.exists() and bundled_sound.resolve() != sound.resolve():
        sound.parent.mkdir(parents=True, exist_ok=True)
        sound.write_bytes(bundled_sound.read_bytes())
