"""轨道级 freeze 缓存 + 统一 stem 库（ADR-0018 / M-V7 D1）。

- **内容指纹** `track_key`：键 = 音源(backend/program) + 效果链参数 + 音符序列 + 采样率
  （**不含**推子/声像/自动化/速度/拍号/总线归属——这些走混音期实时层或属谱面属性）
- `StemStore`：工程内**内容寻址**存储（`<工程>/.stem-cache/<xx>/<key>.wav`，float32 WAV，无声损拼装）
- `StemStore.gc(keep)`：删除无引用 stem（保留集合由 HEAD/收藏版本的指纹计算）；顺带清理退役的 `.render-cache/`
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf

CACHE_VERSION = 2          # 手动口径版本（语义变更时 +1；自动失效见 CODE_FP）
DIRNAME = ".stem-cache"
LEGACY_DIRS = (".render-cache",)   # 退役目录（GC 时清理）


def _code_fingerprint() -> str:
    """渲染路径源码指纹（自动失效）：相关模块改任一字节 → 全部旧茎自然失效。

    教训（M-V7 D4）：只靠人工 CACHE_VERSION 会漏 bump——D1 迭代中间态的旧茎被
    静默命中（8.3e-3 级音差，见 `docs/tasks/m-v7/M-V7-验收.md` §5.3）。自动指纹把
    「记得 bump」从纪律变成机制。
    """
    h = hashlib.sha256()
    base = Path(__file__).parent
    for name in ("mix.py", "effect.py", "instrument.py", "engine.py", "cache.py"):
        p = base / name
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


CODE_FP = _code_fingerprint()   # 模块加载时计算一次


def track_key(track, samplerate: int) -> str:
    """轨道内容指纹（ADR-0018）：只含影响「音源+效果」输出的字段。"""
    inst = track.instrument
    payload = {
        "v": CACHE_VERSION,
        "code": CODE_FP,
        "sr": int(samplerate),
        "backend": str(inst.backend),
        "program": str(inst.program),
        "effects": [e.to_dict() if hasattr(e, "to_dict") else dict(e) for e in (inst.effects or [])],
        "notes": [
            [round(float(n.start), 6), round(float(n.end), 6), int(n.pitch_midi), round(float(n.velocity), 6)]
            for n in track.notes
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:24]


def score_keys(score, samplerate: int) -> set[str]:
    """一个 Score 的全部轨道指纹（= 该"版本"引用的 stem 集合；manifest 即从 git 现算，无需另存）。"""
    return {track_key(t, samplerate) for t in score.tracks}


class StemStore:
    """工程内内容寻址 stem 库（float32 WAV）。"""

    def __init__(self, project_root):
        self.root = Path(project_root)
        self.dir = self.root / DIRNAME

    # ---- 键 / 路径 ----
    def key_for(self, track, samplerate: int) -> str:
        return track_key(track, samplerate)

    def path_for(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.wav"

    def has(self, key: str) -> bool:
        return self.path_for(key).is_file()

    # ---- 读写 ----
    def load(self, key: str) -> np.ndarray | None:
        p = self.path_for(key)
        if not p.is_file():
            return None
        try:
            audio, _ = sf.read(str(p), dtype="float32", always_2d=True)
        except Exception:  # noqa: BLE001 —— 半截文件/损坏 → 视为未命中
            return None
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        return np.ascontiguousarray(audio, dtype=np.float32)

    def save(self, key: str, audio: np.ndarray, samplerate: int) -> Path:
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        sf.write(str(tmp), audio, int(samplerate), subtype="FLOAT", format="WAV")
        tmp.replace(p)   # 原子替换：并发读不会拿到半截文件
        return p

    # ---- 管理 ----
    def keys(self) -> set[str]:
        if not self.dir.is_dir():
            return set()
        return {f.stem for f in self.dir.glob("*/*.wav")}

    def size_bytes(self) -> int:
        if not self.dir.is_dir():
            return 0
        return sum(f.stat().st_size for f in self.dir.glob("*/*.wav"))

    def gc(self, keep: set[str], *, purge_legacy: bool = True) -> dict:
        """删除不被 keep 引用的 stem；顺带清理退役 `.render-cache/`。返回报告。"""
        removed: list[str] = []
        kept = 0
        for key in sorted(self.keys()):
            if key in keep:
                kept += 1
                continue
            try:
                self.path_for(key).unlink()
                removed.append(key)
            except OSError:
                pass
        if self.dir.is_dir():   # 清掉空的两字目录
            for sub in sorted(self.dir.glob("*")):
                if sub.is_dir() and not any(sub.iterdir()):
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
        legacy: list[str] = []
        if purge_legacy:
            for name in LEGACY_DIRS:
                d = self.root / name
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    legacy.append(name)
        return {"removed": removed, "kept": kept, "legacy_removed": legacy}
