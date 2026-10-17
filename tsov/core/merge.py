"""三方合并（P0 A6）：agent 轮中采用时保住用户手改。

基线 = 轮首预置（preseed）；当前 = 宿主谱（可能含用户轮中手改）；传入 = agent 结果。
规则（保守）：
- 只有一边改了 → 采用该边
- 两边改成一样 → 采用
- 两边对同一位置改成不同值 → 冲突（保留当前值，调用方放弃本次采用）
- 列表仅支持「同长度逐项合并」与「agent 追加尾部」；结构性增删/重排 → 冲突
"""

from __future__ import annotations

import copy

_MISSING = object()


def _merge(base, cur, inc, path: str, conflicts: list[str]):
    if cur == inc:
        return copy.deepcopy(cur)
    if cur == base:
        return copy.deepcopy(inc)
    if inc == base:
        return copy.deepcopy(cur)
    if isinstance(base, dict) and isinstance(cur, dict) and isinstance(inc, dict):
        out = copy.deepcopy(cur)
        for k in set(base) | set(cur) | set(inc):
            b = base.get(k, _MISSING)
            c = cur.get(k, _MISSING)
            i = inc.get(k, _MISSING)
            here = f"{path}.{k}"
            if c is _MISSING:
                if i is _MISSING:
                    continue
                if b is _MISSING:
                    out[k] = copy.deepcopy(i)          # 当前没有、传入新增
                elif i == b:
                    continue                            # 当前删除、传入未改 → 保持删除
                else:
                    conflicts.append(f"{here}（一侧删除一侧修改）")
                continue
            if i is _MISSING:
                if b is _MISSING:
                    continue                            # 传入没这个键（保留当前新增）
                if c == b:
                    out.pop(k, None)                    # 传入删除、当前未改 → 跟随删除
                else:
                    conflicts.append(f"{here}（一侧删除一侧修改）")
                continue
            if b is _MISSING:
                if c != i:
                    conflicts.append(here)              # 两边各加不同值
                continue
            out[k] = _merge(b, c, i, here, conflicts)
        return out
    if isinstance(base, list) and isinstance(cur, list) and isinstance(inc, list):
        # agent 追加：前缀逐项合并 + 尾部照收
        if len(base) == len(cur) and len(inc) >= len(base):
            merged = [
                _merge(base[j], cur[j], inc[j], f"{path}[{j}]", conflicts)
                for j in range(len(base))
            ]
            merged.extend(copy.deepcopy(inc[len(base):]))
            return merged
        # 同长度：逐项合并
        if len(base) == len(cur) == len(inc):
            return [
                _merge(b, c, i, f"{path}[{j}]", conflicts)
                for j, (b, c, i) in enumerate(zip(base, cur, inc))
            ]
        conflicts.append(f"{path}（结构增删/重排）")
        return copy.deepcopy(cur)
    conflicts.append(path)
    return copy.deepcopy(cur)


def three_way_merge(base: dict, current: dict, incoming: dict) -> tuple[dict, list[str]]:
    """三方合并；返回 (合并结果, 冲突路径清单)。冲突非空时调用方应放弃采用。"""
    conflicts: list[str] = []
    merged = _merge(base or {}, current or {}, incoming or {}, "$", conflicts)
    return merged if isinstance(merged, dict) else copy.deepcopy(current), conflicts
