"""建议 → 命令（M-V8 E4 段2 · Q4/Q9）：勾选批量 → 命令 dict；预览 = 副本上试跑。

- 数值再计算：volume 落盘时按**当前**值 × 建议差量（suggestion 存 delta_db）——
  建议生成后用户若手动改过音量，不覆盖其手感（差量语义）
- 效果：preset → 展开为既有 add_effect 序列（同批事务，原子）；type+params → 单 add_effect；
  既有效果改参 → set_effect_params（E4 段2 薄 op）
- 预览：命令在 EditBatch 副本上 apply（与落盘同一路径；工程零改动）

本模块只产命令；执行（apply_batch + 快照窗口）在 webapp 路由（ADR-0017 同径）。
"""

from __future__ import annotations

from ..host import EditBatch
from ..presets import load_library
from .suggest import _vol_after


def suggestion_commands(suggestion: dict, score, *, presets_root=None) -> list[dict]:
    """单条建议 → 命令 dict 列表（形状非法 → ValueError；数值域交命令层最终校验）。"""
    kind = str(suggestion.get("kind") or "")
    try:
        ti = int(suggestion.get("track"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"建议 track 非法：{suggestion.get('track')!r}") from e
    if not (0 <= ti < len(score.tracks)):
        raise ValueError(f"建议 track 越界：{ti}（共 {len(score.tracks)} 轨）")
    vals = suggestion.get("values") or {}

    if kind == "level":
        try:
            delta = float(vals.get("delta_db"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"建议 delta_db 非法：{vals.get('delta_db')!r}") from e
        cur = float(getattr(score.tracks[ti].instrument, "volume", 1.0) or 0.0)
        return [{"op": "set_track_mix", "track": ti, "value": {"volume": _vol_after(cur, delta)}}]

    if kind == "pan":
        try:
            p = float(vals.get("pan"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"建议 pan 非法：{vals.get('pan')!r}") from e
        if not (-1.0 <= p <= 1.0):
            raise ValueError(f"建议 pan 越界：{p}（-1~1）")
        return [{"op": "set_track_mix", "track": ti, "value": {"pan": round(p, 4)}}]

    if kind == "effect":
        eff = suggestion.get("effect") or {}
        if eff.get("preset"):
            lib = load_library(presets_root)
            try:
                preset = lib.get_effect(str(eff["preset"]))
            except KeyError as e:
                raise ValueError(str(e)) from e
            return [{"op": "add_effect", "track": ti,
                     "value": {"type": str(fx.type), "params": dict(fx.params or {})}}
                    for fx in preset.chain]
        e = eff.get("effect") or {}
        if e.get("type"):
            return [{"op": "add_effect", "track": ti,
                     "value": {"type": str(e["type"]), "params": dict(e.get("params") or {})}}]
        sp = eff.get("set_params") or {}
        if sp.get("index") is not None and isinstance(sp.get("params"), dict):
            return [{"op": "set_effect_params", "track": ti,
                     "value": {"index": int(sp["index"]), "params": dict(sp["params"])}}]
        raise ValueError(f"效果建议形状不支持：{eff!r}")

    raise ValueError(f"未知建议类型：{kind!r}（level/pan/effect）")


def selected_ids(batch: dict, ids=None) -> list[str]:
    """勾选集（缺省 = 全选）；批内未知 id 忽略、顺序按建议原序。"""
    known = [str(s.get("id")) for s in (batch.get("suggestions") or [])]
    if not ids:
        return known
    want = {str(x) for x in ids}
    return [i for i in known if i in want]


def batch_commands(batch: dict, score, *, ids=None, presets_root=None) -> tuple[list[dict], dict]:
    """选中建议 → 合并命令 + meta（{ids, by_kind, n}）。"""
    want = set(selected_ids(batch, ids))
    sels = [s for s in (batch.get("suggestions") or []) if str(s.get("id")) in want]
    commands: list[dict] = []
    by_kind: dict[str, int] = {}
    for s in sels:
        commands.extend(suggestion_commands(s, score, presets_root=presets_root))
        k = str(s.get("kind") or "")
        by_kind[k] = by_kind.get(k, 0) + 1
    return commands, {"ids": [str(s.get("id")) for s in sels], "by_kind": by_kind, "n": len(sels)}


def preview_score(score, suggestion: dict, *, presets_root=None):
    """单条建议在副本上试跑（命令层同径）→ 新 Score（工程零改动；全部被拒 → ValueError）。"""
    commands = suggestion_commands(suggestion, score, presets_root=presets_root)
    eb = EditBatch(label="调参预览")
    for c in commands:
        eb.add(str(c.get("op")), track=int(c.get("track", 0)), value=c.get("value"))
    new_score, res = eb.apply(score)
    if res.applied == 0 or res.errors:
        raise ValueError("；".join(res.errors) or "预览无生效命令")
    return new_score
