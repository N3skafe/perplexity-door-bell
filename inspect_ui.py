"""읽기 전용 UI Automation 검사 도구.

실행 중인 창의 UI 구조를 살펴보고, 두 시점의 차이를 비교한다.
클릭, 키 입력, 창 활성화, 값 변경은 하지 않는다.
네트워크 요청을 보내지 않고, 입력란과 문서의 내용도 저장하지 않는다.

특정 앱의 프로세스 이름, 버튼 이름, AutomationId는 코드에 들어 있지 않다.
검사로 확인된 값만 스냅샷에 기록하고, 감지기는 그 기록만 사용해야 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

SCHEMA = 1
TOOL_NAME = "inspect_ui"
SHORT_BUTTON_LIMIT = 16
SHORT_LABEL_LIMIT = 16
WINDOW_TITLE_LIMIT = 32
ALWAYS_OMIT_TYPES = {"Edit", "Document"}
BUTTON_TYPES = {
    "Button",
    "SplitButton",
    "CheckBox",
    "RadioButton",
    "MenuItem",
    "Hyperlink",
}
HIGHLIGHT_TYPES = BUTTON_TYPES | {
    "ProgressBar",
    "StatusBar",
    "Slider",
    "Edit",
    "Document",
    "TabItem",
    "ComboBox",
    "TitleBar",
    "Window",
}
# MSAA 상태 비트. 앱별 이름이 아니라 Windows 접근성 상수다.
LEGACY_FOCUSED = 0x4
LEGACY_HOTTRACKED = 0x80
LEGACY_INVISIBLE = 0x8000
LEGACY_OFFSCREEN = 0x10000
LEGACY_VOLATILE_OR_VISIBILITY = (
    LEGACY_FOCUSED | LEGACY_HOTTRACKED | LEGACY_INVISIBLE | LEGACY_OFFSCREEN
)
LEGACY_STATE_NAMES = (
    (0x1, "unavailable"),
    (0x2, "selected"),
    (0x8, "pressed"),
    (0x10, "checked"),
    (0x20, "mixed"),
    (0x40, "readonly"),
    (0x100, "default"),
    (0x200, "expanded"),
    (0x400, "collapsed"),
    (0x800, "busy"),
    (0x1000, "floating"),
    (0x2000, "marqueed"),
    (0x4000, "animated"),
    (0x8000, "invisible"),
    (0x10000, "offscreen"),
    (0x100000, "focusable"),
    (0x200000, "selectable"),
)
WINDOW_FIELDS = (
    "process_exe",
    "is_foreground",
    "is_iconic",
    "is_win32_visible",
    "window_visual_state",
)
CHROME_FIELDS = (
    "control_type",
    "automation_id",
    "class_name",
    "name",
    "name_omitted",
    "enabled",
    "item_status",
    "item_status_omitted",
    "toggle_state",
    "range_value",
    "expand_state",
    "selected",
    "patterns",
    "window_visual_state",
    "aria_role",
    "legacy_role",
    "legacy_state_stable",
    "name_unreadable",
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_hwnd(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "hwnd는 10진수 또는 0x로 시작하는 16진수여야 합니다."
        ) from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("hwnd는 0보다 커야 합니다.")
    return value


def redact_label(control_type: str, text: str, *, is_password: bool = False) -> tuple[str, int, bool]:
    """UI 라벨만 남기고, 본문으로 보일 수 있는 문자열은 버린다."""
    raw = "" if text is None else str(text)
    length = len(raw)
    if is_password or "\n" in raw or "\r" in raw or control_type in ALWAYS_OMIT_TYPES:
        return "", length, True
    if control_type in {"Window", "TitleBar"}:
        limit = WINDOW_TITLE_LIMIT
    elif control_type in BUTTON_TYPES:
        limit = SHORT_BUTTON_LIMIT
    else:
        limit = SHORT_LABEL_LIMIT
    if length > limit:
        return "", length, True
    return " ".join(raw.split()), length, False


def redact_short_field(text: str, limit: int = SHORT_LABEL_LIMIT) -> tuple[str, bool]:
    raw = "" if text is None else str(text)
    if raw == "":
        return "", False
    if "\n" in raw or "\r" in raw or len(raw) > limit:
        return "", True
    return " ".join(raw.split()), False


def decode_legacy_state(state: int | None) -> str:
    if state is None:
        return ""
    names = [name for bit, name in LEGACY_STATE_NAMES if state & bit]
    return ",".join(names)


def stable_legacy_state(state: int | None) -> int | None:
    if state is None:
        return None
    return int(state) & ~LEGACY_VOLATILE_OR_VISIBILITY


def sample_element(**overrides: object) -> dict:
    element = {
        "path": "0",
        "depth": 0,
        "control_type": "Button",
        "automation_id": "",
        "class_name": "",
        "framework_id": "",
        "name": "",
        "name_length": 0,
        "name_omitted": False,
        "name_unreadable": False,
        "enabled": True,
        "offscreen": False,
        "item_status": "",
        "item_status_omitted": False,
        "aria_role": "",
        "is_password": False,
        "patterns": [],
        "toggle_state": None,
        "range": None,
        "expand_state": None,
        "selected": None,
        "window_visual_state": "",
        "legacy_role": None,
        "legacy_state": None,
        "rect": None,
        "runtime_id": [],
        "children_error": False,
        "prop_error": False,
    }
    element.update(overrides)
    return element


def chrome_from_element(element: dict) -> dict:
    range_value = element.get("range")
    if range_value:
        stored_range = {
            "min": range_value.get("min"),
            "max": range_value.get("max"),
            "value": range_value.get("value"),
        }
    else:
        stored_range = None
    return {
        "control_type": element.get("control_type") or "",
        "automation_id": element.get("automation_id") or "",
        "class_name": element.get("class_name") or "",
        "name": element.get("name") or "",
        "name_omitted": bool(element.get("name_omitted")),
        "name_unreadable": bool(element.get("name_unreadable")),
        "enabled": element.get("enabled", None),
        "item_status": element.get("item_status") or "",
        "item_status_omitted": bool(element.get("item_status_omitted")),
        "toggle_state": element.get("toggle_state", None),
        "range_value": stored_range,
        "expand_state": element.get("expand_state", None),
        "selected": element.get("selected", None),
        "patterns": sorted(element.get("patterns") or []),
        "window_visual_state": element.get("window_visual_state") or "",
        "aria_role": element.get("aria_role") or "",
        "legacy_role": element.get("legacy_role", None),
        "legacy_state_stable": stable_legacy_state(element.get("legacy_state")),
    }


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _is_frozen_map(value: tuple) -> bool:
    return bool(value) and all(
        isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value
    )


def _thaw(value: object) -> object:
    if isinstance(value, tuple):
        if _is_frozen_map(value):
            return {key: _thaw(item) for key, item in value}
        return [_thaw(item) for item in value]
    return value


def chrome_key(chrome: dict, *, include_visual: bool = True) -> tuple:
    items = []
    for field in CHROME_FIELDS:
        if not include_visual and field == "window_visual_state":
            items.append((field, ""))
            continue
        items.append((field, _freeze(chrome.get(field))))
    return tuple(items)


def chrome_counter(elements: list[dict], *, include_visual: bool = True) -> Counter:
    counter: Counter = Counter()
    for element in elements:
        counter[chrome_key(chrome_from_element(element), include_visual=include_visual)] += 1
    return counter


def counter_diff(before: Counter, after: Counter) -> tuple[list[tuple[object, int]], list[tuple[object, int]]]:
    removed = list((before - after).items())
    added = list((after - before).items())
    removed.sort(key=lambda item: str(item[0]))
    added.sort(key=lambda item: str(item[0]))
    return removed, added


def make_snapshot(elements: list[dict], **window_overrides: object) -> dict:
    window = {
        "hwnd": 1,
        "hwnd_hex": "0x00000001",
        "pid": 1,
        "process_exe": "App.exe",
        "title": "",
        "title_length": 0,
        "title_omitted": False,
        "class_name": "",
        "is_foreground": False,
        "is_iconic": False,
        "is_win32_visible": True,
        "window_visual_state": "",
    }
    window.update(window_overrides)
    return {
        "tool": TOOL_NAME,
        "schema": SCHEMA,
        "status": "ok",
        "failure": None,
        "elements": elements,
        "window": window,
        "read_error_count": 0,
        "elements_capped": False,
        "depth_limit_reached": False,
    }


def _hard_failure(label: str, snap: dict) -> str | None:
    """읽기 실패만 비교를 막는다. 깊이 제한으로 잘린 트리는 경고 후 비교한다."""
    failure = snap.get("failure")
    status = snap.get("status")
    if status == "failed" or failure in {"window_missing", "uia_failed", "uia_unavailable"}:
        return f"{label}:{failure or status or 'failed'}"
    if snap.get("read_error_count") or failure == "read_error":
        return f"{label}:read_error"
    if snap.get("pattern_probe_incomplete_count") or failure == "pattern_probe_incomplete":
        return f"{label}:pattern_probe_incomplete"
    if status not in {None, "ok", "partial"}:
        return f"{label}:{status}"
    return None


def diff_snapshots(before: dict, after: dict) -> dict:
    reasons: list[str] = []
    warnings: list[str] = []
    for label, snap in (("before", before), ("after", after)):
        reason = _hard_failure(label, snap)
        if reason:
            reasons.append(reason)
        if snap.get("depth_limit_reached"):
            warnings.append(
                f"{label} 스냅샷은 깊이 제한에서 잘렸다. 더 아래 요소의 차이는 포함되지 않는다."
            )
        if snap.get("elements_capped"):
            warnings.append(
                f"{label} 스냅샷은 요소 수 제한에서 잘렸다. 빠뜨린 요소의 차이는 포함되지 않는다."
            )

    comparable = not reasons
    window_changes = {}
    if comparable:
        left = before.get("window") or {}
        right = after.get("window") or {}
        for field in WINDOW_FIELDS:
            if left.get(field) != right.get(field):
                window_changes[field] = {"before": left.get(field), "after": right.get(field)}
        if left.get("hwnd") != right.get("hwnd"):
            warnings.append("different_hwnd")

    removed: list[dict] = []
    added: list[dict] = []
    visibility_changes: list[dict] = []
    only_window_visual_state = False
    if comparable:
        before_elements = before.get("elements") or []
        after_elements = after.get("elements") or []
        removed_pairs, added_pairs = counter_diff(
            chrome_counter(before_elements),
            chrome_counter(after_elements),
        )
        removed = [_public_chrome(key, count) for key, count in removed_pairs]
        added = [_public_chrome(key, count) for key, count in added_pairs]
        bare_removed, bare_added = counter_diff(
            chrome_counter(before_elements, include_visual=False),
            chrome_counter(after_elements, include_visual=False),
        )
        only_window_visual_state = bool(removed or added) and not bare_removed and not bare_added
        visibility_changes = _visibility_changes(before_elements, after_elements)

    return {
        "comparable": comparable,
        "reasons_not_comparable": reasons,
        "warnings": sorted(set(warnings)),
        "window_changes": window_changes,
        "chrome_removed": removed,
        "chrome_added": added,
        "visibility_changes": visibility_changes,
        "only_window_visual_state": only_window_visual_state,
        "type_count_before": dict(Counter(
            element.get("control_type") or "" for element in (before.get("elements") or [])
        )),
        "type_count_after": dict(Counter(
            element.get("control_type") or "" for element in (after.get("elements") or [])
        )),
    }


def _public_chrome(key: tuple, count: int) -> dict:
    data = {field: _thaw(value) for field, value in key}
    data["count"] = count
    return data


def _visibility_changes(before_elements: list[dict], after_elements: list[dict]) -> list[dict]:
    def grouped(elements: list[dict]) -> dict[tuple, Counter]:
        groups: dict[tuple, Counter] = {}
        for element in elements:
            key = chrome_key(chrome_from_element(element), include_visual=False)
            offscreen = element.get("offscreen", None)
            bucket = "unknown" if offscreen is None else ("true" if offscreen else "false")
            groups.setdefault(key, Counter())[bucket] += 1
        return groups

    left = grouped(before_elements)
    right = grouped(after_elements)
    changes = []
    for key in sorted(set(left) & set(right), key=str):
        if left[key] == right[key]:
            continue
        if sum(left[key].values()) != sum(right[key].values()):
            continue
        changes.append(
            {
                "chrome": _public_chrome(key, sum(right[key].values())),
                "offscreen_before": dict(left[key]),
                "offscreen_after": dict(right[key]),
            }
        )
    return changes


def classify_transition(previous_status: str | None, new_status: str, changed: bool) -> str:
    if new_status != "ok":
        return "unknown"
    if previous_status is None:
        return "baseline"
    if previous_status != "ok":
        return "resumed_after_unknown"
    if changed:
        return "changed"
    return "unchanged"


def format_chrome(item: dict) -> str:
    parts = [item.get("control_type") or "(type 없음)"]
    if item.get("automation_id"):
        parts.append(f"id={item['automation_id']}")
    if item.get("class_name"):
        parts.append(f"class={item['class_name']}")
    if item.get("name_unreadable"):
        parts.append("name=(읽기 실패)")
    elif item.get("name_omitted"):
        parts.append("name=(생략)")
    elif item.get("name"):
        parts.append(f"name={item['name']}")
    else:
        parts.append("name=(비어 있음)")
    enabled = item.get("enabled", None)
    if enabled is True:
        parts.append("enabled")
    elif enabled is False:
        parts.append("disabled")
    else:
        parts.append("enabled=?")
    if item.get("item_status") and not item.get("item_status_omitted"):
        parts.append(f"status={item['item_status']}")
    elif item.get("item_status_omitted"):
        parts.append("status=(생략)")
    if item.get("toggle_state") is not None:
        parts.append(f"toggle={item['toggle_state']}")
    if item.get("range_value"):
        parts.append(f"range={item['range_value']}")
    if item.get("expand_state") is not None:
        parts.append(f"expand={item['expand_state']}")
    if item.get("selected") is not None:
        parts.append(f"selected={item['selected']}")
    if item.get("patterns"):
        parts.append("patterns=" + ",".join(item["patterns"]))
    if item.get("window_visual_state"):
        parts.append(f"visual={item['window_visual_state']}")
    if item.get("aria_role"):
        parts.append(f"aria={item['aria_role']}")
    if item.get("legacy_role") is not None:
        parts.append(f"legacy_role={item['legacy_role']}")
    if item.get("legacy_state_stable"):
        parts.append(f"legacy={decode_legacy_state(item['legacy_state_stable'])}")
    count = item.get("count", 1)
    prefix = f"x{count} " if count != 1 else ""
    return prefix + " | ".join(parts)


def print_diff(report: dict, before_label: str, after_label: str) -> None:
    print(f"비교: {before_label} -> {after_label}")
    if not report["comparable"]:
        print("비교 불가. 실패한 조회를 UI 변화나 완료로 해석하지 않는다.")
        for reason in report["reasons_not_comparable"]:
            print(f"  사유: {reason}")
        return
    if report["warnings"]:
        print("주의:")
        for warning in report["warnings"]:
            print(f"  {warning}")
        print("잘린 구간의 차이는 완료 신호로 쓰지 않는다.")
    if report["window_changes"]:
        print("창 상태:")
        for field, change in report["window_changes"].items():
            print(f"  {field}: {change['before']} -> {change['after']}")
    type_before = report["type_count_before"]
    type_after = report["type_count_after"]
    type_fields = sorted(set(type_before) | set(type_after))
    type_changes = [
        (field, type_before.get(field, 0), type_after.get(field, 0))
        for field in type_fields
        if type_before.get(field, 0) != type_after.get(field, 0)
    ]
    if type_changes:
        print("컨트롤 종류 수:")
        for field, left, right in type_changes:
            print(f"  {field or '(빈 타입)'}: {left} -> {right}")
    if report["only_window_visual_state"]:
        print("요소 차이는 창 표시 상태(normal/maximized/minimized)뿐이다.")
    _print_chrome_list("빠짐", report["chrome_removed"])
    _print_chrome_list("추가", report["chrome_added"])
    if report["visibility_changes"]:
        print(
            f"offscreen만 다른 요소: {len(report['visibility_changes'])}종류 "
            "(이 변화만으로 완료로 처리하지 않는다)"
        )
        for item in report["visibility_changes"][:40]:
            print(
                "  "
                + format_chrome(item["chrome"])
                + f" | offscreen {item['offscreen_before']} -> {item['offscreen_after']}"
            )
        rest = len(report["visibility_changes"]) - 40
        if rest > 0:
            print(f"  ... 나머지 {rest}종류")
    if (
        not report["window_changes"]
        and not report["chrome_removed"]
        and not report["chrome_added"]
        and not report["visibility_changes"]
    ):
        print("저장한 UI 상태에 차이가 없다.")


def _print_chrome_list(title: str, items: list[dict]) -> None:
    if not items:
        return
    print(f"{title}: {len(items)}종류")
    for item in items[:80]:
        print("  " + format_chrome(item))
    rest = len(items) - 80
    if rest > 0:
        print(f"  ... 나머지 {rest}종류")


def is_highlight(element: dict) -> bool:
    control_type = element.get("control_type") or ""
    if control_type in HIGHLIGHT_TYPES:
        return True
    patterns = set(element.get("patterns") or [])
    if patterns & {"Invoke", "Toggle", "RangeValue", "ExpandCollapse", "SelectionItem"}:
        return True
    if element.get("item_status") or element.get("item_status_omitted"):
        return True
    if element.get("toggle_state") is not None or element.get("range") is not None:
        return True
    if element.get("legacy_state_stable") or stable_legacy_state(element.get("legacy_state")):
        return True
    if element.get("name") and control_type in {"Text", "Custom", "Group"}:
        return True
    return False


def format_element_line(element: dict) -> str:
    indent = "  " * int(element.get("depth") or 0)
    parts = [f"[{element.get('path')}] {element.get('control_type') or '(type 없음)'}"]
    if element.get("automation_id"):
        parts.append(f"id={element['automation_id']}")
    if element.get("class_name"):
        parts.append(f"class={element['class_name']}")
    if element.get("name_unreadable"):
        parts.append("name=(읽기 실패)")
    elif element.get("name_omitted"):
        parts.append("name=(생략)")
    elif element.get("name"):
        parts.append(f"name={element['name']}")
    enabled = element.get("enabled", None)
    parts.append("enabled" if enabled is True else "disabled" if enabled is False else "enabled=?")
    offscreen = element.get("offscreen", None)
    parts.append(
        "offscreen" if offscreen is True else "onscreen" if offscreen is False else "offscreen=?"
    )
    if element.get("patterns"):
        parts.append("patterns=" + ",".join(element["patterns"]))
    if element.get("window_visual_state"):
        parts.append(f"visual={element['window_visual_state']}")
    legacy = decode_legacy_state(stable_legacy_state(element.get("legacy_state")))
    if legacy:
        parts.append(f"legacy={legacy}")
    if element.get("children_error") or element.get("prop_error"):
        parts.append("read_error")
    rect = element.get("rect") or {}
    if rect:
        parts.append(f"{rect.get('w')}x{rect.get('h')}")
    return indent + " | ".join(parts)


def print_snapshot(snapshot: dict) -> None:
    window = snapshot.get("window") or {}
    print("읽기 전용 UIA 검사. 클릭, 키 입력, 창 활성화를 하지 않았다.")
    print(f"시각: {snapshot.get('captured_at')}")
    print(f"라벨: {snapshot.get('label')}")
    if snapshot.get("note"):
        print(f"메모: {snapshot.get('note')}")
    print(
        "창: "
        f"hwnd={window.get('hwnd_hex')} "
        f"pid={window.get('pid')} "
        f"exe={window.get('process_exe') or '(알 수 없음)'} "
        f"class={window.get('class_name') or ''}"
    )
    if window.get("title_omitted"):
        print("제목: (생략)")
    elif window.get("title"):
        print(f"제목: {window.get('title')}")
    else:
        print("제목: (비어 있음)")
    print(
        "Win32: "
        f"foreground={_yn(window.get('is_foreground'))} "
        f"minimized={_yn(window.get('is_iconic'))} "
        f"visible={_yn(window.get('is_win32_visible'))} "
        f"visual={window.get('window_visual_state') or '?'}"
    )
    print(
        "조회: "
        f"status={snapshot.get('status')} "
        f"elements={len(snapshot.get('elements') or [])} "
        f"errors={snapshot.get('read_error_count')} "
        f"pattern_probe_gaps={snapshot.get('pattern_probe_incomplete_count', 0)} "
        f"depth_limit={_yn(snapshot.get('depth_limit_reached'))} "
        f"element_cap={_yn(snapshot.get('elements_capped'))} "
        f"duration_ms={snapshot.get('duration_ms')}"
    )
    failure = snapshot.get("failure")
    if failure == "read_error":
        print("요소 속성을 읽지 못한 곳이 있다. 이 결과를 감지 성공으로 처리하지 않는다.")
    elif failure == "pattern_probe_incomplete":
        print("일부 패턴을 확인하지 못했다. 이 결과를 감지 성공으로 처리하지 않는다.")
    elif failure == "depth_truncated":
        print(
            f"읽기 오류는 없다. 최대 깊이 {snapshot.get('max_depth')}에서 "
            "자식이 더 있어 그 아래는 수집하지 않았다."
        )
    elif failure == "element_cap":
        print("읽기 오류는 없다. 요소 수 제한에 도달해서 나머지를 수집하지 않았다.")
    elif snapshot.get("status") != "ok":
        print("UI를 온전히 읽지 못했다. 이 결과를 감지 성공으로 처리하지 않는다.")
        if failure:
            print(f"실패 사유: {failure}")
    counts = Counter(element.get("control_type") or "" for element in snapshot.get("elements") or [])
    if counts:
        print("컨트롤 종류:")
        for name, count in sorted(counts.items()):
            print(f"  {name or '(빈 타입)'}: {count}")
    highlights = [element for element in snapshot.get("elements") or [] if is_highlight(element)]
    print(f"주요 요소: {len(highlights)}개")
    for element in highlights:
        print(format_element_line(element))
    print(f"전체 트리: {len(snapshot.get('elements') or [])}개")
    for element in snapshot.get("elements") or []:
        print(format_element_line(element))


def _yn(value: object) -> str:
    if value is True:
        return "Y"
    if value is False:
        return "N"
    return "?"


def chrome_digest(elements: list[dict]) -> str:
    lines = [repr(chrome_key(chrome_from_element(element))) for element in elements]
    lines.sort()
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def load_snapshot(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("tool") != TOOL_NAME or data.get("schema") != SCHEMA:
        raise ValueError(f"이 도구의 스냅샷이 아닙니다: {path}")
    return data


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    kept, length, omitted = redact_label("Button", "Stop")
    check("button-kept", kept == "Stop" and length == 4 and omitted is False)
    _text, _length, omitted = redact_label("Edit", "answer text")
    check("edit-omitted", omitted is True and _text == "")
    _text, _length, omitted = redact_label("Document", "title")
    check("document-omitted", omitted is True)
    _text, _length, omitted = redact_label("Text", "a\nb")
    check("newline-omitted", omitted is True and _text == "")
    _text, _length, omitted = redact_label("Button", "secret", is_password=True)
    check("password-omitted", omitted is True and _text == "")
    _text, length, omitted = redact_label("Button", "x" * 81)
    check("long-button-omitted", omitted is True and length == 81 and _text == "")
    _text, length, omitted = redact_label("Button", "x" * 20)
    check("medium-button-omitted", omitted is True and length == 20 and _text == "")
    _text, _length, omitted = redact_label("Window", "ExampleApp")
    check("short-window-title-kept", omitted is False and _text == "ExampleApp")
    _text, _length, omitted = redact_label("Window", "Q" * 33)
    check("long-window-title-omitted", omitted is True and _text == "")

    short_text = sample_element(control_type="Text", name="", name_omitted=True, name_length=8)
    long_text = sample_element(control_type="Text", name="", name_omitted=True, name_length=4000)
    check("content-length-ignored", chrome_from_element(short_text) == chrome_from_element(long_text))
    unreadable = sample_element(name="", name_unreadable=True)
    blank = sample_element(name="", name_unreadable=False)
    check("unreadable-name-distinct", chrome_from_element(unreadable) != chrome_from_element(blank))

    idle = make_snapshot([sample_element(name="Send", automation_id="submit")])
    moved = make_snapshot(
        [sample_element(name="Send", automation_id="submit", rect={"l": 9, "t": 9, "w": 10, "h": 10})]
    )
    same = diff_snapshots(idle, moved)
    check("position-ignored", same["comparable"] and not same["chrome_added"] and not same["chrome_removed"])

    generating = make_snapshot([sample_element(name="Stop", automation_id="submit")])
    changed = diff_snapshots(idle, generating)
    check(
        "name-change",
        changed["comparable"]
        and changed["chrome_removed"]
        and changed["chrome_added"]
        and changed["chrome_removed"][0]["name"] == "Send"
        and changed["chrome_added"][0]["name"] == "Stop",
    )

    hidden = make_snapshot([sample_element(name="Send", automation_id="submit", offscreen=True)])
    visibility = diff_snapshots(idle, hidden)
    check(
        "offscreen-separate",
        visibility["comparable"]
        and not visibility["chrome_added"]
        and not visibility["chrome_removed"]
        and len(visibility["visibility_changes"]) == 1,
    )

    minimized = make_snapshot(
        [sample_element(name="Send", automation_id="submit", window_visual_state="minimized")],
        is_iconic=True,
        window_visual_state="minimized",
    )
    visual = diff_snapshots(idle, minimized)
    check("minimize-window-flag", visual["window_changes"].get("is_iconic", {}).get("after") is True)
    check("minimize-visual-only-elements", visual["only_window_visual_state"] is True)

    missing = make_snapshot([], failure="window_missing")
    missing["status"] = "failed"
    failed = diff_snapshots(idle, missing)
    check("failed-read-not-comparable", failed["comparable"] is False)
    truncated = make_snapshot([sample_element(name="Send", automation_id="submit")])
    truncated["status"] = "partial"
    truncated["failure"] = "depth_truncated"
    truncated["depth_limit_reached"] = True
    other = make_snapshot([sample_element(name="Stop", automation_id="submit")])
    other["status"] = "partial"
    other["failure"] = "incomplete_or_read_error"
    other["depth_limit_reached"] = True
    limited = diff_snapshots(truncated, other)
    check(
        "depth-limit-still-comparable",
        limited["comparable"] is True and limited["chrome_added"] and limited["chrome_removed"],
    )
    read_failed = make_snapshot([sample_element(name="Send")])
    read_failed["status"] = "partial"
    read_failed["failure"] = "read_error"
    read_failed["read_error_count"] = 2
    blocked = diff_snapshots(idle, read_failed)
    check("read-error-not-comparable", blocked["comparable"] is False)

    check("transition-baseline", classify_transition(None, "ok", False) == "baseline")
    check("transition-unknown", classify_transition("ok", "failed", True) == "unknown")
    check(
        "transition-resume",
        classify_transition("failed", "ok", True) == "resumed_after_unknown",
    )
    check("transition-changed", classify_transition("ok", "ok", True) == "changed")
    check("transition-unchanged", classify_transition("ok", "ok", False) == "unchanged")
    check("busy-bit", "busy" in decode_legacy_state(0x800))
    check(
        "focus-bit-not-stable",
        stable_legacy_state(LEGACY_FOCUSED | 0x800) == 0x800,
    )
    json.dumps(changed, ensure_ascii=False)
    json.dumps(visibility, ensure_ascii=False)

    if failures:
        print("self-test 실패: " + ", ".join(failures))
        return 1
    print("self-test 통과 (읽기 전용 비교 로직)")
    return 0


# Win32와 UIA는 검사 명령을 실행할 때만 초기화한다.
user32 = None
kernel32 = None


def init_win32() -> None:
    global user32, kernel32
    if user32 is not None:
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _window_text(hwnd: int) -> str:
    import ctypes

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(256)
    copied = user32.GetClassNameW(hwnd, buffer, 256)
    if copied <= 0:
        return ""
    return buffer.value


def _process_id(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_exe(pid: int, cache: dict[int, str]) -> str:
    import ctypes
    from ctypes import wintypes

    if pid in cache:
        return cache[pid]
    if pid <= 0:
        cache[pid] = ""
        return ""
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        cache[pid] = ""
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            cache[pid] = ""
            return ""
        cache[pid] = Path(buffer.value).name
        return cache[pid]
    finally:
        kernel32.CloseHandle(process)


def list_windows(contains: str | None = None) -> list[dict]:
    import ctypes
    from ctypes import wintypes

    init_win32()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    if not user32.EnumWindows(callback, 0):
        raise OSError("창 목록을 열거하지 못했다.")

    foreground = int(user32.GetForegroundWindow() or 0)
    exe_cache: dict[int, str] = {}
    rows = []
    needle = (contains or "").casefold()
    for hwnd in found:
        title = _window_text(hwnd)
        visible = bool(user32.IsWindowVisible(hwnd))
        iconic = bool(user32.IsIconic(hwnd))
        if not visible and not iconic and title == "":
            continue
        class_name = _class_name(hwnd)
        pid = _process_id(hwnd)
        exe = _process_exe(pid, exe_cache)
        stored_title, title_length, title_omitted = redact_label("Window", title)
        row = {
            "hwnd": hwnd,
            "hwnd_hex": f"0x{hwnd:08X}",
            "pid": pid,
            "process_exe": exe,
            "class_name": class_name,
            "title": stored_title,
            "title_length": title_length,
            "title_omitted": title_omitted,
            "is_foreground": hwnd == foreground,
            "is_iconic": iconic,
            "is_win32_visible": visible,
        }
        if needle:
            haystack = " ".join(
                [exe, class_name, stored_title if not title_omitted else ""]
            ).casefold()
            if needle not in haystack:
                continue
        rows.append(row)
    rows.sort(key=lambda item: (item["process_exe"].casefold(), item["title"].casefold(), item["hwnd"]))
    return rows


def print_window_list(rows: list[dict]) -> None:
    print("읽기 전용 창 목록. 창을 활성화하거나 입력을 보내지 않는다.")
    print("제목이 길거나 여러 줄이면 내용을 생략한다. 최소화된 창도 포함한다.")
    print(f"표시 {len(rows)}개")
    for row in rows:
        if row["title_omitted"]:
            title = "(제목 생략)"
        else:
            title = row["title"] or "(제목 없음)"
        print(
            f"{row['hwnd_hex']}  pid={row['pid']:<6}  "
            f"vis={_yn(row['is_win32_visible'])} min={_yn(row['is_iconic'])} "
            f"fg={_yn(row['is_foreground'])}  "
            f"exe={row['process_exe'] or '(알 수 없음)'}  "
            f"class={row['class_name']}  title={title}"
        )


_UIA = None


def init_uia() -> bool:
    global _UIA
    if _UIA is not None:
        return True
    try:
        import comtypes
        from pywinauto import timings
        from pywinauto.uia_defines import IUIA
    except ImportError:
        print("pywinauto가 설치되어 있지 않습니다.")
        print("설치: python -m pip install -r requirements.txt")
        return False
    try:
        comtypes.CoInitialize()
        timings.Timings.fast()
        timings.Timings.window_find_timeout = 1
        iuia = IUIA()
        import comtypes.gen.UIAutomationClient as api
    except Exception as exc:
        print(f"UI Automation을 시작하지 못했습니다: {type(exc).__name__}")
        return False
    _UIA = {"iuia": iuia, "api": api}
    missing = [
        name
        for name in (
            "UIA_IsEnabledPropertyId",
            "UIA_IsOffscreenPropertyId",
            "UIA_ItemStatusPropertyId",
            "UIA_IsPasswordPropertyId",
            "UIA_AriaRolePropertyId",
            "UIA_IsInvokePatternAvailablePropertyId",
            "UIA_IsTogglePatternAvailablePropertyId",
            "UIA_IsRangeValuePatternAvailablePropertyId",
            "UIA_IsExpandCollapsePatternAvailablePropertyId",
            "UIA_IsSelectionItemPatternAvailablePropertyId",
            "UIA_IsValuePatternAvailablePropertyId",
            "UIA_IsWindowPatternAvailablePropertyId",
            "UIA_IsLegacyIAccessiblePatternAvailablePropertyId",
            "UIA_LegacyIAccessiblePatternId",
            "IUIAutomationLegacyIAccessiblePattern",
        )
        if not hasattr(api, name)
    ]
    if missing:
        print("UIA 타입 라이브러리에서 다음 항목을 찾지 못했다: " + ", ".join(missing))
        print("찾지 못한 속성은 추측해서 채우지 않고 비워 둔다.")
    return True


def _api():
    return _UIA["api"]


def _prop_id(name: str) -> int | None:
    api = _api()
    if not hasattr(api, name):
        return None
    return int(getattr(api, name))


def _raw_element(info):
    element = getattr(info, "element", None)
    if callable(element):
        element = element()
    if element is None:
        element = getattr(info, "_element", None)
    return element


_MISSING = object()


def _property(element, prop_id: int | None):
    if element is None or prop_id is None:
        return _MISSING
    try:
        return element.GetCurrentPropertyValue(prop_id)
    except Exception:
        return _MISSING


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _read_patterns(wrapper, element, prop_errors: list[bool]) -> dict:
    api = _api()
    patterns: list[str] = []
    result = {
        "patterns": patterns,
        "toggle_state": None,
        "range": None,
        "expand_state": None,
        "selected": None,
        "window_visual_state": "",
        "legacy_role": None,
        "legacy_state": None,
    }

    def available(attr: str) -> bool:
        prop_id = _prop_id(attr)
        if prop_id is None:
            return False
        value = _property(element, prop_id)
        # 선택 패턴 조회 실패는 그 패턴이 없다고 단정하지 않되,
        # 요소 전체를 읽기 실패로 만들지는 않는다. 호출부가 따로 기록한다.
        if value is _MISSING:
            prop_errors.append(False)
            return False
        return _as_bool(value) is True

    if available("UIA_IsInvokePatternAvailablePropertyId"):
        patterns.append("Invoke")
    if available("UIA_IsValuePatternAvailablePropertyId"):
        # 값 패턴의 문자열은 본문일 수 있으므로 존재만 기록한다.
        patterns.append("Value")
    if available("UIA_IsTogglePatternAvailablePropertyId"):
        patterns.append("Toggle")
        try:
            result["toggle_state"] = int(wrapper.iface_toggle.CurrentToggleState)
        except Exception:
            prop_errors.append(True)
    if available("UIA_IsRangeValuePatternAvailablePropertyId"):
        patterns.append("RangeValue")
        try:
            pattern = wrapper.iface_range_value
            result["range"] = {
                "min": round(float(pattern.CurrentMinimum), 3),
                "max": round(float(pattern.CurrentMaximum), 3),
                "value": round(float(pattern.CurrentValue), 3),
            }
        except Exception:
            prop_errors.append(True)
    if available("UIA_IsExpandCollapsePatternAvailablePropertyId"):
        patterns.append("ExpandCollapse")
        try:
            result["expand_state"] = int(wrapper.iface_expand_collapse.CurrentExpandCollapseState)
        except Exception:
            prop_errors.append(True)
    if available("UIA_IsSelectionItemPatternAvailablePropertyId"):
        patterns.append("SelectionItem")
        try:
            result["selected"] = bool(wrapper.iface_selection_item.CurrentIsSelected)
        except Exception:
            prop_errors.append(True)
    if available("UIA_IsWindowPatternAvailablePropertyId"):
        patterns.append("Window")
        try:
            visual = int(wrapper.iface_window.CurrentWindowVisualState)
            result["window_visual_state"] = {0: "normal", 1: "maximized", 2: "minimized"}.get(
                visual, str(visual)
            )
        except Exception:
            prop_errors.append(True)
    if available("UIA_IsLegacyIAccessiblePatternAvailablePropertyId"):
        patterns.append("LegacyIAccessible")
        try:
            pattern_id = _prop_id("UIA_LegacyIAccessiblePatternId")
            unknown = element.GetCurrentPattern(pattern_id)
            legacy = unknown.QueryInterface(api.IUIAutomationLegacyIAccessiblePattern)
            result["legacy_role"] = int(legacy.CurrentRole)
            result["legacy_state"] = int(legacy.CurrentState)
        except Exception:
            prop_errors.append(True)
    patterns.sort()
    return result


def _read_current(element, attr: str):
    try:
        return True, getattr(element, attr)
    except Exception:
        return False, None


def _control_type_name(type_id: int) -> str:
    mapping = _UIA["iuia"].known_control_type_ids
    return mapping.get(int(type_id)) or f"#{int(type_id)}"


def _list_children(wrapper):
    """자식 요소를 읽는다. 조회 실패를 빈 목록으로 바꾸지 않는다."""
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    element = _raw_element(wrapper.element_info)
    if element is None:
        return [], True
    iuia = _UIA["iuia"]
    try:
        array = element.FindAll(iuia.tree_scope["children"], iuia.true_condition)
    except Exception:
        return [], True
    if array is None:
        return [], True
    try:
        length = int(array.Length)
    except Exception:
        return [], True
    children = []
    failed = False
    for index in range(length):
        try:
            children.append(UIAWrapper(UIAElementInfo(array.GetElement(index))))
        except Exception:
            failed = True
    return children, failed


def _read_element(wrapper, depth: int, path: str) -> dict:
    info = wrapper.element_info
    prop_errors: list[bool] = []
    element = _raw_element(info)
    name_unreadable = False
    if element is None:
        prop_errors.append(True)
        control_type = automation_id = class_name = framework_id = raw_name = ""
    else:
        ok, type_id = _read_current(element, "CurrentControlType")
        if not ok or type_id is None:
            prop_errors.append(True)
            control_type = ""
        else:
            control_type = _control_type_name(int(type_id))
        ok, automation_raw = _read_current(element, "CurrentAutomationId")
        if not ok:
            prop_errors.append(True)
            automation_id = ""
        else:
            automation_id = _as_text(automation_raw)
        ok, class_raw = _read_current(element, "CurrentClassName")
        if not ok:
            prop_errors.append(True)
            class_name = ""
        else:
            class_name = _as_text(class_raw)
        ok, framework_raw = _read_current(element, "CurrentFrameworkId")
        if not ok:
            prop_errors.append(True)
            framework_id = ""
        else:
            framework_id = _as_text(framework_raw)
        ok, name_raw = _read_current(element, "CurrentName")
        if not ok:
            prop_errors.append(True)
            name_unreadable = True
            raw_name = ""
        else:
            raw_name = _as_text(name_raw)
    password_raw = _property(element, _prop_id("UIA_IsPasswordPropertyId"))
    is_password = False if password_raw is _MISSING else _as_bool(password_raw) is True
    if name_unreadable:
        name, name_length, name_omitted = "", 0, True
    else:
        name, name_length, name_omitted = redact_label(
            control_type, raw_name, is_password=is_password
        )
    del raw_name
    enabled_raw = _property(element, _prop_id("UIA_IsEnabledPropertyId"))
    offscreen_raw = _property(element, _prop_id("UIA_IsOffscreenPropertyId"))
    enabled = None if enabled_raw is _MISSING else _as_bool(enabled_raw)
    offscreen = None if offscreen_raw is _MISSING else _as_bool(offscreen_raw)
    if enabled is None or offscreen is None:
        prop_errors.append(True)
    item_raw = _property(element, _prop_id("UIA_ItemStatusPropertyId"))
    if item_raw is _MISSING:
        item_status, item_status_omitted = "", False
    else:
        item_status, item_status_omitted = redact_short_field(_as_text(item_raw))
    aria_raw = _property(element, _prop_id("UIA_AriaRolePropertyId"))
    if aria_raw is _MISSING:
        aria_role, aria_omitted = "", False
    else:
        aria_role, aria_omitted = redact_short_field(_as_text(aria_raw))
    if aria_omitted:
        aria_role = ""
    runtime_id: list[int] = []
    if element is not None:
        try:
            runtime = element.GetRuntimeId()
            runtime_id = [int(part) for part in list(runtime)]
        except Exception:
            prop_errors.append(True)
    try:
        rect_info = info.rectangle
        rect = {
            "l": int(rect_info.left),
            "t": int(rect_info.top),
            "w": int(rect_info.right - rect_info.left),
            "h": int(rect_info.bottom - rect_info.top),
        }
    except Exception:
        rect = None
        prop_errors.append(True)

    pattern_info = _read_patterns(wrapper, element, prop_errors) if element is not None else {
        "patterns": [],
        "toggle_state": None,
        "range": None,
        "expand_state": None,
        "selected": None,
        "window_visual_state": "",
        "legacy_role": None,
        "legacy_state": None,
    }
    if element is None:
        prop_errors.append(True)
    return {
        "path": path,
        "depth": depth,
        "control_type": control_type,
        "automation_id": automation_id,
        "class_name": class_name,
        "framework_id": framework_id,
        "name": name,
        "name_length": name_length,
        "name_omitted": name_omitted,
        "name_unreadable": name_unreadable,
        "enabled": enabled,
        "offscreen": offscreen,
        "item_status": item_status,
        "item_status_omitted": item_status_omitted,
        "aria_role": aria_role,
        "is_password": is_password,
        "rect": rect,
        "runtime_id": runtime_id,
        "children_error": False,
        "prop_error": any(prop_errors),
        "pattern_probe_incomplete": any(flag is False for flag in prop_errors),
        **pattern_info,
    }


def capture_window(hwnd: int, *, max_depth: int, max_elements: int, label: str, note: str) -> dict:
    init_win32()
    started = time.monotonic()
    alive = bool(user32.IsWindow(hwnd))
    pid = _process_id(hwnd) if alive else 0
    exe = _process_exe(pid, {}) if alive else ""
    raw_title = _window_text(hwnd) if alive else ""
    title, title_length, title_omitted = redact_label("Window", raw_title)
    del raw_title
    class_name = _class_name(hwnd) if alive else ""
    foreground = int(user32.GetForegroundWindow() or 0)
    window = {
        "hwnd": hwnd,
        "hwnd_hex": f"0x{hwnd:08X}",
        "pid": pid,
        "process_exe": exe,
        "title": title,
        "title_length": title_length,
        "title_omitted": title_omitted,
        "class_name": class_name,
        "is_foreground": alive and hwnd == foreground,
        "is_iconic": bool(user32.IsIconic(hwnd)) if alive else False,
        "is_win32_visible": bool(user32.IsWindowVisible(hwnd)) if alive else False,
        "window_visual_state": "",
    }
    base = {
        "tool": TOOL_NAME,
        "schema": SCHEMA,
        "captured_at": now_iso(),
        "label": label,
        "note": note,
        "content_policy": "multiline_password_edit_document_and_overlong_names_omitted",
        "window": window,
        "elements": [],
        "read_error_count": 0,
        "elements_capped": False,
        "depth_limit_reached": False,
        "duration_ms": 0,
        "status": "failed",
        "failure": None,
    }
    if not alive:
        base["failure"] = "window_missing"
        base["duration_ms"] = int((time.monotonic() - started) * 1000)
        return base
    if not init_uia():
        base["failure"] = "uia_unavailable"
        base["duration_ms"] = int((time.monotonic() - started) * 1000)
        return base
    try:
        from pywinauto import Desktop

        wrapper = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception:
        base["failure"] = "uia_failed"
        base["duration_ms"] = int((time.monotonic() - started) * 1000)
        return base

    elements: list[dict] = []
    read_errors = 0
    depth_limit_reached = False
    elements_capped = False
    stack = [(wrapper, 0, "0")]
    while stack:
        if len(elements) >= max_elements:
            elements_capped = True
            break
        current, depth, path = stack.pop()
        try:
            element = _read_element(current, depth, path)
        except Exception:
            read_errors += 1
            element = sample_element(path=path, depth=depth, prop_error=True, children_error=True)
            element["enabled"] = None
            element["offscreen"] = None
        if element["prop_error"]:
            read_errors += 1
        elements.append(element)
        if len(elements) % 50 == 0:
            print(f"수집 중... {len(elements)}개", file=sys.stderr)
        if depth >= max_depth:
            children, children_failed = _list_children(current)
            if children_failed:
                element["children_error"] = True
                read_errors += 1
            elif children:
                depth_limit_reached = True
            continue
        children, children_failed = _list_children(current)
        if children_failed:
            element["children_error"] = True
            read_errors += 1
        for index in range(len(children) - 1, -1, -1):
            stack.append((children[index], depth + 1, f"{path}/{index}"))

    if elements:
        window["window_visual_state"] = elements[0].get("window_visual_state") or ""
    probe_incomplete = sum(1 for element in elements if element.get("pattern_probe_incomplete"))
    base["elements"] = elements
    base["read_error_count"] = read_errors
    base["pattern_probe_incomplete_count"] = probe_incomplete
    base["max_depth"] = max_depth
    base["elements_capped"] = elements_capped
    base["depth_limit_reached"] = depth_limit_reached
    if read_errors:
        base["status"] = "partial"
        base["failure"] = "read_error"
    elif probe_incomplete:
        base["status"] = "partial"
        base["failure"] = "pattern_probe_incomplete"
    elif elements_capped:
        base["status"] = "partial"
        base["failure"] = "element_cap"
    elif depth_limit_reached:
        base["status"] = "partial"
        base["failure"] = "depth_truncated"
    else:
        base["status"] = "ok"
        base["failure"] = None
    base["duration_ms"] = int((time.monotonic() - started) * 1000)
    print(f"수집 완료: {len(elements)}개, {base['duration_ms']}ms", file=sys.stderr)
    return base


def _note(text: str) -> str:
    stored, _length, omitted = redact_label("Button", text or "")
    return "" if omitted else stored


def cmd_list(args: argparse.Namespace) -> int:
    try:
        rows = list_windows(args.contains)
    except OSError as exc:
        print(f"창 목록 조회 실패: {type(exc).__name__}. 완료로 처리하지 않는다.")
        return 2
    print_window_list(rows)
    return 0


def cmd_tree(args: argparse.Namespace, *, require_save: bool) -> int:
    if require_save and (not args.out or not args.label):
        print("snapshot 명령은 --out 과 --label 이 필요하다.")
        return 2
    snapshot = capture_window(
        args.hwnd,
        max_depth=args.max_depth,
        max_elements=args.max_elements,
        label=args.label or "manual",
        note=_note(args.note or ""),
    )
    print_snapshot(snapshot)
    if args.out:
        write_snapshot(Path(args.out), snapshot)
        print(f"저장: {args.out}")
    if snapshot["status"] != "ok":
        return 2
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    try:
        before = load_snapshot(Path(args.before))
        after = load_snapshot(Path(args.after))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"스냅샷을 읽지 못했다: {type(exc).__name__}")
        return 2
    report = diff_snapshots(before, after)
    print_diff(report, args.before, args.after)
    if args.out:
        write_snapshot_report(Path(args.out), report)
        print(f"저장: {args.out}")
    if not report["comparable"]:
        return 2
    if report["window_changes"] or report["chrome_removed"] or report["chrome_added"] or report["visibility_changes"]:
        return 1
    return 0


def write_snapshot_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def cmd_watch(args: argparse.Namespace) -> int:
    print("탐색용 읽기 전용 감시. 소리를 재생하지 않고 창을 활성화하지 않는다.")
    print("전체 트리를 매 주기마다 출력하지 않고, 바뀐 요약만 출력한다.")
    print("조회 실패와 창 닫힘은 UNKNOWN이며 완료로 처리하지 않는다.")
    deadline = None if args.max_seconds is None else time.monotonic() + args.max_seconds
    previous_status = None
    previous_digest = None
    previous_snapshot = None
    saves = 0
    warned_slow = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("감시 제한 시간에 도달했다. 완료 알림을 보낸 것이 아니다.")
                return 0
            started = time.monotonic()
            snapshot = capture_window(
                args.hwnd,
                max_depth=args.max_depth,
                max_elements=args.max_elements,
                label="watch",
                note=_note(args.note or ""),
            )
            status = snapshot["status"]
            digest = chrome_digest(snapshot.get("elements") or []) if status == "ok" else None
            changed = bool(previous_digest and digest and digest != previous_digest)
            kind = classify_transition(previous_status, status, changed or previous_digest is None)
            if status != "ok":
                kind = "unknown"
            window = snapshot.get("window") or {}
            print(
                f"[{snapshot['captured_at']}] {kind} "
                f"status={status} signature={digest or '-'} "
                f"elements={len(snapshot.get('elements') or [])} "
                f"fg={_yn(window.get('is_foreground'))} "
                f"min={_yn(window.get('is_iconic'))} "
                f"vis={_yn(window.get('is_win32_visible'))} "
                f"{snapshot.get('duration_ms')}ms"
            )
            if kind == "unknown":
                print("  UNKNOWN. 창을 닫았거나 UI 조회에 실패했다. 완료로 처리하지 않는다.")
                if snapshot.get("failure"):
                    print(f"  사유: {snapshot['failure']}")
            elif kind == "resumed_after_unknown":
                print("  조회가 끊긴 뒤의 결과다. 완료 신호로 쓰지 않는다.")
            elif kind == "changed" and previous_snapshot is not None:
                report = diff_snapshots(previous_snapshot, snapshot)
                print_diff(report, "previous", "current")
            save_unknown = kind == "unknown" and previous_status not in {"failed", "partial"}
            save_this = kind in {"baseline", "changed", "resumed_after_unknown"} or save_unknown
            if args.save_on_change and save_this:
                if saves < args.max_saves:
                    destination = Path(args.save_on_change) / f"watch-{saves:03d}-{status}.json"
                    snapshot["label"] = f"watch-{saves:03d}"
                    write_snapshot(destination, snapshot)
                    print(f"  저장: {destination}")
                    saves += 1
                elif saves == args.max_saves:
                    print("  저장 상한에 도달했다. 이후에는 출력만 한다.")
                    saves += 1
            if status == "ok":
                previous_snapshot = snapshot
                previous_digest = digest
            previous_status = status
            remaining = args.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            elif not warned_slow:
                print("  조회가 폴링 간격보다 오래 걸린다. 간격을 늘리거나 깊이를 줄인다.")
                warned_slow = True
    except KeyboardInterrupt:
        print("사용자가 감시를 중단했다. 완료로 처리하지 않는다.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실행 중인 창의 UI Automation 트리를 읽기 전용으로 검사한다.",
        epilog=(
            "예시:\n"
            "  python inspect_ui.py list\n"
            "  python inspect_ui.py tree --hwnd 0x1234 --out snapshots/before.json --label before\n"
            "  python inspect_ui.py diff snapshots/before.json snapshots/after.json\n"
            "  python inspect_ui.py watch --hwnd 0x1234 --interval 1\n"
            "  python inspect_ui.py self-test"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="최상위 창 목록을 출력한다")
    list_parser.add_argument("--contains", help="프로세스 이름, 클래스, 짧은 제목에 포함된 문자열")
    list_parser.set_defaults(func=cmd_list)

    def add_capture_args(target: argparse.ArgumentParser, *, snapshot: bool) -> None:
        target.add_argument("--hwnd", type=parse_hwnd, required=True)
        target.add_argument("--max-depth", type=int, default=48)
        target.add_argument("--max-elements", type=int, default=2500)
        target.add_argument("--label", default="" if snapshot else "manual")
        target.add_argument("--note", default="")
        target.add_argument("--out")

    tree_parser = sub.add_parser("tree", help="선택한 창의 UIA 트리를 출력한다")
    add_capture_args(tree_parser, snapshot=False)
    tree_parser.set_defaults(func=lambda args: cmd_tree(args, require_save=False))

    snap_parser = sub.add_parser("snapshot", help="UIA 트리를 JSON으로 저장한다")
    add_capture_args(snap_parser, snapshot=True)
    snap_parser.set_defaults(func=lambda args: cmd_tree(args, require_save=True))

    diff_parser = sub.add_parser("diff", help="두 스냅샷의 UI 차이를 비교한다")
    diff_parser.add_argument("before")
    diff_parser.add_argument("after")
    diff_parser.add_argument("--out")
    diff_parser.set_defaults(func=cmd_diff)

    watch_parser = sub.add_parser("watch", help="트리를 다시 출력하지 않고 변화 요약만 감시한다")
    watch_parser.add_argument("--hwnd", type=parse_hwnd, required=True)
    watch_parser.add_argument("--interval", type=float, default=1.0)
    watch_parser.add_argument("--max-depth", type=int, default=48)
    watch_parser.add_argument("--max-elements", type=int, default=2500)
    watch_parser.add_argument("--note", default="")
    watch_parser.add_argument("--max-seconds", type=float)
    watch_parser.add_argument("--save-on-change")
    watch_parser.add_argument("--max-saves", type=int, default=50)
    watch_parser.set_defaults(func=cmd_watch)

    test_parser = sub.add_parser("self-test", help="비교 로직을 UI 없이 검증한다")
    test_parser.set_defaults(func=lambda args: self_test())
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 이상이 필요합니다.")
        return 2
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_depth", 1) < 0 or getattr(args, "max_elements", 1) < 1:
        print("깊이와 요소 수는 0보다 커야 합니다. 깊이는 0 이상이어야 합니다.")
        return 2
    if getattr(args, "interval", 1) < 0.2:
        print("폴링 간격은 0.2초 이상이어야 합니다.")
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
