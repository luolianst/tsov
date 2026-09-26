"""配器模式库（M-V8 E4 段3 · Q14「三段一库」之④）：三包 <pack>.patterns.json 的装载/校验/查询。

模式库 = 角色 × 段型 × 变体 ID × 事件模板 + fills 库 + 强度档缩放；散文规格（各包
JSON 的 pattern_notes）留作 description/sources 溯源。本模块只做「装载+校验+查询」，
网格→音符的解析在 expand。

事件模板格式：[grid, len, vel, token]（16 分格制、1 起；grid/len 以格计、len ≥1）
- token 语法：
  - 纯数字（"36"）= 绝对 MIDI 音高（鼓组用）；
  - 相对 token：root / third / fifth / seventh / octave(=root+12) / chord / root5(根+五度两音)
    / arp:0..3（和弦音序号）；可加 ±12 后缀（"root+12" / "chord-12" / "arp:1+12"）；
  - 旋律跟随由变体字段 melody_offset（null / 0 / 12 / -12）表达——此类变体 events 为空。
校验红线（加载即查、报错带位置）：grid∈1..gpb、grid+len 不越小节、vel∈(0,1]、token 合法、
default 引用存在、fills 角色存在、档位键齐全。ADR-0017：本模块纯数据层，零宿主依赖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.names import note_name_to_midi

DEFAULT_PATTERN_DIR = Path(__file__).resolve().parents[2] / "presets" / "arrangements"

SECTION_KINDS = ("intro", "verse", "pre", "chorus", "bridge", "interlude", "outro", "full")

ENERGY_TIER_KEYS = ("1", "2", "3")
STRENGTH_KEYS = ("soft", "standard", "full")

_TOKEN_RE = re.compile(r"^(rest|root|third|fifth|seventh|octave|chord|root5|arp:[0-3])([+-]12)?$")


def token_ok(tok) -> tuple[bool, str]:
    """token 语法检查 → (ok, 错误说明)。"""
    s = str(tok)
    if s.isdigit():
        if 0 <= int(s) <= 127:
            return True, ""
        return False, f"绝对音高越界：{s}"
    m = _TOKEN_RE.match(s)
    if not m:
        return False, f"token 非法：{s!r}（可用 rest/root/third/fifth/seventh/octave/chord/root5/arp:0..3[±12] 或纯数字）"
    if m.group(1) == "rest" and m.group(2):
        return False, f"rest 不接受 ±12 后缀：{s!r}"
    return True, ""


def parse_token(tok) -> dict:
    """token → 结构：abs → {kind:'abs', pitch}；rest → {kind:'rest'}；相对 → {kind, arp_idx, offset}。"""
    s = str(tok)
    if s.isdigit():
        return {"kind": "abs", "pitch": int(s)}
    m = _TOKEN_RE.match(s)
    if not m:
        raise ValueError(f"token 非法：{s!r}")
    base = m.group(1)
    if base == "rest":
        return {"kind": "rest"}
    offset = int(m.group(2) or 0)
    idx = None
    if base.startswith("arp:"):
        idx = int(base.split(":", 1)[1])
        base = "arp"
    return {"kind": base, "arp_idx": idx, "offset": offset}


def check_event(e, gpb: int) -> list[str]:
    """单个事件 [grid, len, vel, token] 校验 → 问题清单（空 = 通过）。"""
    out: list[str] = []
    if not (isinstance(e, list) and len(e) == 4):
        return ["事件需 [grid, len, vel, token] 四元素数组"]
    grid, ln, vel, tok = e
    if not isinstance(grid, int) or not (1 <= grid <= gpb):
        out.append(f"grid 越界：{grid!r}（1..{gpb}）")
    if not isinstance(ln, int) or ln < 1:
        out.append(f"len 非法：{ln!r}")
    elif isinstance(grid, int) and 1 <= grid <= gpb and grid - 1 + ln > gpb:
        out.append(f"grid+len 越小节：{grid}+{ln}>{gpb}")
    if not isinstance(vel, (int, float)) or not (0.0 < float(vel) <= 1.0):
        out.append(f"vel 非法：{vel!r}（需 0<v≤1）")
    ok, msg = token_ok(tok)
    if not ok:
        out.append(msg)
    return out


def validate_patterns(data: dict, *, source: str = "") -> list[str]:
    """整包校验 → 问题清单（空 = 通过）。加载/测试/树侧核查共用。"""
    p: list[str] = []

    def err(msg: str) -> None:
        p.append(f"{source}: {msg}" if source else msg)

    if not isinstance(data, dict):
        return ["patterns 数据不是对象"]
    if not str(data.get("pack") or "").strip():
        err("缺 pack 名")
    meter = str(data.get("meter") or "")
    m = re.fullmatch(r"(\d+)/(\d+)", meter)
    gpb = data.get("grids_per_bar")
    if not m:
        err(f"meter 格式非法：{meter!r}（如 6/8、4/4）")
    else:
        num, den = int(m.group(1)), int(m.group(2))
        expect = max(1, round(num * 16 / den))
        if not isinstance(gpb, int) or gpb != expect:
            err(f"grids_per_bar 与拍号不一致：{gpb!r}（{meter} 应为 {expect}）")
    if not isinstance(gpb, int) or gpb < 1:
        gpb = int(gpb) if isinstance(gpb, int) else 0
    tiers = data.get("energy_tiers") or {}
    for k in ENERGY_TIER_KEYS:
        t = tiers.get(k)
        if not isinstance(t, dict) or not isinstance(t.get("vel_scale"), (int, float)) or t.get("vel_scale", 0) <= 0:
            err(f"energy_tiers[{k}] 缺或 vel_scale 非法")
    strengths = data.get("strengths") or {}
    for k in STRENGTH_KEYS:
        v = strengths.get(k)
        if not isinstance(v, (int, float)) or v <= 0:
            err(f"strengths[{k}] 缺或非法")
    crash = data.get("section_crash")
    if crash is not None:
        if not (isinstance(crash, dict) and isinstance(crash.get("pitch"), int) and 0 <= crash["pitch"] <= 127
                and isinstance(crash.get("vel"), (int, float)) and 0.0 < float(crash["vel"]) <= 1.0):
            err("section_crash 非法（需 {pitch:0..127, vel:(0,1]}）")
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles:
        err("roles 为空")
        roles = {}
    fills = data.get("fills") or {}
    for role, spec in roles.items():
        if not isinstance(spec, dict):
            err(f"role {role} 非对象")
            continue
        rng = spec.get("register")
        if rng is not None:
            lo = note_name_to_midi(rng[0]) if isinstance(rng, list) and len(rng) == 2 else None
            hi = note_name_to_midi(rng[1]) if isinstance(rng, list) and len(rng) == 2 else None
            if lo is None or hi is None or lo >= hi:
                err(f"{role}.register 非法：{rng!r}（需两个音名且 lo<hi）")
        vr = spec.get("velocity_range")
        if not (isinstance(vr, list) and len(vr) == 2 and all(isinstance(x, int) and 0 <= x <= 127 for x in vr)):
            err(f"{role}.velocity_range 非法：{vr!r}")
        variants = spec.get("variants")
        if not isinstance(variants, dict) or not variants:
            err(f"{role}.variants 为空")
            variants = {}
        for vid, var in variants.items():
            if not isinstance(var, dict):
                err(f"{role}.{vid} 非对象")
                continue
            if not str(var.get("desc") or "").strip():
                err(f"{role}.{vid} 缺 desc")
            if var.get("energy") not in (1, 2, 3):
                err(f"{role}.{vid}.energy 非法：{var.get('energy')!r}（需 1..3）")
            mo = var.get("melody_offset")
            if mo is not None and mo not in (0, 12, -12):
                err(f"{role}.{vid}.melody_offset 非法：{mo!r}（null/0/±12）")
            if mo is None:
                ev = var.get("events")
                if not isinstance(ev, list) or not ev:
                    err(f"{role}.{vid}.events 需非空数组（旋律跟随变体才可空）")
                    continue
                for k, e in enumerate(ev):
                    for x in check_event(e, gpb):
                        err(f"{role}.{vid}.events[{k}] {x}")
            elif var.get("events"):
                err(f"{role}.{vid}：旋律跟随变体不应带 events")
        defaults = spec.get("default") or {}
        for kind, vid in defaults.items():
            if kind not in SECTION_KINDS:
                err(f"{role}.default 段型非法：{kind!r}（可用 {'/'.join(SECTION_KINDS)}）")
            if vid not in variants:
                err(f"{role}.default[{kind}] 引用不存在变体：{vid!r}")
    for fid, f in fills.items():
        if not isinstance(f, dict):
            err(f"fill {fid} 非对象")
            continue
        if str(f.get("role") or "") not in roles:
            err(f"fill {fid} 角色不存在：{f.get('role')!r}")
        evs = f.get("events")
        if not isinstance(evs, list) or not evs:
            err(f"fill {fid}.events 需非空数组")
            continue
        for k, e in enumerate(evs):
            if not (isinstance(e, list) and len(e) == 5):
                err(f"fill {fid}.events[{k}] 需 [bar_offset, grid, len, vel, token] 五元素")
                continue
            bo = e[0]
            if not isinstance(bo, int) or bo < 0:
                err(f"fill {fid}.events[{k}] bar_offset 非法：{bo!r}")
            for x in check_event(e[1:], gpb):
                err(f"fill {fid}.events[{k}] {x}")
    return p


class PatternLibrary:
    """单包模式库（已校验）。查角色/变体/段型默认/fills/目录。"""

    def __init__(self, data: dict, *, path: Path | None = None):
        problems = validate_patterns(data, source=path.name if path else "")
        if problems:
            head = "；".join(problems[:10])
            tail = f"（共 {len(problems)} 条）" if len(problems) > 10 else ""
            raise ValueError(f"模式库校验失败：{head}{tail}")
        self.data = data
        self.path = path
        self.pack = str(data["pack"])
        self.meter = str(data["meter"])
        self.grids_per_bar = int(data["grids_per_bar"])
        self.roles: dict = data["roles"]
        self.fills: dict = data.get("fills") or {}
        self.energy_tiers: dict = data["energy_tiers"]
        self.strengths: dict = data["strengths"]
        self.section_crash: dict | None = data.get("section_crash")
        self.description = str(data.get("description") or "")
        self.sources = list(data.get("sources") or [])

    # ---- 装载 ----

    @classmethod
    def from_file(cls, path: str | Path) -> "PatternLibrary":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data, path=p)

    # ---- 查询 ----

    def role_ids(self) -> list[str]:
        return sorted(self.roles)

    def role(self, role: str) -> dict:
        if role not in self.roles:
            raise KeyError(f"角色不存在：{role!r}（可用：{', '.join(self.role_ids())}）")
        return self.roles[role]

    def variant(self, role: str, vid: str) -> dict:
        variants = self.role(role).get("variants") or {}
        if vid not in variants:
            raise KeyError(f"变体不存在：{role}.{vid!r}（可用：{', '.join(sorted(variants))}）")
        return variants[vid]

    def default_variant(self, role: str, kind: str) -> str | None:
        """段型默认变体：精确 → 'full' 兜底 → None。"""
        defaults = self.role(role).get("default") or {}
        if kind in defaults:
            return str(defaults[kind])
        if "full" in defaults:
            return str(defaults["full"])
        return None

    def fill(self, fid: str) -> dict:
        if fid not in self.fills:
            raise KeyError(f"fill 不存在：{fid!r}（可用：{', '.join(sorted(self.fills)) or '无'}）")
        return self.fills[fid]

    def catalog(self) -> dict:
        """紧凑目录（喂 decide 的 LLM 提示词/前端展示）：角色 → 变体清单。"""
        out: dict = {}
        for role in self.role_ids():
            spec = self.roles[role]
            items = []
            for vid, var in sorted((spec.get("variants") or {}).items()):
                items.append({
                    "id": vid,
                    "energy": var.get("energy"),
                    "melody_offset": var.get("melody_offset"),
                    "desc": str(var.get("desc"))[:100],
                })
            out[role] = {
                "title": spec.get("title"),
                "register": spec.get("register"),
                "pan": spec.get("pan"),
                "level_hint_db": spec.get("level_hint_db"),
                "variants": items,
                "default_by_section": spec.get("default") or {},
            }
        return out

    def fill_catalog(self) -> dict:
        return {fid: {"role": f.get("role"), "desc": str(f.get("desc"))[:100]}
                for fid, f in sorted(self.fills.items())}


def load_patterns(pack: str, *, root: str | Path | None = None) -> PatternLibrary:
    """按包名装载 <pack>.patterns.json（root 缺省 presets/arrangements/）。"""
    base = Path(root) if root is not None else DEFAULT_PATTERN_DIR
    path = base / f"{pack}.patterns.json"
    if not path.is_file():
        raise FileNotFoundError(f"找不到模式库：{path}（现有：{', '.join(available_packs(root=root)) or '无'}）")
    return PatternLibrary.from_file(path)


def available_packs(*, root: str | Path | None = None) -> list[str]:
    """有模式库的包名清单。"""
    base = Path(root) if root is not None else DEFAULT_PATTERN_DIR
    suffix = ".patterns.json"
    return sorted(p.name[: -len(suffix)] for p in base.glob(f"*{suffix}"))
