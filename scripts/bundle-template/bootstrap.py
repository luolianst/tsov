"""tsov 一键开箱包 · 启动引导（stdlib-only；由包内 python-base 运行）。

职责：
1. 搬家修复（fixup）：把自带 venv 的绝对路径改写到「当前所在目录」——解压到任意路径、
   移动文件夹后都能自愈（每次启动幂等执行，几百毫秒）。
2. 自检（check）：venv / tsov 包 / torch（含 GAME 接线）/ 引擎文件 / ffmpeg 全检。
3. 启动（默认）：选可用端口 → 起 web 服务 → 健康检查通过 → 自动打开浏览器 → 守候进程。

用法（一般由同目录「启动tsov.bat」调用，无需手敲）：
    python-base\\python.exe bootstrap.py            # 一键启动
    python-base\\python.exe bootstrap.py check      # 环境自检
    python-base\\python.exe bootstrap.py --port 9000
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / "tsov" / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
BASE_PY = ROOT / "python-base" / "python.exe"
GAME_VENV = ROOT / "vendor" / "GAME" / ".venv"
GAME_PY = GAME_VENV / "Scripts" / "python.exe"
FFMPEG = ROOT / "ffmpeg" / "ffmpeg.exe"


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _write_if_changed(path: Path, text: str) -> None:
    """只在内容变化时写（幂等；避免每次启动无谓落盘）。"""
    try:
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
            return
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"[警告] 路径修复写入失败：{path} —— {e}")


def _rewrite_home(cfg: Path, home: str, drop: tuple = ("executable", "command")) -> None:
    """重写 pyvenv.cfg 的 home 行（其余键保留）；缺省顺带清掉 executable/command 注释行（含原装机路径）。"""
    if not cfg.is_file():
        return
    dropl = tuple(d.lower() for d in drop)
    lines = [ln for ln in cfg.read_text(encoding="utf-8", errors="replace").splitlines()
             if not ln.strip().lower().startswith("home")
             and not ln.strip().lower().startswith(dropl)]
    _write_if_changed(cfg, "\n".join([f"home = {home}"] + lines) + "\n")


def _ensure_standard_python(venv_dir: Path) -> None:
    """把 uv trampoline 版 python.exe/pythonw.exe 换成 python-base 自带的标准 venv 启动器。

    uv 造的启动器嵌死装机绝对路径——换台机器后报
    「uv trampoline failed to spawn Python child process / entity not found」。
    标准启动器读 pyvenv.cfg 定位基座，可随包任意路径搬家（幂等：已是标准件即跳过）。
    """
    stub_dir = ROOT / "python-base" / "Lib" / "venv" / "scripts" / "nt"
    scripts = venv_dir / "Scripts"
    for name in ("python.exe", "pythonw.exe"):
        target, stub = scripts / name, stub_dir / name
        if not (target.is_file() and stub.is_file()):
            continue
        try:
            if b"uv trampoline" not in target.read_bytes()[:65536]:
                continue
            shutil.copy2(stub, target)
            print(f"[修复] 已换标准 Python 启动器：{target}")
        except OSError as e:
            print(f"[警告] 启动器替换失败：{target} —— {e}")


def fixup() -> None:
    """搬家修复：所有写死的绝对路径 → 当前目录（幂等）。"""
    base = str(ROOT / "python-base")
    # 主 venv：基座指向 + tsov 包定位（editable .pth → 本包根）
    _rewrite_home(VENV / "pyvenv.cfg", base)
    _write_if_changed(VENV / "Lib" / "site-packages" / "_editable_impl_tsov.pth", str(ROOT) + "\n")
    for du in (VENV / "Lib" / "site-packages").glob("tsov-*.dist-info/direct_url.json"):
        try:
            du.unlink()   # 纯元数据，含开发机绝对路径，删掉更干净
        except OSError:
            pass
    _ensure_standard_python(VENV)
    # GAME venv：基座指向 + 接线（让它能看到主 venv 的 torch 等）
    _rewrite_home(GAME_VENV / "pyvenv.cfg", base)
    _write_if_changed(GAME_VENV / "Lib" / "site-packages" / "tsovvenv.pth",
                      str(VENV / "Lib" / "site-packages") + "\n")
    _ensure_standard_python(GAME_VENV)


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=str(ROOT))


def check() -> int:
    """环境自检（打包验收 / 用户排障用）。"""
    print("========== tsov 一键包 · 环境自检 ==========")
    print(f"包位置：{ROOT}")
    ok = True

    def mark(good: bool, label: str, extra: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'OK' if good else '缺失/失败'}] {label}{(' — ' + extra) if extra else ''}")

    mark(BASE_PY.is_file(), "python-base（自带的 Python 运行时）")
    mark(VENV_PY.is_file(), "tsov 运行环境（tsov/.venv）")
    mark(GAME_PY.is_file(), "GAME 运行环境（vendor/GAME/.venv）")

    if VENV_PY.is_file():
        r = _run([str(VENV_PY), "-c", "import tsov; print(tsov.__file__)"])
        loc = (r.stdout or "").strip()
        in_root = bool(loc) and loc.lower().startswith(str(ROOT).lower())
        mark(r.returncode == 0 and in_root, "tsov 包导入", (loc or r.stderr.strip())[:200])
        r = _run([str(VENV_PY), "-c", "import torch; print(torch.__version__, '| CUDA:', torch.cuda.is_available())"], timeout=300)
        mark(r.returncode == 0, "torch（转录/渲染依赖）", (r.stdout.strip() or r.stderr.strip())[:200])
    if GAME_PY.is_file():
        r = _run([str(GAME_PY), "-c", "import torch; print('game → torch', torch.__version__)"], timeout=300)
        mark(r.returncode == 0, "GAME venv 接线（共用主环境 torch）", (r.stdout.strip() or r.stderr.strip())[:200])

    engine_files = [
        ("FluidSynth DLL", ROOT / "vendor" / "fluidsynth" / "bin" / "libfluidsynth-3.dll"),
        ("GM 音色库", ROOT / "vendor" / "soundfonts" / "FluidR3_GM.sf2"),
        ("RMVPE 模型", ROOT / "vendor" / "RMVPE" / "rmvpe.pt"),
    ]
    for label, p in engine_files:
        mark(p.is_file(), label, f"{p.stat().st_size:,}B" if p.is_file() else str(p))
    pts = sorted((ROOT / "vendor" / "GAME" / "pretrained").glob("*.pt")) if (ROOT / "vendor" / "GAME" / "pretrained").is_dir() else []
    mark(len(pts) > 0, "GAME 权重（pretrained/*.pt）", f"{len(pts)} 个文件")
    mark(FFMPEG.is_file(), "ffmpeg（音频处理）")

    print("=" * 46)
    print("自检结果：" + ("全部通过 ✓ 可以开跑" if ok else "有缺项（见上）——请检查是否完整解压"))
    return 0 if ok else 1


def _pick_port(start: int = 8790, tries: int = 12) -> int:
    """选一个可用端口（默认从 8790 起）。"""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _wait_health(url: str, timeout_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 —— 服务还没起来，继续等
            time.sleep(0.5)
    return False


def main(argv: list[str]) -> int:
    _utf8_console()
    print("============================================")
    print("  tsov 一键开箱包 —— 哼唱 → 乐谱 → 成品的开源工作台")
    print("============================================")
    if argv and argv[0] == "check":
        fixup()
        return check()
    fixup()
    if not VENV_PY.is_file():
        print("[错误] 找不到 tsov 运行环境 —— 请确认整个文件夹已完整解压（不要只把单个文件拖出来），")
        print("       然后双击「启动tsov.bat」再试。")
        return 2
    r = _run([str(VENV_PY), "-c", "import tsov"], timeout=120)
    if r.returncode != 0:
        print("[错误] 运行环境校验失败。请先运行自检：")
        boot_path = ROOT / "bootstrap.py"
        print(f'  "{BASE_PY}" "{boot_path}" check')
        print((r.stderr or "")[-600:])
        return 2
    port = _pick_port()
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    env = dict(os.environ)
    env["PATH"] = str(FFMPEG.parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    url = f"http://127.0.0.1:{port}"
    print(f"正在启动服务…（首次约 10～30 秒；日志见下方）")
    print(f"服务地址：{url}")
    print("-" * 46)
    proc = subprocess.Popen([str(VENV_PY), "-m", "tsov.cli", "web", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT), env=env)
    ready = _wait_health(url, 90)
    if ready:
        print("-" * 46)
        print(f"服务已就绪 ✓  已在浏览器打开：{url}")
        print("（没自动打开的话，把上面地址复制到浏览器即可）")
        print("对话前先在 ⚙ 设置 → 对话 / LLM 里填一个 API Key（见使用说明）。")
        print("关闭本窗口 = 退出 tsov。")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    else:
        print("[警告] 启动超时——请看下方服务日志排查（或运行 bootstrap.py check）。")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
