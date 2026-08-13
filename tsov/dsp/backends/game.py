"""GAME 转录后端（openvpi/GAME，洛怜拍板 M4 新底座）。

- 端到端歌声→MIDI 专用模型（D3PM 生成式音符边界 + 浮点音高），优于 CREPE 的
  帧级音高 + 规则分割（M4 集成定案：GAME 输出音乐化音符边界，不走 quantize）。
- 实现：subprocess 调 `vendor/GAME/infer.py extract`（GAME 独立 venv 复用 tsov torch），
  `--output-formats mid,csv --pitch-format number` —— MIDI 给音符边界/力度（音高已取整），
  CSV 给浮点音高 → deviation_cents = (float - round(float)) * 100（SOME 系线性音分近似）。
- 失败明确报错（含子进程 stderr），不静默。
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ...core.notes import Note, Voice
from ..pitch import midi_to_hz
from . import TranscribeBackend

# 项目内 GAME 安装位置（vendor/ 不入库，换机需重新安装）
_ROOT = Path(__file__).resolve().parents[3]
_GAME_DIR = _ROOT / "vendor" / "GAME"
_GAME_INFER_PY = _GAME_DIR / "infer.py"
_GAME_VENV_PY = _GAME_DIR / ".venv" / "Scripts" / "python.exe"

# 默认参数（代码内默认值 + **params 覆盖，ADR-0005 决策 7）
DEFAULTS = {
    "model": None,          # None → 自动找 vendor/GAME/pretrained/*.pt
    "timeout": 900.0,       # 秒（GAME 模型加载 ~20s + 推理）
    "batch_size": 1,
    "ornament_ms": 150.0,   # 短于此标记装饰音
    "workdir": None,        # None → 自动临时目录（output/.game-tmp/<uuid>）
}


def _default_model() -> Path:
    if not _GAME_DIR.is_dir():
        raise RuntimeError(f"GAME 未安装：{_GAME_DIR}（需 clone openvpi/GAME + venv + pretrained）")
    pt_files = sorted((_GAME_DIR / "pretrained").glob("*.pt"))
    if not pt_files:
        raise RuntimeError(f"GAME 权重缺失：{_GAME_DIR / 'pretrained'} 下没有 .pt（v1.0.0 medium 三件套）")
    return pt_files[0]


class GameBackend(TranscribeBackend):
    name = "game"

    def transcribe(self, audio_path: str, **params) -> Voice:
        cfg = {**DEFAULTS, **params}
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f"音频不存在：{audio_path}")

        model = cfg["model"] or _default_model()
        model = Path(model)
        if not model.is_file():
            raise FileNotFoundError(f"GAME 模型不存在：{model}")

        midi_path, csv_path, workdir = self._run_game(audio_path, model, cfg)
        try:
            notes = self._parse_outputs(midi_path, csv_path, cfg)
        finally:
            if cfg["workdir"] is None:
                shutil.rmtree(workdir, ignore_errors=True)
        return self._build_voice(audio_path, notes)

    # ------------------------------------------------------------------
    # 子进程调用
    # ------------------------------------------------------------------

    def _run_game(self, audio_path: Path, model: Path, cfg: dict) -> tuple[Path, Path, Path]:
        if cfg["workdir"] is not None:
            workdir = Path(cfg["workdir"])
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            workdir = Path(tempfile.mkdtemp(prefix="game-", dir=self._default_work_root()))

        cmd = [
            str(_GAME_VENV_PY), str(_GAME_INFER_PY), "extract",
            str(audio_path), "-m", str(model),
            "--output-dir", str(workdir),
            "--output-formats", "mid,csv",
            "--pitch-format", "number",
            "--batch-size", str(int(cfg["batch_size"])),
        ]
        # Windows GBK 控制台 + rich 进度条冲突：强制 UTF-8（handoff 坑 33）
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=float(cfg["timeout"]),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"GAME 推理超时（>{cfg['timeout']}s）：{audio_path}") from e

        if proc.returncode != 0:
            raise RuntimeError(
                f"GAME 推理失败（rc={proc.returncode}）：{audio_path}\n"
                f"stderr:\n{(proc.stderr or '')[-3000:]}"
            )

        midis = sorted(workdir.glob("*.mid"))
        csvs = sorted(workdir.glob("*.csv"))
        if not midis:
            raise RuntimeError(f"GAME 未产出 MIDI：{workdir}\nstderr tail:\n{(proc.stderr or '')[-1500:]}")
        midi_path = midis[0]
        csv_path = csvs[0] if csvs else None
        return midi_path, csv_path, workdir

    def _default_work_root(self) -> Path:
        root = _ROOT / "output" / ".game-tmp"
        root.mkdir(parents=True, exist_ok=True)
        return root

    # ------------------------------------------------------------------
    # 解析：pretty_midi 音符边界 + CSV 浮点音高
    # ------------------------------------------------------------------

    def _parse_outputs(self, midi_path: Path, csv_path: Path | None, cfg: dict) -> list[Note]:
        import pretty_midi

        pm = pretty_midi.PrettyMIDI(str(midi_path))
        midi_notes = []
        for inst in pm.instruments:
            midi_notes.extend((float(n.start), float(n.end), float(n.pitch), int(n.velocity)) for n in inst.notes)
        midi_notes.sort(key=lambda n: n[0])

        csv_pitches: list[float] = []
        if csv_path is not None:
            with csv_path.open(encoding="utf8", newline="") as f:
                rows = list(csv.reader(f))[1:]  # 跳表头
            for row in rows:
                try:
                    csv_pitches.append(float(row[2]))
                except (ValueError, IndexError):
                    csv_pitches.append(0.0)

        ornament_s = float(cfg["ornament_ms"]) / 1000.0
        notes: list[Note] = []
        for i, (start, end, pitch_midi_f, velocity) in enumerate(midi_notes):
            pitch_f = csv_pitches[i] if i < len(csv_pitches) and csv_pitches[i] > 0 else pitch_midi_f
            pitch_int = int(round(pitch_f))
            # SOME 系线性音分近似：浮点音高距最近整音的差值 × 100
            dev = round((pitch_f - round(pitch_f)) * 100.0, 2)
            vel = max(0.0, min(1.0, velocity / 127.0))
            notes.append(
                Note(
                    start=start,
                    end=end,
                    pitch_midi=pitch_int,
                    pitch_hz=midi_to_hz(pitch_int),
                    velocity=vel,
                    confidence=vel,  # 无置信度信息，力度代偿（basic-pitch 同款）
                    deviation_cents=dev,
                    is_ornament=(end - start) < ornament_s,
                )
            )
        notes.sort(key=lambda n: n.start)
        return notes

    # ------------------------------------------------------------------
    # Voice 组装（复用 transcribe._build_voice，含乐句分割）
    # ------------------------------------------------------------------

    def _build_voice(self, audio_path: Path, notes: list[Note]) -> Voice:
        from ..transcribe import _build_voice

        return _build_voice(str(audio_path), self.name, notes)
