"""P62b-6（W11）：SSE 白名单一致性自动检查。

扫 `tsov/webapp/**/*.py` 的 `bus.publish(...)` 事件类型（字符串字面量 + 注释豁免的动态源）
⊆ `tsov/web/static/js/events.js` 的 `SSE_TYPES`——不一致即红（防「发布新事件但没登白名单
→ 前端静默收不到」再犯；chain_log/favorite 两处漂移即此坑实锤）。

- 正向（红条件）：字面量发布类型不在白名单。
- 动态源：第二参数非字面量者必须显式为已知豁免源（chain runner 的 `ev` 转发），否则红。
- 反向（仅打印警告）：白名单项在发布面找不到字面量来源（可能由动态源/链事件转发）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEBAPP = REPO / "tsov" / "webapp"
EVENTS_JS = REPO / "tsov" / "web" / "static" / "js" / "events.js"
RUNNER = REPO / "tsov" / "chain" / "runner.py"

# 动态发布源豁免：`bus.publish(<project>, ev, {...})`——chain runner 事件经 on_event 原样转发；
# 事件名在 tsov/chain/runner.py 的 _emit(...) 字面量中逐一在册（下方扫描自动收集）。
EXEMPT_DYNAMIC = {"ev"}


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO)).replace("\\", "/")


def scan_publishes() -> tuple[set[str], list[str]]:
    """返回（字面量事件集合，异常清单）。异常 = 未识别的动态源/无法解析的调用。"""
    literals: set[str] = set()
    problems: list[str] = []
    for f in sorted(WEBAPP.rglob("*.py")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"bus\.publish\(", text):
            line = text.count("\n", 0, m.start()) + 1
            ls = text.rfind("\n", 0, m.start()) + 1
            if "#" in text[ls:m.start()]:            # 行内注释里的示例调用
                continue
            chunk = text[m.end(): m.end() + 300]
            cm = re.match(r"\s*[^,]+,\s*(.+?)(?:,|\n|\)|\Z)", chunk, re.S)
            if not cm:
                problems.append(f"{_rel(f)}:{line} 无法解析 publish 参数")
                continue
            expr = cm.group(1).strip()
            lm = re.fullmatch(r"[\"']([a-z_][a-z0-9_]*)[\"']", expr)
            if lm:
                literals.add(lm.group(1))
            elif expr in EXEMPT_DYNAMIC:
                pass                                  # 已知动态源（见文件头豁免说明）
            else:
                problems.append(f"{_rel(f)}:{line} 未识别的动态发布源：{expr[:60]!r}")
    return literals, problems


def scan_chain_emits() -> set[str]:
    """chain runner 的 _emit 事件名（经 on_event lambda 转发进 bus）。"""
    if not RUNNER.is_file():
        return set()
    return set(re.findall(r'_emit\(\s*"([a-z_][a-z0-9_]*)"', RUNNER.read_text(encoding="utf-8")))


def whitelist() -> set[str]:
    text = EVENTS_JS.read_text(encoding="utf-8")
    m = re.search(r"const SSE_TYPES = \[([\s\S]*?)\];", text)
    assert m, "events.js 未找到 SSE_TYPES 数组"
    return set(re.findall(r"[\"']([a-z_][a-z0-9_]*)[\"']", m.group(1)))


def test_sse_publish_events_in_whitelist():
    """正向：发布面（字面量 + 链事件）必须 ⊆ SSE_TYPES。"""
    published, problems = scan_publishes()
    published |= scan_chain_emits()
    allowed = whitelist()
    missing = sorted(published - allowed)
    assert not missing, (
        "发布但未登 SSE 白名单（前端静默收不到）："
        + ", ".join(missing)
        + " → 登 events.js SSE_TYPES，或确认无需转发后收窄发布面"
    )
    asserts = sorted(problems)
    assert not asserts, "动态发布源未豁免/无法解析：\n" + "\n".join(asserts)


def test_sse_whitelist_reverse_report(capsys):
    """反向（仅警告）：白名单项在发布面无字面量来源 → 打印提示，不失败。"""
    published, _ = scan_publishes()
    published |= scan_chain_emits()
    orphans = sorted(whitelist() - published)
    if orphans:
        print(f"[warn] SSE_TYPES 无字面量发布来源（可能仅由动态源转发/预留）：{orphans}")
