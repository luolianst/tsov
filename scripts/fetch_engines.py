#!/usr/bin/env python3
"""tsov — fetch the optional engines (not bundled in the repository).

One script for every optional third-party component:

  fluidsynth  FluidSynth 2.x DLLs             -> vendor/fluidsynth/bin/       (Windows)
  soundfont   FluidR3_GM.sf2 (~148 MB)        -> vendor/soundfonts/
  rmvpe       Dream-High/RMVPE + weights      -> vendor/RMVPE/                (humming)
  game        openvpi/GAME + venv + weights   -> vendor/GAME/                 (vocals)
  vst3        test plugins (Dexed synth       -> vendor/vst3/                 (VST3 demo)
              + TAL-Chorus-LX chorus)

Usage (run from the repository root):

  python scripts/fetch_engines.py                    # fetch everything + verify
  python scripts/fetch_engines.py --only rmvpe vst3  # subset:
                                                     #   fluidsynth soundfont rmvpe game vst3
  python scripts/fetch_engines.py --check            # probe download sources only, no writes
  python scripts/fetch_engines.py --no-verify        # skip the functional checks at the end
  python scripts/fetch_engines.py --dest vendor      # custom target dir (default: ./vendor)

China mirrors (or just let it auto-fallback):
  --gh-mirror https://ghfast.top          # for GitHub release downloads / clones
  --hf-mirror https://hf-mirror.com       # for rmvpe.pt

Requires: git + Python 3.9+ (stdlib only). GAME setup additionally needs `uv`
and the tsov venv (tsov/.venv — see the README quickstart). After fetching, run
`tsov backends` and the smoke test in docs/setup-engines.md to verify.

GAME's venv is wired to REUSE the tsov venv (single source of truth for
torch/numpy/scipy and friends): a `tsovvenv.pth` points at the tsov
site-packages, and the duplicated shared packages are pruned (see
SHARED_PRUNE) so GAME never shadows them with incompatible copies.

Idempotent: existing files are kept (use --force to re-fetch).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO / "vendor"
UA = {"User-Agent": "tsov-fetch-engines/1.1"}

# Tested pins (see docs/setup-engines.md)
GAME_COMMIT = "4ad815c90dfe2442730f3fdc866fd23e737cbc97"
RMVPE_COMMIT = "a6db1cd7d26014aa739383367afd9bab57fc624c"
DEFAULT_STEPS = ["fluidsynth", "soundfont", "rmvpe", "game", "vst3"]
URLS = {
    "game_weights": "https://github.com/openvpi/GAME/releases/download/v1.0.0/GAME-1.0-medium.zip",
    "game_repo": "https://github.com/openvpi/GAME.git",
    "rmvpe_repo": "https://github.com/Dream-High/RMVPE.git",
    "rmvpe_pt": [
        "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        "https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
    ],
    "soundfont": [
        "https://github.com/pianobooster/fluid-soundfont/releases/download/v3.1/FluidR3_GM.sf2",
        "https://github.com/urish/cinto/raw/master/media/FluidR3%20GM.sf2",
    ],
    "fluidsynth_pinned": "https://github.com/FluidSynth/fluidsynth/releases/download/v2.6.1/fluidsynth-v2.6.1-win10-x64-cpp11.zip",
    "fluidsynth_api": "https://api.github.com/repos/FluidSynth/fluidsynth/releases/latest",
    "dexed": "https://github.com/asb2m10/dexed/releases/download/v1.0.1/Dexed-1.0.1-win.zip",
    "tal": "https://tal-software.com/downloads/plugins/install_TAL-Chorus-LX.zip",
}

# Packages that must resolve from the TSOV venv — never duplicated inside GAME's venv.
# Derived from the tested reference environment: GAME's venv was produced with the
# full dependency closure, then this shared set was removed so torch/numpy/scipy/etc.
# have exactly one copy (in tsov/.venv, which GAME sees via tsovvenv.pth). Without
# this, a duplicated numpy/scipy pair (e.g. numpy 1.26 + a scipy build using np.long)
# breaks imports.
SHARED_PRUNE = [
    "absl_py", "certifi", "cffi", "charset_normalizer", "cloudpickle", "colorama", "contourpy",
    "cycler", "decorator", "filelock", "fonttools", "fsspec", "grpcio", "h5py", "idna", "jinja2",
    "joblib", "kiwisolver", "lazy_loader", "librosa", "llvmlite", "markdown", "markupsafe",
    "matplotlib", "mido", "mpmath", "msgpack", "narwhals", "networkx", "numba", "numpy",
    "packaging", "pillow", "platformdirs", "pooch", "protobuf", "pycparser", "pyparsing",
    "python_dateutil", "requests", "resampy", "scikit_learn", "scipy", "setuptools", "six",
    "soundfile", "soxr", "sympy", "tensorboard", "tensorboard_data_server", "threadpoolctl",
    "torch", "tqdm", "typing_extensions", "urllib3", "werkzeug",
]

IS_WIN = sys.platform.startswith("win")


def log(msg: str) -> None:
    print(f"[fetch-engines] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[fetch-engines] ERROR: {msg}", file=sys.stderr, flush=True)


def mirror(url: str, gh_mirror: str | None) -> str:
    if gh_mirror and url.startswith(("https://github.com/", "https://api.github.com/")):
        return gh_mirror.rstrip("/") + "/" + url
    return url


def probe(url: str, timeout: int = 60):
    """Return (status, total_bytes, note) — ranged GET of the first 1 KiB."""
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = None
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
            elif r.status == 200 and r.headers.get("Content-Length"):
                total = int(r.headers["Content-Length"])
            n = len(r.read(1024))
            return r.status, total, f"read {n} B"
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)


def download(url: str, dest: Path, label: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    label = label or dest.name
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        next_mark = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if got >= next_mark:
                pct = f"{got * 100 // total}%" if total else f"{got // (1 << 20)} MB"
                log(f"    {label}: {pct}")
                next_mark += 32 << 20
    if total and got < total:
        tmp.unlink(missing_ok=True)
        raise OSError(f"incomplete download: got {got:,} of {total:,} B")
    tmp.replace(dest)
    log(f"    saved {dest.relative_to(REPO) if dest.is_relative_to(REPO) else dest} ({dest.stat().st_size:,} B)")


def try_download(urls: list[str], dest: Path, label: str, gh_mirror: str | None) -> bool:
    for i, url in enumerate(urls):
        u = mirror(url, gh_mirror)
        for attempt in (1, 2):  # one retry — some hosts (e.g. tal-software.com) are flaky
            try:
                log(f"    downloading {label} from {u}" + ("" if attempt == 1 else " (retry)"))
                download(u, dest, label)
                return True
            except Exception as e:  # noqa: BLE001
                fail(f"    {label} source {i + 1}/{len(urls)} attempt {attempt} failed: {e}")
    return False


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def extract_basenames(zip_path: Path, wanted: list[str], dest: Path) -> list[str]:
    """Extract members whose basename matches `wanted`, flat, into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1]
            if base in wanted:
                with zf.open(member) as src, open(dest / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                done.append(base)
    return done


def extract_vst3_bundle(zip_path: Path, vst3_root: Path) -> tuple[str, int] | None:
    """Extract the first `*.vst3/` bundle subtree found in the zip, keeping its layout.

    Returns (bundle_name, file_count) or None. E.g. the Dexed zip contains
    `Dexed.vst3/Contents/x86_64-win/Dexed.vst3` -> vst3_root/Dexed.vst3/... .
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        prefix = None
        for n in names:
            parts = n.split("/")
            for i, part in enumerate(parts[:-1]):
                if part.endswith(".vst3"):
                    prefix = "/".join(parts[: i + 1])
                    break
            if prefix:
                break
        if not prefix:
            return None
        bundle = prefix.rsplit("/", 1)[-1]
        dest = vst3_root / bundle
        count = 0
        for n in names:
            if not n.startswith(prefix + "/") or n.endswith("/"):
                continue
            rel = n[len(prefix) + 1:]
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            count += 1
        return bundle, count


def extract_root_member(zip_path: Path, basename: str, out_path: Path) -> bool:
    """Extract a single zip member by basename (e.g. LICENSE) to out_path."""
    with zipfile.ZipFile(zip_path) as zf:
        for n in zf.namelist():
            if n.rsplit("/", 1)[-1] == basename:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(out_path, "wb") as f:
                    shutil.copyfileobj(src, f)
                return True
    return False


def tsov_venv_site_packages() -> Path | None:
    venv = REPO / "tsov" / ".venv"
    if IS_WIN:
        cand = venv / "Lib" / "site-packages"
        return cand if cand.is_dir() else None
    pkgs = sorted((venv / "lib").glob("python*/site-packages"))
    return pkgs[0] if pkgs else None


def tsov_venv_python() -> Path | None:
    p = REPO / "tsov" / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return p if p.is_file() else None


def game_venv_python(game_dir: Path) -> Path:
    return game_dir / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


def dist_name_of(dist_info_dir: Path) -> str | None:
    m = re.match(r"^(.+?)-\d[^-]*\.dist-info$", dist_info_dir.name)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def fetch_fluidsynth(dest: Path, args) -> bool:
    log("fluidsynth: FluidSynth DLLs")
    if not IS_WIN:
        log("  non-Windows: install via your package manager instead — `brew install fluid-synth` / `apt install fluidsynth` (pyfluidsynth finds the system library). Skipping.")
        return True
    bin_dir = dest / "fluidsynth" / "bin"
    if bin_dir.is_dir() and any(bin_dir.glob("libfluid*.dll")) and not args.force:
        log(f"  already present: {bin_dir} — skipping (use --force to re-fetch)")
        return True

    url = None
    try:  # resolve latest win10-x64 asset via GitHub API (rate-limited, fall back to pin)
        with urllib.request.urlopen(urllib.request.Request(URLS["fluidsynth_api"], headers=UA), timeout=30) as r:
            rel = json.load(r)
        for a in rel.get("assets", []):
            if "win10-x64" in a["name"]:
                url = a["browser_download_url"]
                log(f"  latest release {rel.get('tag_name')}: {a['name']} ({a['size']:,} B)")
                break
    except Exception as e:  # noqa: BLE001
        log(f"  release lookup failed ({e}); using pinned v2.6.1")
    url = url or URLS["fluidsynth_pinned"]

    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "fluidsynth.zip"
        if not try_download([url], zpath, "fluidsynth zip", args.gh_mirror):
            return False
        got = extract_basenames(zpath, ["libfluidsynth-3.dll", "SDL3.dll", "sndfile.dll", "fluidsynth.exe"], bin_dir)
    log(f"  placed in {bin_dir}: {', '.join(sorted(got)) or 'NONE FOUND — check the zip layout'}")
    return bool(got)


def fetch_soundfont(dest: Path, args) -> bool:
    log("soundfont: FluidR3_GM.sf2")
    target = dest / "soundfonts" / "FluidR3_GM.sf2"
    if target.is_file() and not args.force:
        log(f"  already present: {target} ({target.stat().st_size:,} B) — skipping")
        return True
    ok = try_download(URLS["soundfont"], target, "FluidR3_GM.sf2", args.gh_mirror)
    if ok and target.stat().st_size < 100_000_000:
        fail(f"  {target} looks too small ({target.stat().st_size:,} B) — got an error page?")
        return False
    return ok


def fetch_rmvpe(dest: Path, args) -> bool:
    log("rmvpe: Dream-High/RMVPE + rmvpe.pt")
    rdir = dest / "RMVPE"
    if not (rdir / ".git").is_dir():
        url = mirror(URLS["rmvpe_repo"], args.gh_mirror)
        log(f"  cloning {url} -> {rdir}")
        p = run(["git", "clone", url, str(rdir)])
        if p.returncode != 0:
            fail(f"  clone failed:\n{p.stderr[-1500:]}")
            return False
    else:
        log(f"  repo already present: {rdir}")
    p = run(["git", "-C", str(rdir), "checkout", RMVPE_COMMIT])
    if p.returncode != 0:
        log(f"  note: could not checkout pinned commit {RMVPE_COMMIT[:10]} ({p.stderr.strip()[:200]}) — keeping current revision")

    # upstream ships no rmvpe_model.py — it's an RVC-derived adaptation maintained here
    model_src = REPO / "scripts" / "vendor-extras" / "rmvpe_model.py"
    model_dst = rdir / "rmvpe_model.py"
    if not model_dst.is_file() or args.force:
        if model_src.is_file():
            shutil.copyfile(model_src, model_dst)
            log("  placed rmvpe_model.py (upstream Dream-High repo does NOT ship it — RVC-derived adaptation maintained in this repo)")
        else:
            fail("  scripts/vendor-extras/rmvpe_model.py not found in the repo — cannot complete RMVPE setup")
            return False
    else:
        log("  rmvpe_model.py already present — skipping")

    pt = rdir / "rmvpe.pt"
    if pt.is_file() and not args.force:
        log(f"  already present: {pt} ({pt.stat().st_size:,} B) — skipping")
        return True
    urls = URLS["rmvpe_pt"]
    if args.hf_mirror:  # user-forced mirror goes first
        urls = [urls[1].replace("https://hf-mirror.com", args.hf_mirror.rstrip("/")), urls[0]]
    return try_download(urls, pt, "rmvpe.pt", None)


def prune_shared_from_game(site: Path, gpy: Path) -> None:
    """Remove duplicated shared packages from GAME's venv (see SHARED_PRUNE)."""
    present: dict[str, str] = {}
    for d in site.glob("*.dist-info"):
        name = dist_name_of(d)
        if name:
            present[name.lower().replace("_", "-")] = name
    wanted = {n.replace("_", "-").lower() for n in SHARED_PRUNE}
    todo = sorted(present[k] for k in (present.keys() & wanted))
    if not todo:
        log("  shared-package prune: nothing to do")
        return
    uv = shutil.which("uv")
    if not uv:
        fail("  `uv` not found — cannot prune duplicated shared packages")
        return
    log(f"  pruning {len(todo)} shared packages from GAME's venv (torch/numpy/scipy & co. resolve from the tsov venv)")
    p = run([uv, "pip", "uninstall", "--python", str(gpy), *todo])
    if p.returncode != 0:
        fail(f"  prune reported issues: {(p.stderr or p.stdout or '')[-400:]}")


def fetch_game(dest: Path, args) -> bool:
    log("game: openvpi/GAME (repo + weights + venv + patch)")
    gdir = dest / "GAME"
    ok_all = True

    # 1) repo + pinned checkout
    if not (gdir / ".git").is_dir():
        url = mirror(URLS["game_repo"], args.gh_mirror)
        log(f"  cloning {url} -> {gdir} (~100 MB)")
        p = run(["git", "clone", url, str(gdir)])
        if p.returncode != 0:
            fail(f"  clone failed:\n{p.stderr[-1500:]}")
            return False
    else:
        log(f"  repo already present: {gdir}")
    p = run(["git", "-C", str(gdir), "checkout", GAME_COMMIT])
    if p.returncode != 0:
        log(f"  note: could not checkout pinned commit {GAME_COMMIT[:10]} — keeping current revision (patch may need updating)")

    # 2) weights (v1.0.0 medium -> pretrained/)
    pretrained = gdir / "pretrained"
    if (pretrained / "model.pt").is_file() and not args.force:
        log(f"  already present: {pretrained / 'model.pt'} — skipping")
    else:
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "GAME-1.0-medium.zip"
            if not try_download([URLS["game_weights"]], zp, "GAME-1.0-medium.zip", args.gh_mirror):
                ok_all = False
            else:
                got = extract_basenames(zp, ["model.pt", "config.yaml", "lang_map.json"], pretrained)
                log(f"  weights placed in {pretrained}: {', '.join(sorted(got)) or 'NONE — check zip layout'}")
                ok_all = ok_all and "model.pt" in got

    # 3) device patch
    patch = REPO / "scripts" / "patches" / "game-device-flag.patch"
    if patch.is_file():
        c = run(["git", "-C", str(gdir), "apply", "--check", str(patch)])
        if c.returncode == 0:
            run(["git", "-C", str(gdir), "apply", str(patch)])
            log("  device patch applied (adds --device / accelerator pass-through)")
        else:
            rev = run(["git", "-C", str(gdir), "apply", "--reverse", "--check", str(patch)])
            if rev.returncode == 0:
                log("  device patch already applied — skipping")
            else:
                log("  note: device patch does not apply cleanly (GAME revision mismatch?) — continuing; tsov degrades gracefully without --device")

    # 4) venv (reusing the tsov venv's interpreter; --system-site-packages like the tested setup)
    gpy = game_venv_python(gdir)
    if not gpy.is_file():
        tsov_py = tsov_venv_python()
        if tsov_py is None:
            fail("  tsov venv not found (tsov/.venv) — set it up first (README quickstart), then re-run")
            return False
        log(f"  creating venv: {gdir / '.venv'} (from tsov venv python)")
        p = run([str(tsov_py), "-m", "venv", "--system-site-packages", str(gdir / ".venv")])
        if p.returncode != 0 or not gpy.is_file():
            fail(f"  venv creation failed:\n{p.stderr[-1000:]}")
            return False

    site = gpy.parent.parent / "Lib/site-packages" if IS_WIN else None
    if not IS_WIN:
        site = next(iter((gdir / ".venv").glob("lib/python*/site-packages")), None)
    if site is None:
        fail("  could not locate GAME venv site-packages")
        return False

    # 5) dependencies (torch & the shared numeric stack come from the tsov venv — see prune below)
    if not (site / "lightning").is_dir() or args.force:
        uv = shutil.which("uv")
        if not uv:
            fail("  `uv` not found on PATH — install it from https://docs.astral.sh/uv/ then re-run")
            return False
        log("  installing GAME requirements via uv (can take a few minutes)…")
        p = run([uv, "pip", "install", "--python", str(gpy), "-r", str(gdir / "requirements.txt")])
        if p.returncode != 0:
            fail(f"  dependency install failed:\n{(p.stderr or p.stdout)[-1500:]}")
            return False
    else:
        log("  GAME deps already installed — skipping")

    # 6) prune duplicated shared packages (numpy/scipy/torch shadows — see SHARED_PRUNE)
    prune_shared_from_game(site, gpy)

    # 7) wire GAME's venv to the tsov venv site-packages (so it sees torch)
    tsp = tsov_venv_site_packages()
    if tsp is None:
        fail("  tsov venv site-packages not found — is tsov installed? (README quickstart)")
        return False
    pth = site / "tsovvenv.pth"
    wanted = str(tsp)
    if not pth.is_file() or pth.read_text(encoding="utf-8", errors="replace").strip() != wanted:
        pth.write_text(wanted + "\n", encoding="utf-8")
        log(f"  wrote {pth.name} -> {wanted}")
    else:
        log("  tsovvenv.pth already wired — skipping")
    return ok_all


def fetch_vst3(dest: Path, args) -> bool:
    log("vst3: test plugins (Dexed synth + TAL-Chorus-LX chorus)")
    vdir = dest / "vst3"
    ok_all = True

    if (vdir / "Dexed.vst3").is_dir() and not args.force:
        log(f"  Dexed already present: {vdir / 'Dexed.vst3'} — skipping")
    else:
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "Dexed-1.0.1-win.zip"
            if not try_download([URLS["dexed"]], zp, "Dexed-1.0.1-win.zip", args.gh_mirror):
                ok_all = False
            else:
                got = extract_vst3_bundle(zp, vdir)
                if got:
                    bundle, count = got
                    extract_root_member(zp, "LICENSE", vdir / "Dexed-LICENSE.txt")
                    log(f"  placed {vdir / bundle} ({count} files)")
                else:
                    fail("  no *.vst3 bundle found in the Dexed zip")
                    ok_all = False

    if (vdir / "TAL-Chorus-LX.vst3").is_dir() and not args.force:
        log(f"  TAL-Chorus-LX already present: {vdir / 'TAL-Chorus-LX.vst3'} — skipping")
    else:
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "install_TAL-Chorus-LX.zip"
            if not try_download([URLS["tal"]], zp, "install_TAL-Chorus-LX.zip", None):
                ok_all = False
            else:
                got = extract_vst3_bundle(zp, vdir)
                if got:
                    bundle, count = got
                    log(f"  placed {vdir / bundle} ({count} files)")
                else:
                    fail("  no *.vst3 bundle found in the TAL zip")
                    ok_all = False
    return ok_all


STEPS = {"fluidsynth": fetch_fluidsynth, "soundfont": fetch_soundfont, "rmvpe": fetch_rmvpe, "game": fetch_game, "vst3": fetch_vst3}


# ---------------------------------------------------------------------------
# probes & verification
# ---------------------------------------------------------------------------

def do_check(args) -> int:
    log("check: probing download sources (no writes)")
    urls = []
    urls.append(("fluidsynth (api)", URLS["fluidsynth_api"]))
    urls.append(("fluidsynth (pin)", URLS["fluidsynth_pinned"]))
    urls += [("soundfont", u) for u in URLS["soundfont"]]
    urls += [("rmvpe.pt", u) for u in URLS["rmvpe_pt"]]
    urls.append(("game weights", URLS["game_weights"]))
    urls.append(("dexed", URLS["dexed"]))
    urls.append(("tal", URLS["tal"]))
    rc = 0
    for label, u in urls:
        st, total, note = probe(mirror(u, args.gh_mirror))
        size = f"{total:,} B" if total else "?"
        mark = "OK " if st and st < 400 else "FAIL"
        if mark == "FAIL":
            rc = 1
        log(f"  [{mark}] {label:16s} status={st} size={size:>16s}  {u}  ({note})")
    return rc


def _check(label: str, cmd: list[str], gate: Path) -> bool:
    if not gate.exists():
        log(f"  [skip] {label} — not installed")
        return True
    p = run(cmd, cwd=str(REPO))
    if p.returncode == 0:
        last = (p.stdout or "").strip().splitlines()
        log(f"  [OK]   {label}: {last[-1] if last else 'ok'}")
        return True
    fail(f"  [FAIL] {label}:\n{(p.stderr or p.stdout or '')[-1200:]}")
    return False


def do_verify(dest: Path) -> int:
    log("verify: functional checks")
    tsv = tsov_venv_python()
    if tsv is None:
        fail("verify needs the tsov venv (tsov/.venv) — see the README quickstart")
        return 1
    rc = 0
    ok = _check(
        "fluidsynth + soundfont",
        [str(tsv), "-c",
         "from tsov.render.fluidsynth_backend import default_soundfont,_dll_dir,_load_fluidsynth;"
         "f=_load_fluidsynth();s=f.Synth();sf=default_soundfont();"
         "assert sf and _dll_dir();assert s.sfload(sf)!=f.FLUID_FAILED;s.delete();print('sfload ok:',sf)"],
        dest / "soundfonts" / "FluidR3_GM.sf2",
    )
    rc |= 0 if ok else 1
    ok = _check(
        "rmvpe (files + torch)",
        [str(tsv), "-c",
         f"from pathlib import Path;p=Path({str(dest / 'RMVPE')!r});"
         "assert p.joinpath('rmvpe.pt').is_file() and p.joinpath('rmvpe_model.py').is_file();"
         "import torch;print('torch',torch.__version__)"],
        dest / "RMVPE" / "rmvpe.pt",
    )
    rc |= 0 if ok else 1
    gpy = game_venv_python(dest / "GAME")
    ok = _check(
        "game (venv + torch via pth)",
        [str(gpy), "-c", "import torch,lightning;print('torch',torch.__version__)"],
        dest / "GAME" / "pretrained" / "model.pt",
    )
    rc |= 0 if ok else 1
    ok = _check(
        "vst3 plugins (load via pedalboard)",
        [str(tsv), "-c",
         "from pedalboard import load_plugin;"
         f"d=load_plugin({str(dest / 'vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3')!r});"
         f"t=load_plugin({str(dest / 'vst3/TAL-Chorus-LX.vst3/Contents/x86_64-win/TAL-Chorus-LX.vst3')!r});"
         "assert d.is_instrument and not t.is_instrument;print('instrument+effect load ok')"],
        dest / "vst3" / "TAL-Chorus-LX.vst3",
    )
    rc |= 0 if ok else 1
    log("verify done" if rc == 0 else "verify FAILED — see messages above")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch the optional tsov engines (not bundled).")
    ap.add_argument("--only", nargs="+", choices=DEFAULT_STEPS, default=DEFAULT_STEPS, help="subset of engines to fetch")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="target vendor dir (default: ./vendor)")
    ap.add_argument("--check", action="store_true", help="probe download sources only; no writes")
    ap.add_argument("--no-verify", action="store_true", help="skip the functional verification at the end")
    ap.add_argument("--force", action="store_true", help="re-fetch even if present")
    ap.add_argument("--gh-mirror", default=None, help="GitHub mirror prefix, e.g. https://ghfast.top")
    ap.add_argument("--hf-mirror", default=None, help="HuggingFace mirror, e.g. https://hf-mirror.com")
    args = ap.parse_args()

    if args.check:
        return do_check(args)

    dest = Path(args.dest).resolve()
    log(f"target: {dest}")
    failed = []
    for name in args.only:
        try:
            if not STEPS[name](dest, args):
                failed.append(name)
        except Exception as e:  # noqa: BLE001 — keep going, report at the end
            fail(f"{name}: unexpected error: {e}")
            failed.append(name)
    rc = 1 if failed else 0
    if failed:
        fail(f"done with failures: {', '.join(failed)}")
    if not args.no_verify:
        rc |= do_verify(dest)
    if rc == 0:
        log("all done. also try: tsov backends  (+ smoke test in docs/setup-engines.md)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
