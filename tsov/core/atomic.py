"""原子写文件（P0 A2）：同目录临时件 + ``os.replace``。

用途：score.json / 快照 / 索引 / 状态档 / 批次 JSON 等一切「写一半就坏」的落盘。
- 临时件与目标同目录（跨盘 replace 会失败）
- fsync 保证掉电前数据已落盘（Windows 上对普通文件可用）
- 失败时清临时件，目标文件保持旧内容
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def _tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")


def write_bytes_atomic(path, data: bytes) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(p)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:   # 某些文件系统不支持 fsync；退化即可
                pass
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    return str(p)


def write_text_atomic(path, text: str, encoding: str = "utf-8") -> str:
    return write_bytes_atomic(path, str(text).encode(encoding))


def write_json_atomic(path, obj, *, indent: int = 1, ensure_ascii: bool = False,
                      sort_keys: bool = False) -> str:
    return write_text_atomic(
        path,
        json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys),
    )
