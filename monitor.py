"""Perplexity 창에서 생성 종료를 감시한다.

클릭, 키 입력, 창 활성화, 값 변경, 네트워크 요청은 하지 않는다.
답변 본문과 입력란 내용은 읽지 않는다.
앱을 끄면 알리지 않고, 다시 켜지면 새 창을 찾는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import i18n
import inspect_ui
import notify
import paths

ALERT_LOCK = threading.RLock()

CONFIG_PATH = paths.install_dir() / "config.json"
OBSERVATION_LIMIT = 50


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    window = data.get("window") or {}
    anchor = data.get("anchor") or {}
    idle = data.get("idle_submit") or {}
    required = [
        window.get("process_exe"),
        window.get("class_name"),
        window.get("title"),
        anchor.get("control_type"),
        anchor.get("automation_id"),
        idle.get("control_type"),
        idle.get("name"),
    ]
    if any(not value for value in required):
        raise ValueError("설정에 검사로 확인된 창 또는 버튼 속성이 없다.")
    if "enabled" not in idle or "legacy_unavailable" not in idle:
        raise ValueError("제출 버튼의 활성 상태가 설정에 없다.")
    data.setdefault("generating_names", [])
    data.setdefault("observed_composer_button_names", [])
    data.setdefault("completion_distinct_from_cancel", False)
    data.setdefault("sound_on_stop_disappear", False)
    data.setdefault("poll_interval_sec", 0.5)
    data.setdefault("confirm_sec", 1.0)
    data.setdefault("sound_file", "sounds/done.wav")
    data.setdefault("popup_enabled", True)
    data["language"] = i18n.normalize(data.get("language"))
    data.setdefault("composer_name_limit", 24)
    data.setdefault("max_parent_hops", 8)
    if data["poll_interval_sec"] < 0.2:
        raise ValueError("폴링 간격은 0.2초 이상이어야 한다.")
    if data["confirm_sec"] < 0.2:
        raise ValueError("재확인 시간은 0.2초 이상이어야 한다.")
    return data


def save_alert_settings(
    path: Path,
    sound_file: str,
    popup_enabled: bool,
    language: str | None = None,
) -> None:
    with ALERT_LOCK:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sound_file"] = sound_file
        data["popup_enabled"] = bool(popup_enabled)
        if language is not None:
            data["language"] = i18n.normalize(language)
        data.pop("sound_alias", None)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)


def refresh_alert_settings(path: Path, config: dict) -> None:
    with ALERT_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
            return
        if isinstance(data.get("sound_file"), str):
            config["sound_file"] = data["sound_file"]
        if "popup_enabled" in data:
            config["popup_enabled"] = bool(data["popup_enabled"])
        if isinstance(data.get("language"), str):
            config["language"] = i18n.normalize(data["language"])


def will_play_sound(config: dict) -> bool:
    if not config.get("generating_names"):
        return False
    return bool(config.get("completion_distinct_from_cancel")) or bool(
        config.get("sound_on_stop_disappear")
    )


def disarm_reason(config: dict) -> str:
    if not config.get("generating_names"):
        return "생성 중 버튼이나 상태를 아직 관측하지 않았다."
    if not config.get("completion_distinct_from_cancel") and not config.get("sound_on_stop_disappear"):
        return "정상 완료와 취소를 구분하는 속성을 아직 관측하지 않았다."
    return ""


def _matches_idle(obs: dict, config: dict) -> bool:
    buttons = obs.get("buttons") or []
    expected = config.get("observed_composer_button_names") or []
    if any(button.get("name_omitted") or button.get("name_unreadable") for button in buttons):
        return False
    names = sorted(button.get("name") or "" for button in buttons)
    if names != sorted(expected):
        return False
    idle = config["idle_submit"]
    return any(
        button.get("name") == idle["name"]
        and button.get("enabled") is idle["enabled"]
        and button.get("unavailable") is idle["legacy_unavailable"]
        for button in buttons
    )


def _is_generating(obs: dict, config: dict) -> bool:
    names = set(config.get("generating_names") or [])
    if not names:
        return False
    return any(
        not button.get("name_omitted")
        and not button.get("name_unreadable")
        and button.get("offscreen") is False
        and button.get("name") in names
        for button in obs.get("buttons") or []
    )


def _settled(obs: dict, config: dict, generating: bool) -> bool:
    if generating:
        return False
    if config.get("sound_on_stop_disappear"):
        return True
    return _matches_idle(obs, config)


class Tracker:
    """IDLE, GENERATING, COMPLETED, UNKNOWN 전이.

    생성 중을 관측하기 전에는 완료로 넘어가지 않는다.
    조회 실패, 창 닫힘, 최소화는 완료가 아니다.
    그 구간 뒤에 창이 다시 보여도 이번 생성은 알리지 않는다.
    """

    def __init__(self, config: dict, player):
        self.config = config
        self.player = player
        self.phase = "UNKNOWN"
        self.started = False
        self.generating_seen = False
        self.candidate_since: float | None = None
        self.sounded = False
        self.minimized_hold = False
        self.last_status: str | None = None

    def update(self, obs: dict, now: float) -> dict:
        previous = self.phase
        had_started = self.started
        self.started = True
        events: list[str] = []
        sound = False

        minimized = bool((obs.get("window") or {}).get("is_iconic"))
        if obs.get("status") != "ok" or minimized:
            if self.generating_seen or previous == "GENERATING":
                events.append("interrupted")
            if minimized and obs.get("status") == "ok":
                if not self.minimized_hold:
                    events.append("minimized")
                self.minimized_hold = True
                self.phase = "UNKNOWN" if self.generating_seen or previous == "GENERATING" else "IDLE"
            else:
                self.minimized_hold = False
                if previous != "UNKNOWN" or self.last_status != obs.get("status"):
                    events.append("unknown")
                self.phase = "UNKNOWN"
            self.generating_seen = False
            self.candidate_since = None
            self.sounded = False
            self.last_status = obs.get("status")
            return self._result(events, sound)
        self.minimized_hold = False
        self.last_status = "ok"

        generating = _is_generating(obs, self.config)
        idle = _settled(obs, self.config, generating)

        if not self.config.get("generating_names"):
            if idle:
                if had_started and previous == "UNKNOWN":
                    events.append("resumed_after_unknown")
                elif not had_started:
                    events.append("baseline")
                self.phase = "IDLE"
            else:
                if previous != "UNKNOWN":
                    events.append("unconfirmed_change")
                self.phase = "UNKNOWN"
            self.candidate_since = None
            return self._result(events, sound)

        if generating:
            if previous != "GENERATING":
                self.sounded = False
                events.append("generating")
            self.phase = "GENERATING"
            self.generating_seen = True
            self.candidate_since = None
            return self._result(events, sound)

        if previous == "GENERATING" and idle and self.generating_seen:
            if self.candidate_since is None:
                self.candidate_since = now
                events.append("candidate")
                return self._result(events, sound)
            if now - self.candidate_since < float(self.config["confirm_sec"]):
                events.append("confirming")
                return self._result(events, sound)
            self.candidate_since = None
            self.generating_seen = False
            self.phase = "IDLE"
            allow_sound = bool(self.config.get("sound_on_stop_disappear")) or bool(
                self.config.get("completion_distinct_from_cancel")
            )
            if not allow_sound:
                events.append("cancel_not_distinct")
                return self._result(events, sound)
            if not self.sounded:
                self.player(str(self.config.get("sound_file") or ""))
                self.sounded = True
                sound = True
                events.append(
                    "generation_ended" if self.config.get("sound_on_stop_disappear") else "completed"
                )
            return self._result(events, sound)

        if idle:
            if had_started and previous == "UNKNOWN":
                events.append("resumed_after_unknown")
            self.phase = "IDLE"
            self.candidate_since = None
            return self._result(events, sound)

        self.candidate_since = None
        if previous == "GENERATING":
            self.phase = "GENERATING"
            events.append("not_idle")
            return self._result(events, sound)
        if previous != "UNKNOWN":
            events.append("unconfirmed_change")
        self.phase = "UNKNOWN"
        return self._result(events, sound)

    def _result(self, events: list[str], sound: bool) -> dict:
        return {
            "phase": self.phase,
            "events": events,
            "sound": sound,
            "popup": bool(sound and self.config.get("popup_enabled")),
        }


def play_sound(token: str) -> None:
    error = notify.play_wav(notify.resolve_sound_file(token))
    if error:
        print(i18n.text("ko", error))


def _window_matches(row: dict, config: dict) -> bool:
    window = config["window"]
    if row.get("title_omitted"):
        return False
    return (
        row.get("process_exe") == window["process_exe"]
        and row.get("class_name") == window["class_name"]
        and row.get("title") == window["title"]
    )


def _flags(hwnd: int) -> dict:
    inspect_ui.init_win32()
    user32 = inspect_ui.user32
    alive = bool(user32.IsWindow(hwnd))
    foreground = int(user32.GetForegroundWindow() or 0)
    return {
        "hwnd": hwnd,
        "hwnd_hex": f"0x{hwnd:08X}",
        "alive": alive,
        "is_foreground": alive and hwnd == foreground,
        "is_iconic": bool(user32.IsIconic(hwnd)) if alive else False,
        "is_win32_visible": bool(user32.IsWindowVisible(hwnd)) if alive else False,
    }


def _visual_state(element) -> str:
    from pywinauto.uia_defines import IUIA

    try:
        iuia = IUIA()
        unknown = element.GetCurrentPattern(iuia.UIA_dll.UIA_WindowPatternId)
        if unknown is None:
            return ""
        pattern = unknown.QueryInterface(iuia.ui_automation_client.IUIAutomationWindowPattern)
        visual = int(pattern.CurrentWindowVisualState)
    except Exception:
        return ""
    return {0: "normal", 1: "maximized", 2: "minimized"}.get(visual, str(visual))


def _control_type(element) -> str | None:
    from pywinauto.uia_defines import IUIA

    try:
        type_id = int(element.CurrentControlType)
    except Exception:
        return None
    return IUIA().known_control_type_ids.get(type_id) or f"#{type_id}"


def _children(element):
    from pywinauto.uia_defines import IUIA

    iuia = IUIA()
    try:
        array = element.FindAll(iuia.tree_scope["children"], iuia.true_condition)
    except Exception:
        return None
    if array is None:
        return None
    try:
        length = int(array.Length)
    except Exception:
        return None
    children = []
    for index in range(length):
        try:
            children.append(array.GetElement(index))
        except Exception:
            return None
    return children


def _read_button(element, name_limit: int) -> dict | None:
    from pywinauto.uia_defines import IUIA

    try:
        raw_name = element.CurrentName
    except Exception:
        return None
    if raw_name is None:
        raw_name = ""
    raw_name = str(raw_name)
    omitted = "\n" in raw_name or "\r" in raw_name or len(raw_name) > name_limit
    name = "" if omitted else " ".join(raw_name.split())
    try:
        enabled_raw = element.CurrentIsEnabled
    except Exception:
        return None
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    elif isinstance(enabled_raw, int) and enabled_raw in (0, 1):
        enabled = bool(enabled_raw)
    else:
        return None
    try:
        offscreen_raw = element.CurrentIsOffscreen
        if isinstance(offscreen_raw, bool):
            offscreen = offscreen_raw
        elif isinstance(offscreen_raw, int) and offscreen_raw in (0, 1):
            offscreen = bool(offscreen_raw)
        else:
            offscreen = None
    except Exception:
        offscreen = None
    unavailable = None
    try:
        iuia = IUIA()
        pattern_id = iuia.UIA_dll.UIA_LegacyIAccessiblePatternId
        unknown = element.GetCurrentPattern(pattern_id)
        if unknown is not None:
            legacy = unknown.QueryInterface(iuia.ui_automation_client.IUIAutomationLegacyIAccessiblePattern)
            unavailable = bool(int(legacy.CurrentState) & 0x1)
    except Exception:
        unavailable = None
    return {
        "name": name,
        "name_omitted": omitted,
        "name_unreadable": False,
        "enabled": enabled,
        "unavailable": unavailable,
        "offscreen": offscreen,
    }


def _read_stop_buttons(root, base: dict, names: list[str]) -> dict:
    from pywinauto.uia_defines import IUIA

    iuia = IUIA()
    scope = iuia.tree_scope["descendants"]
    buttons = []
    try:
        for name in names:
            condition = iuia.iuia.CreatePropertyCondition(iuia.UIA_dll.UIA_NamePropertyId, name)
            found = root.FindAll(scope, condition)
            count = 0 if found is None else int(found.Length)
            for index in range(count):
                element = found.GetElement(index)
                if _control_type(element) != "Button":
                    continue
                button = _read_button(element, max(len(name), 24))
                if button is None or button.get("offscreen") is None:
                    base["status"] = "read_failed"
                    return base
                if button.get("name") != name or button.get("offscreen") is not False:
                    continue
                buttons.append(button)
    except Exception:
        base["status"] = "read_failed"
        return base
    base["status"] = "ok"
    base["buttons"] = buttons
    return base


def read_composer(hwnd: int, config: dict) -> dict:
    flags = _flags(hwnd)
    base = {"status": "read_failed", "buttons": [], "window": flags}
    if not flags["alive"]:
        base["status"] = "window_missing"
        return base
    if not inspect_ui.init_uia():
        base["status"] = "uia_failed"
        return base
    try:
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        root = UIAElementInfo(hwnd).element
        iuia = IUIA()
        condition = iuia.iuia.CreatePropertyCondition(
            iuia.UIA_dll.UIA_AutomationIdPropertyId,
            config["anchor"]["automation_id"],
        )
        found = root.FindAll(iuia.tree_scope["descendants"], condition)
        count = 0 if found is None else int(found.Length)
        flags["window_visual_state"] = _visual_state(root)
    except Exception:
        base["status"] = "read_failed"
        return base
    if count == 0:
        base["status"] = "anchor_missing"
        return base
    if count != 1:
        base["status"] = "anchor_ambiguous"
        return base
    try:
        anchor = found.GetElement(0)
    except Exception:
        base["status"] = "read_failed"
        return base
    if _control_type(anchor) != config["anchor"]["control_type"]:
        base["status"] = "anchor_missing"
        return base

    generating_names = [str(name) for name in (config.get("generating_names") or []) if str(name)]
    if generating_names:
        return _read_stop_buttons(root, base, generating_names)

    try:
        walker = IUIA().iuia.ControlViewWalker
    except Exception:
        base["status"] = "read_failed"
        return base
    current = anchor
    composer_children = None
    for _hop in range(int(config["max_parent_hops"])):
        try:
            parent = walker.GetParentElement(current)
        except Exception:
            base["status"] = "read_failed"
            return base
        if parent is None:
            break
        children = _children(parent)
        if children is None:
            base["status"] = "read_failed"
            return base
        button_found = False
        for child in children:
            if _control_type(child) == "Button":
                button_found = True
                break
        if button_found:
            composer_children = children
            break
        current = parent
    if composer_children is None:
        base["status"] = "composer_missing"
        return base

    buttons = []
    limit = int(config["composer_name_limit"])
    for child in composer_children:
        control_type = _control_type(child)
        if control_type is None:
            base["status"] = "read_failed"
            return base
        targets = []
        if control_type == "Button":
            targets = [child]
        else:
            grandchildren = _children(child)
            if grandchildren is None:
                base["status"] = "read_failed"
                return base
            for grandchild in grandchildren:
                if _control_type(grandchild) == "Button":
                    targets.append(grandchild)
        for target in targets:
            button = _read_button(target, limit)
            if button is None:
                base["status"] = "read_failed"
                return base
            buttons.append(button)
    base["status"] = "ok"
    base["buttons"] = buttons
    return base


def _process_matches(hwnd: int, config: dict) -> bool:
    inspect_ui.init_win32()
    if not inspect_ui.user32.IsWindow(hwnd):
        return False
    pid = inspect_ui._process_id(hwnd)
    exe = inspect_ui._process_exe(pid, {})
    return exe == config["window"]["process_exe"]


def select_window(config: dict, hwnd: int | None = None) -> dict:
    if hwnd is not None:
        if not _process_matches(hwnd, config):
            return {
                "status": "window_missing",
                "buttons": [],
                "window": _flags(hwnd),
                "match_count": 0,
            }
        obs = read_composer(hwnd, config)
        obs["match_count"] = 1 if obs["status"] == "ok" else 0
        return obs
    rows = [row for row in inspect_ui.list_windows() if _window_matches(row, config)]
    if not rows:
        return {"status": "window_missing", "buttons": [], "window": {}, "match_count": 0}
    matches = []
    failures = []
    for row in rows:
        obs = read_composer(row["hwnd"], config)
        obs["listed"] = {
            "process_exe": row.get("process_exe"),
            "class_name": row.get("class_name"),
            "title": row.get("title"),
        }
        if obs["status"] == "ok":
            matches.append(obs)
        else:
            failures.append(obs)
    if len(matches) == 1:
        matches[0]["match_count"] = 1
        return matches[0]
    if len(matches) > 1:
        return {
            "status": "ambiguous_window",
            "buttons": [],
            "window": {},
            "match_count": len(matches),
        }
    status = failures[0]["status"] if failures else "window_missing"
    return {
        "status": status,
        "buttons": [],
        "window": failures[0].get("window") if failures else {},
        "match_count": 0,
    }


def format_button(button: dict) -> str:
    if button.get("name_unreadable"):
        name = "name=(읽기 실패)"
    elif button.get("name_omitted"):
        name = "name=(생략)"
    elif button.get("name"):
        name = f"name={button['name']}"
    else:
        name = "name=(비어 있음)"
    enabled = button.get("enabled")
    enabled_text = "enabled" if enabled is True else "disabled" if enabled is False else "enabled=?"
    unavailable = button.get("unavailable")
    unavailable_text = (
        "unavailable" if unavailable is True else "available" if unavailable is False else "unavailable=?"
    )
    offscreen = button.get("offscreen")
    offscreen_text = "offscreen" if offscreen is True else "onscreen" if offscreen is False else "offscreen=?"
    return f"{name} {enabled_text} {unavailable_text} {offscreen_text}"


def format_observation(obs: dict, result: dict, config: dict) -> str:
    window = obs.get("window") or {}
    lines = [
        f"phase={result['phase']} status={obs.get('status')} sound={'yes' if result['sound'] else 'no'}",
        "완료 알림을 보내지 않았다."
        if not result["sound"]
        else "생성이 끝났다. 취소나 오류로 멈춘 경우에도 소리를 한 번 재생했다."
        + (" 팝업을 띄웠다." if result.get("popup") else ""),
        (
            f"창 hwnd={window.get('hwnd_hex', '-')} "
            f"fg={inspect_ui._yn(window.get('is_foreground'))} "
            f"min={inspect_ui._yn(window.get('is_iconic'))} "
            f"vis={inspect_ui._yn(window.get('is_win32_visible'))} "
            f"visual={window.get('window_visual_state') or '?'}"
        ),
    ]
    if result["events"]:
        lines.append("events=" + ",".join(result["events"]))
    if not will_play_sound(config):
        lines.append("알림 꺼짐: " + disarm_reason(config))
    if obs.get("status") != "ok":
        lines.append("UNKNOWN. 창을 닫았거나 UI를 읽지 못했다. 완료로 처리하지 않는다.")
    for button in obs.get("buttons") or []:
        lines.append("  " + format_button(button))
    return "\n".join(lines)


def _signature(obs: dict) -> tuple:
    if obs.get("status") != "ok":
        return (obs.get("status"),)
    rows = []
    for button in obs.get("buttons") or []:
        rows.append(
            (
                button.get("name") or "",
                bool(button.get("name_omitted")),
                button.get("enabled"),
                button.get("unavailable"),
                button.get("offscreen"),
            )
        )
    return tuple(sorted(rows))


def _append_observation(path: Path, obs: dict, result: dict, saved: int) -> int:
    if saved >= OBSERVATION_LIMIT:
        return saved
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": inspect_ui.now_iso(),
        "phase": result["phase"],
        "status": obs.get("status"),
        "events": result["events"],
        "sound": result["sound"],
        "window": {
            "hwnd_hex": (obs.get("window") or {}).get("hwnd_hex"),
            "is_foreground": (obs.get("window") or {}).get("is_foreground"),
            "is_iconic": (obs.get("window") or {}).get("is_iconic"),
            "is_win32_visible": (obs.get("window") or {}).get("is_win32_visible"),
        },
        "buttons": obs.get("buttons") or [],
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return saved + 1


def follow_window(config: dict, pinned_hwnd: int | None, cached_hwnd: int | None, select) -> tuple[dict, int | None]:
    """고정 창이 없으면, 기억한 창이 실패할 때 조건에 맞는 새 창을 다시 찾는다."""
    if pinned_hwnd is not None:
        return select(config, pinned_hwnd), cached_hwnd
    obs = select(config, cached_hwnd) if cached_hwnd is not None else select(config, None)
    if cached_hwnd is not None and obs.get("status") != "ok":
        obs = select(config, None)
    if obs.get("status") == "ok":
        cached_hwnd = (obs.get("window") or {}).get("hwnd")
    elif obs.get("status") in {"window_missing", "ambiguous_window", "anchor_missing"}:
        cached_hwnd = None
    return obs, cached_hwnd


def rebind_report(config: dict | None = None) -> str:
    config = config or load_config(CONFIG_PATH)
    obs, cached = follow_window(config, None, 1, select_window)
    window = obs.get("window") or {}
    listed = obs.get("listed") or {}
    cached_text = f"0x{cached:08X}" if isinstance(cached, int) else "-"
    return (
        f"status={obs.get('status')} hwnd={window.get('hwnd_hex', '-')} "
        f"cached={cached_text} exe={listed.get('process_exe', '-')}"
    )


def cmd_once(config: dict, hwnd: int | None) -> int:
    obs = select_window(config, hwnd)
    tracker = Tracker(config, lambda _token: None)
    result = tracker.update(obs, time.monotonic())
    print(format_observation(obs, result, config))
    return 0 if obs.get("status") == "ok" else 2


def cmd_run(config: dict, hwnd: int | None, max_seconds: float | None, config_path: Path | None = None) -> int:
    print("읽기 전용 감시. 클릭, 키 입력, 창 활성화를 하지 않는다.")
    print("입력란의 글과 답변 본문은 읽지 않는다. 생성 중으로 관측한 버튼이 화면에 있는지만 본다.")
    print("앱을 끄면 알리지 않는다. 다시 켜지면 프로세스, 창 종류, 제목, 입력란으로 새 창을 찾는다.")
    if will_play_sound(config):
        print("그 버튼이 보였다가 재확인 시간 동안 사라지면 저장한 소리를 한 번 재생한다. 취소와 오류로 멈춘 경우도 포함한다.")
        if config.get("popup_enabled"):
            print("그때 이 프로그램의 팝업도 띄운다.")
    else:
        print("알림 꺼짐: " + disarm_reason(config))
    path = config_path or CONFIG_PATH
    popups = notify.PopupService()
    tracker = Tracker(config, play_sound)
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    previous_signature = None
    saved = 0
    cached_hwnd = hwnd
    seen_hwnd = None
    log_path = Path(__file__).with_name("observations") / "composer.jsonl"
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("감시 제한 시간에 도달했다. 완료 알림을 보낸 것이 아니다.")
                return 0
            started = time.monotonic()
            refresh_alert_settings(path, config)
            previous_cached = cached_hwnd
            obs, cached_hwnd = follow_window(config, hwnd, cached_hwnd, select_window)
            result = tracker.update(obs, started)
            current_hwnd = (obs.get("window") or {}).get("hwnd")
            signature = _signature(obs)
            changed = signature != previous_signature or (
                obs.get("status") == "ok" and seen_hwnd not in (None, current_hwnd)
            )
            if changed or result["events"]:
                print(f"[{inspect_ui.now_iso()}]")
                print(format_observation(obs, result, config))
                if (
                    hwnd is None
                    and obs.get("status") == "ok"
                    and previous_cached not in (None, current_hwnd)
                    and current_hwnd
                ):
                    print("창 번호가 바뀌어 새 창에 맞췄다.")
            if changed:
                saved = _append_observation(log_path, obs, result, saved)
            if obs.get("status") == "ok":
                seen_hwnd = current_hwnd
            elif obs.get("status") in {"window_missing", "ambiguous_window"}:
                seen_hwnd = None
            if result.get("popup"):
                popups.show(language=str(config.get("language") or "ko"))
            previous_signature = signature
            remaining = float(config["poll_interval_sec"]) - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("사용자가 감시를 중단했다. 완료로 처리하지 않는다.")
        return 0


def _button(name: str, enabled: bool, unavailable: bool, offscreen: bool = False) -> dict:
    return {
        "name": name,
        "name_omitted": False,
        "name_unreadable": False,
        "enabled": enabled,
        "unavailable": unavailable,
        "offscreen": offscreen,
    }


def _obs(status: str, buttons: list[dict], iconic: bool = False) -> dict:
    return {"status": status, "buttons": buttons, "window": {"is_iconic": iconic}}


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    sounds: list[str] = []
    idle_names = ["파일", "제출"]
    config = {
        "poll_interval_sec": 0.5,
        "confirm_sec": 1.0,
        "sound_file": "",
        "generating_names": [],
        "completion_distinct_from_cancel": False,
        "observed_composer_button_names": idle_names,
        "idle_submit": {"name": "제출", "enabled": False, "legacy_unavailable": True},
    }
    idle = _obs("ok", [_button("파일", True, False), _button("제출", False, True)])
    typed = _obs("ok", [_button("파일", True, False), _button("제출", True, False)])
    tracker = Tracker(config, sounds.append)
    first = tracker.update(idle, 0)
    check("baseline-idle", first["phase"] == "IDLE" and first["sound"] is False and sounds == [])
    changed = tracker.update(typed, 1)
    check("typing-not-complete", changed["phase"] == "UNKNOWN" and "unconfirmed_change" in changed["events"])
    check("typing-silent", sounds == [])
    missing = tracker.update(_obs("window_missing", []), 2)
    check("missing-unknown", missing["phase"] == "UNKNOWN" and "unknown" in missing["events"] and sounds == [])
    back = tracker.update(idle, 3)
    check("resume-not-complete", "resumed_after_unknown" in back["events"] and sounds == [])

    armed = dict(config)
    armed["generating_names"] = ["중지"]
    armed["completion_distinct_from_cancel"] = True
    armed["sound_file"] = "custom.wav"
    armed["popup_enabled"] = True
    tracker = Tracker(armed, sounds.append)
    tracker.update(idle, 0)
    generating = _obs(
        "ok",
        [_button("파일", True, False), _button("중지", True, False)],
    )
    seen = tracker.update(generating, 1)
    check("saw-generating", seen["phase"] == "GENERATING" and seen["sound"] is False)
    early = tracker.update(idle, 1.4)
    check("confirm-wait", early["phase"] == "GENERATING" and "candidate" in early["events"] and sounds == [])
    flicker = tracker.update(generating, 1.6)
    check("flicker-resets", flicker["phase"] == "GENERATING" and sounds == [])
    tracker.update(idle, 2.0)
    done = tracker.update(idle, 3.1)
    check(
        "one-sound",
        done["sound"] is True and done["popup"] is True and sounds == ["custom.wav"] and "completed" in done["events"],
    )
    again = tracker.update(idle, 4)
    check("no-second-sound", again["sound"] is False and sounds == ["custom.wav"])
    tracker.update(generating, 5)
    tracker.update(idle, 6)
    tracker.update(idle, 7.1)
    check("second-generation-sounds", sounds == ["custom.wav", "custom.wav"])

    lost = Tracker(armed, sounds.append)
    lost.update(idle, 0)
    lost.update(generating, 1)
    interrupted = lost.update(_obs("read_failed", []), 2)
    check("read-failure-not-complete", "interrupted" in interrupted["events"] and interrupted["sound"] is False)
    after = lost.update(idle, 5)
    check("gap-prevents-sound", after["sound"] is False and "completed" not in after["events"])

    indistinct = dict(armed)
    indistinct["completion_distinct_from_cancel"] = False
    quiet: list[str] = []
    tracker = Tracker(indistinct, quiet.append)
    tracker.update(idle, 0)
    tracker.update(generating, 1)
    tracker.update(idle, 2)
    finished = tracker.update(idle, 3.2)
    check(
        "cancel-not-treated-as-success",
        finished["sound"] is False and "cancel_not_distinct" in finished["events"] and quiet == [],
    )

    accepted_sounds: list[str] = []
    accepted = dict(armed)
    accepted["completion_distinct_from_cancel"] = False
    accepted["sound_on_stop_disappear"] = True
    accepted["generating_names"] = ["응답 중지(Esc)"]
    stop = _obs("ok", [_button("응답 중지(Esc)", True, False, False)])
    gone = _obs("ok", [])
    tracker = Tracker(accepted, accepted_sounds.append)
    baseline = tracker.update(gone, 0)
    check("stop-absent-is-idle", baseline["phase"] == "IDLE" and baseline["sound"] is False)
    tracker.update(stop, 1)
    tracker.update(gone, 2)
    ended = tracker.update(gone, 3.2)
    check(
        "cancel-still-sounds",
        ended["sound"] is True and "generation_ended" in ended["events"] and accepted_sounds == ["custom.wav"],
    )
    hidden = Tracker(accepted, accepted_sounds.append)
    hidden_only = hidden.update(_obs("ok", [_button("응답 중지(Esc)", True, False, True)]), 0)
    check("offscreen-stop-is-not-generating", hidden_only["phase"] == "IDLE" and hidden_only["sound"] is False)

    restarted: list[str] = []
    tracker = Tracker(accepted, restarted.append)
    tracker.update(gone, 0)
    tracker.update(stop, 1)
    closed = tracker.update(_obs("window_missing", []), 1.2)
    check("close-during-generation-silent", closed["sound"] is False and "interrupted" in closed["events"])
    reopened = tracker.update(gone, 2)
    check("reopen-does-not-continue", reopened["sound"] is False and "generation_ended" not in reopened["events"])
    much_later = tracker.update(gone, 20)
    check("reopen-stays-silent", much_later["sound"] is False and restarted == [])
    tracker.update(stop, 21)
    tracker.update(gone, 22)
    ended_again = tracker.update(gone, 23.2)
    check("new-generation-after-reopen", ended_again["sound"] is True and restarted == ["custom.wav"])

    during_confirm: list[str] = []
    tracker = Tracker(accepted, during_confirm.append)
    tracker.update(stop, 0)
    tracker.update(gone, 1)
    tracker.update(_obs("window_missing", []), 1.4)
    after_gap = tracker.update(gone, 10)
    check("close-during-confirm-silent", after_gap["sound"] is False and during_confirm == [])

    minimized_sounds: list[str] = []
    tracker = Tracker(accepted, minimized_sounds.append)
    tracker.update(stop, 0)
    held = tracker.update(_obs("ok", [], iconic=True), 1)
    check("minimize-does-not-complete", held["sound"] is False and "interrupted" in held["events"])
    still = tracker.update(_obs("ok", [], iconic=True), 30)
    check("minimize-hold-stays-silent", still["sound"] is False and minimized_sounds == [])
    restored = tracker.update(gone, 31)
    check("restore-does-not-announce", restored["sound"] is False and "generation_ended" not in restored["events"])
    tracker.update(stop, 32)
    tracker.update(gone, 33)
    after_restore = tracker.update(gone, 34.2)
    check("seen-again-after-restore", after_restore["sound"] is True and minimized_sounds == ["custom.wav"])

    confirm_then_minimize: list[str] = []
    tracker = Tracker(accepted, confirm_then_minimize.append)
    tracker.update(stop, 0)
    tracker.update(gone, 1)
    tracker.update(_obs("ok", [], iconic=True), 1.4)
    later = tracker.update(gone, 10)
    check("minimize-cancels-pending-alert", later["sound"] is False and confirm_then_minimize == [])

    swapped: list[str] = []
    swapped_config = dict(accepted)
    tracker = Tracker(swapped_config, swapped.append)
    tracker.update(gone, 0)
    tracker.update(stop, 1)
    swapped_config["sound_file"] = "second.wav"
    swapped_config["popup_enabled"] = False
    tracker.update(gone, 2)
    heard = tracker.update(gone, 3.2)
    check(
        "settings-apply-to-next-alert",
        heard["sound"] is True and heard["popup"] is False and swapped == ["second.wav"],
    )

    calls: list[int | None] = []

    def fake_select(_config, selected):
        calls.append(selected)
        if selected == 5:
            return {"status": "window_missing", "buttons": [], "window": {"hwnd": 5}}
        if selected is None:
            return {"status": "ok", "buttons": [], "window": {"hwnd": 9, "hwnd_hex": "0x9"}}
        return {"status": "ok", "buttons": [], "window": {"hwnd": selected}}

    obs, cached = follow_window({}, None, 5, fake_select)
    check("rebind-scans-after-dead-hwnd", calls == [5, None] and cached == 9 and obs["status"] == "ok")
    calls.clear()
    _obs_pinned, cached = follow_window({}, 5, 5, fake_select)
    check("pinned-hwnd-does-not-scan", calls == [5] and cached == 5)

    def ambiguous_select(_config, selected):
        if selected == 5:
            return {"status": "window_missing", "buttons": [], "window": {}}
        return {"status": "ambiguous_window", "buttons": [], "window": {}, "match_count": 2}

    obs, cached = follow_window({}, None, 5, ambiguous_select)
    check("ambiguous-clears-cache", obs["status"] == "ambiguous_window" and cached is None)

    def failed_select(_config, selected):
        return {"status": "read_failed", "buttons": [], "window": {"hwnd": selected}}

    _obs_failed, cached = follow_window({}, None, 5, failed_select)
    check("read-failure-keeps-cache", cached == 5)

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "window": {"title": "Perplexity"},
                    "sound_file": "a.wav",
                    "sound_alias": "SystemAsterisk",
                    "popup_enabled": True,
                    "generating_names": ["응답 중지(Esc)"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        save_alert_settings(path, "b.wav", False)
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("save-keeps-detection", saved["window"]["title"] == "Perplexity" and saved["generating_names"] == ["응답 중지(Esc)"])
        check("save-replaces-alert", saved["sound_file"] == "b.wav" and saved["popup_enabled"] is False and "sound_alias" not in saved)
        live = {"sound_file": "", "popup_enabled": True}
        refresh_alert_settings(path, live)
        check("reload-reads-alert", live["sound_file"] == "b.wav" and live["popup_enabled"] is False)
        path.write_text("{", encoding="utf-8")
        refresh_alert_settings(path, live)
        check("reload-keeps-previous-on-corrupt", live["sound_file"] == "b.wav" and live["popup_enabled"] is False)

    wav_error = notify.validate_wav(Path("not-a-sound.txt"))
    check("reject-non-wav", wav_error == "not_wav")
    for lang in ("ko", "en", "zh", "ja"):
        for key in i18n.STRINGS["ko"]:
            check(f"i18n-{lang}-{key}", key in i18n.STRINGS[lang] and bool(i18n.text(lang, key)))
    check("i18n-default-korean", i18n.normalize("nope") == "ko" and i18n.normalize(None) == "ko")
    notify.ensure_default_sound(notify.DEFAULT_SOUND)
    check("default-wav", notify.validate_wav(notify.DEFAULT_SOUND) is None)

    real = load_config(CONFIG_PATH)
    check("real-config-has-anchor", real["anchor"]["automation_id"] == "ask-input")
    check("real-config-armed", will_play_sound(real) is True)
    check("real-config-stop-name", real["generating_names"] == ["응답 중지(Esc)"])
    check("real-config-sounds-on-disappear", real["sound_on_stop_disappear"] is True)
    check("real-config-cancel-not-distinct", real["completion_distinct_from_cancel"] is False)
    check("real-config-no-system-alias", "sound_alias" not in real)
    check("real-config-sound-file", real["sound_file"] == "sounds/done.wav")
    check("real-config-popup", real["popup_enabled"] is True)
    check("real-config-language", real["language"] == "ko")
    check("real-wav-plays-as-file", notify.validate_wav(notify.resolve_sound_file(real["sound_file"])) is None)

    if failures:
        print("self-test 실패: " + ", ".join(failures))
        return 1
    print("self-test 통과")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="생성 중에 관측한 중지 버튼이 사라지면 소리를 한 번 재생한다. 답변 본문은 읽지 않는다."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="한 번 읽고 상태를 출력한다")
    once.add_argument("--hwnd", type=inspect_ui.parse_hwnd)
    once.add_argument("--config", type=Path, default=CONFIG_PATH)
    run = sub.add_parser("run", help="폴링하며 변화만 출력한다")
    run.add_argument("--hwnd", type=inspect_ui.parse_hwnd)
    run.add_argument("--config", type=Path, default=CONFIG_PATH)
    run.add_argument("--max-seconds", type=float)
    test = sub.add_parser("self-test", help="상태 머신을 UI 없이 검증한다")
    test.add_argument("--config", type=Path, default=CONFIG_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    inspect_ui.configure_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        return self_test()
    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"설정을 읽지 못했다: {type(exc).__name__}")
        return 2
    if args.command == "once":
        return cmd_once(config, args.hwnd)
    return cmd_run(config, args.hwnd, args.max_seconds, args.config)


if __name__ == "__main__":
    raise SystemExit(main())
