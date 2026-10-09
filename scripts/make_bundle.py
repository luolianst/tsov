"""打包「tsov 一键开箱包」（Windows / 网盘分发版）——stdlib-only。

产物结构（<name> = tsov-v<version>-win64）：
  <name>/
    启动tsov.bat          双击入口（scripts/bundle-template/start-tsov.bat）
    bootstrap.py          启动引导（搬家修复/自检/起服务；每次启动幂等自愈）
    使用说明.txt
    python-base/          CPython 运行时（来源 = tsov/.venv 的 pyvenv.cfg home 所指）
    tsov/                 程序本体（源码 + tsov/.venv 全依赖）
    vendor/               引擎五件套（FluidSynth / 音色库 / RMVPE / GAME / VST3）
    scripts/ docs/ README* LICENSE pyproject.toml .env.example
    ffmpeg/ffmpeg.exe     音频处理

用法：
  python scripts/make_bundle.py                    # 全流程：stage → 修复 → 自检 → zip 到桌面
  python scripts/make_bundle.py --skip-zip         # 只出 stage 目录（验收/调试用）
  python scripts/make_bundle.py --out <zip 路径>   # 自定义产物路径
  python scripts/make_bundle.py --keep-stage       # 保留 stage（默认保留，验收后手动清）

要点：
- 白名单复制（根级只带 README/LICENSE/pyproject/.env.example 等），**绝不带 .env**；
- **docs/ 只复制 git 跟踪（公开面）文件**——工作区里隐身留存的内部过程档案绝不进包；
- 排除 output/ knowledge/ tests/ handoff.md AGENTS.md .git，及 __pycache__（venv 内部保留 pyc）；
- 打包前门禁：stage 扫非公开面文件（零容忍）+ 包内 bootstrap.py check（不过则中止）；
- zip 用 Windows 自带 bsdtar（可打 zip64）；缺失则退回 zipfile。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "scripts" / "bundle-template"
DEFAULT_STAGE = Path(os.environ.get("LOCALAPPDATA", "C:/")) / "hermes" / "cache" / "scratch" / "tsov-bundle"
DESKTOP = Path(os.environ.get("USERPROFILE", "C:/")) / "Desktop"

# 根级白名单（绝不把 .env / output / knowledge / skills / samples / .git 带进去）
ROOT_DIRS = ["tsov", "vendor", "scripts", "docs", "presets"]
ROOT_FILES = ["README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md", "pyproject.toml", ".env.example"]
SKIP_DIRNAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".github", "knowledge", "tests"}
SKIP_FILENAMES = {".env", ".env.local", ".env.production"}


def read_version() -> str:
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("无法从 pyproject.toml 读取版本号")


def _ignore(dirpath: str, names: list[str]) -> set[str]:
    """复制过滤：排除缓存/垃圾；venv 内部保留 pyc（启动更快）。"""
    in_venv = ".venv" in dirpath.replace("/", os.sep)
    out: set[str] = set()
    for n in names:
        if n in SKIP_FILENAMES:
            out.add(n)
        elif not in_venv and (n in SKIP_DIRNAMES or n.endswith((".pyc", ".pyo"))):
            out.add(n)
    return out


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_ignore, symlinks=False, dirs_exist_ok=False)


def git_tracked_files() -> set[str]:
    """公开面 = git 跟踪的文件集（仓库相对路径；-z 防 CJK 八进制转义）。"""
    r = subprocess.run(["git", "-C", str(REPO), "-c", "core.quotePath=false", "ls-files", "-z"],
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git ls-files 失败——无法确定公开面，中止打包")
    return {p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def copy_docs_public(dst: Path, tracked: set[str]) -> int:
    """docs/ 只复制公开面（git 跟踪）文件到 dst（相对路径原样保留）；返回复制件数。"""
    n = 0
    for rel in sorted(x for x in tracked if x.startswith("docs/")):
        src = REPO / rel
        if not src.is_file():
            print(f"      ⚠ 跟踪文件缺失，跳过：{rel}")
            continue
        t = dst / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, t)
        n += 1
    return n


def gate_stage(name_root: Path, tracked: set[str]) -> None:
    """门禁：stage 内（除自带件/运行时外）不得出现非公开面（git 跟踪）文件。"""
    allowed_extra = {"bootstrap.py", "启动tsov.bat", "使用说明.txt"}
    skip_heads = {"python-base", "vendor", "ffmpeg"}
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(name_root):
        rel_dir = os.path.relpath(dirpath, name_root).replace("\\", "/")
        if rel_dir != "." and rel_dir.split("/", 1)[0] in skip_heads:
            dirnames[:] = []
            continue
        dirnames[:] = [x for x in dirnames if x != ".venv"]
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir != "." else fn
            if rel in allowed_extra:
                continue
            if rel not in tracked:
                bad.append(rel)
    if bad:
        print(f"[gate] ✗ stage 含 {len(bad)} 个非公开面文件：")
        for b in sorted(bad)[:40]:
            print("      -", b)
        if len(bad) > 40:
            print(f"      … 共 {len(bad)} 个")
        raise SystemExit("[gate] 中止打包——非公开内容不得进分发包")
    print("[gate] ✓ 非公开面零残留")


def locate_base_python() -> Path:
    """从 tsov/.venv/pyvenv.cfg 的 home 读基座 python 目录（真目录）。"""
    cfg = REPO / "tsov" / ".venv" / "pyvenv.cfg"
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("home"):
            p = Path(line.split("=", 1)[1].strip())
            if p.is_dir():
                return p.resolve()
    raise SystemExit(f"找不到基座 Python（读 {cfg} 的 home 行失败）")


def locate_ffmpeg() -> Path:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("PATH 里找不到 ffmpeg（打包需要它）")
    return Path(exe)


def run_check(name_root: Path) -> None:
    """用包内 python-base 跑 bootstrap.py check（不过则中止）。"""
    base_py = name_root / "python-base" / "python.exe"
    boot = name_root / "bootstrap.py"
    r = subprocess.run([str(base_py), str(boot), "check"], text=True, encoding="utf-8",
                       errors="replace", cwd=str(name_root))
    if r.returncode != 0:
        raise SystemExit("包内自检未通过 —— 中止打包（先修问题再重跑）")


def gate_bat(name_root: Path) -> None:
    """启动器字节门禁：批处理必须全 CRLF —— LF-only 会被 cmd 绞碎逐段当命令执行。"""
    data = (name_root / "启动tsov.bat").read_bytes()
    crlf = data.count(b"\r\n")
    lone_lf = data.count(b"\n") - crlf
    if crlf == 0 or lone_lf > 0:
        raise SystemExit(f"启动器行尾非法：CRLF={crlf} 孤LF={lone_lf}（必须全 CRLF）—— 中止打包")
    print(f"      ✓ 启动器行尾 CRLF×{crlf}")


def gate_no_trampoline(name_root: Path) -> None:
    """门禁：venv 的 python 启动器不得是 uv trampoline（嵌死装机路径，换机拉不起子进程）。"""
    must = [("tsov/.venv/Scripts/python.exe", True), ("tsov/.venv/Scripts/pythonw.exe", True),
            ("vendor/GAME/.venv/Scripts/python.exe", True), ("vendor/GAME/.venv/Scripts/pythonw.exe", True),
            ("python-base/python.exe", True), ("python-base/pythonw.exe", False)]
    bad = []
    for rel, required in must:
        p = name_root / rel
        if not p.is_file():
            if required:
                bad.append(f"缺 {rel}")
            continue
        head = p.read_bytes()[:65536]
        if b"uv trampoline" in head or b"LuoLian" in head:
            bad.append(rel)
    if bad:
        raise SystemExit("启动器门禁未过（uv trampoline / 装机路径残留）：" + "；".join(bad))
    # 信息级：其余脚本 exe 里仍嵌装机路径的数量（已知存量：uv 脚本 trampoline，不在运行链上）
    n = 0
    for d in ("tsov/.venv/Scripts", "vendor/GAME/.venv/Scripts"):
        for p in (name_root / d).glob("*.exe"):
            try:
                if b"LuoLian" in p.read_bytes():
                    n += 1
            except OSError:
                pass
    if n:
        print(f"      ℹ 另有 {n} 个脚本 exe 嵌装机路径（存量·不在运行链，未处理）")
    print("      ✓ 启动器无 uv trampoline / 装机路径")


def run_bat_smoke(name_root: Path) -> None:
    """真跑一遍「启动tsov.bat check」—— 门禁必须覆盖 bat 本体的 cmd 解析层。"""
    r = subprocess.run(["cmd.exe", "/c", "启动tsov.bat", "check"],
                       capture_output=True, stdin=subprocess.DEVNULL,
                       cwd=str(name_root), timeout=600, creationflags=0x08000000)
    raw = r.stdout + r.stderr
    if ("不是内部或外部命令".encode("gbk") in raw or
            "不是内部或外部命令".encode("utf-8") in raw):
        raise SystemExit("启动器冒烟：cmd 解析碎裂（出现「不是内部或外部命令」）—— 中止打包")
    if not ("全部通过".encode("utf-8") in raw or "全部通过".encode("gbk") in raw):
        raise SystemExit("启动器冒烟：未见自检通过标记 —— 中止打包\n"
                         + raw.decode("utf-8", errors="replace")[-2000:])
    print("      ✓ 启动器本体冒烟（cmd 解析 + 自检标记）")


def verify_zip_bat(out: Path, folder: str) -> None:
    """zip 级静态预验：包内启动器必须全 CRLF（打完 zip 再对一次账）。"""
    with zipfile.ZipFile(out) as zf:
        entry = None
        for i in zf.infolist():
            n = i.filename
            try:
                n = n.encode("cp437").decode("gbk")  # bsdtar 中文名无 UTF-8 标志
            except Exception:
                pass
            if n == f"{folder}/启动tsov.bat":
                entry = i
                break
        if entry is None:
            raise SystemExit(f"zip 预验：包内找不到 {folder}/启动tsov.bat —— 中止交付")
        data = zf.read(entry)
    crlf = data.count(b"\r\n")
    if crlf == 0 or data.count(b"\n") - crlf > 0:
        raise SystemExit("zip 预验：包内启动器行尾非法 —— 中止交付")
    print(f"      ✓ zip 预验：启动器 CRLF×{crlf} 在包")


def make_zip(stage: Path, folder: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    bsdtar = Path("C:/WINDOWS/system32/tar.exe")
    if bsdtar.is_file():
        print(f"[zip] bsdtar → {out}")
        r = subprocess.run([str(bsdtar), "-a", "-c", "-f", str(out), "-C", str(stage), folder])
        if r.returncode == 0 and out.is_file():
            return
        print("[zip] bsdtar 失败，退回 zipfile …")
    print(f"[zip] zipfile(压缩级 1) → {out}（较慢，请耐心）")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        root = stage / folder
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                zf.write(p, p.relative_to(stage).as_posix())


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 tsov 一键开箱包")
    ap.add_argument("--out", default=None, help="zip 产物路径（默认：桌面）")
    ap.add_argument("--stage", default=str(DEFAULT_STAGE), help="stage 目录")
    ap.add_argument("--skip-zip", action="store_true", help="只出 stage，不压 zip")
    ap.add_argument("--keep-stage", action="store_true", help="保留旧 stage（默认重建）")
    args = ap.parse_args()

    version = read_version()
    folder = f"tsov-v{version}-win64"
    stage = Path(args.stage)
    name_root = stage / folder
    out = Path(args.out) if args.out else DESKTOP / f"tsov-v{version}-一键开箱包-win64.zip"

    print(f"[1/6] 版本 {version} · 产物 {folder}")
    if name_root.exists():
        if not args.keep_stage:
            print(f"[2/6] 清理旧 stage：{name_root}")
            shutil.rmtree(name_root)
        else:
            raise SystemExit(f"stage 已存在（--keep-stage）：{name_root}")
    else:
        print("[2/6] stage 全新")
    stage.mkdir(parents=True, exist_ok=True)

    print("[3/6] 复制程序本体 / 引擎 / 文档 …（几 GB，约 2–6 分钟）")
    tracked = git_tracked_files()
    for d in ROOT_DIRS:
        src = REPO / d
        if not src.is_dir():
            continue
        if d == "docs":
            n = copy_docs_public(name_root, tracked)
            print(f"      ✓ docs（公开面 {n} 件）")
        else:
            copy_tree(src, name_root / d)
            print(f"      ✓ {d}")
    for f in ROOT_FILES:
        src = REPO / f
        if src.is_file():
            shutil.copy2(src, name_root / f)
    # 模板三件（改名进包）
    shutil.copy2(TEMPLATE / "bootstrap.py", name_root / "bootstrap.py")
    shutil.copy2(TEMPLATE / "start-tsov.bat", name_root / "启动tsov.bat")
    (name_root / "使用说明.txt").write_text(
        (TEMPLATE / "readme-cn.txt").read_text(encoding="utf-8").replace("{VERSION}", version),
        encoding="utf-8")
    print("      ✓ 启动器 / 使用说明 / bootstrap")

    print("[4/6] 自带运行时：python-base + ffmpeg …")
    base_src = locate_base_python()
    copy_tree(base_src, name_root / "python-base")
    print(f"      ✓ python-base ← {base_src}")
    ff_src = locate_ffmpeg()
    (name_root / "ffmpeg").mkdir(exist_ok=True)
    shutil.copy2(ff_src, name_root / "ffmpeg" / "ffmpeg.exe")
    print(f"      ✓ ffmpeg ← {ff_src}（{ff_src.stat().st_size:,}B）")

    print("[5/6] 搬家修复 + 门禁 + 包内自检 …")
    sys.path.insert(0, str(name_root))
    import bootstrap  # noqa: E402 —— 用包内模板做同款修复（bootstrap.ROOT 已由 __file__ 决定）
    bootstrap.fixup()
    gate_stage(name_root, tracked)
    gate_bat(name_root)
    gate_no_trampoline(name_root)
    run_check(name_root)
    run_bat_smoke(name_root)
    print("[5/6] 自检通过 ✓")

    if args.skip_zip:
        print(f"[6/6] --skip-zip：完成。stage = {name_root}")
        return 0
    print("[6/6] 压缩 zip（GB 级，约 5–20 分钟）…")
    t0 = datetime.now()
    make_zip(stage, folder, out)
    verify_zip_bat(out, folder)
    dt = (datetime.now() - t0).total_seconds()
    print(f"[6/6] 完成 ✓ {out}（{out.stat().st_size:,}B，{dt:.0f}s）")
    print(f"      （zip 内含顶层目录 {folder}/；解压后双击「启动tsov.bat」即可）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
