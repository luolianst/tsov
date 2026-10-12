"""子进程转录入口（U1：转录＝子进程形态、杀进程取消、崩溃隔离；批E 段2 E2-3）。

父进程：`tsov/chain/tools.py::_run_transcribe_spawn` 经 `_exec_child` 拉起本模块——
    python -m tsov.dsp.backends.spawn <backend> <audio> <out.json> [params_json]
产物：out.json ＝ Voice.to_dict()（**原子替换写**：`.tmp` → os.replace；父进程负责半成品清理）。
失败：异常上抛 → 非零退出 + traceback（父进程截尾报错，不静默）。
运行前提：PYTHONPATH 含仓库根（父进程 env_extra 注入）；本模块同 venv 直跑（重依赖惰性导入）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

USAGE = "用法：python -m tsov.dsp.backends.spawn <backend> <audio> <out.json> [params_json]"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(USAGE)
        return 2
    backend, audio, out = argv[0], argv[1], argv[2]
    params = json.loads(argv[3]) if len(argv) > 3 and argv[3].strip() else {}

    from tsov.dsp.backends import get_backend

    print(f"[spawn] backend={backend} audio={audio}")
    print(f"[spawn] params={params}")
    backend_obj = get_backend(backend)

    print("[spawn] 加载模型并推理中…")
    voice = backend_obj.transcribe(audio, **params)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(voice.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out_path)
    print(f"[spawn] OK notes={len(voice.notes)} → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
