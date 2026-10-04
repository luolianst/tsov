#!/usr/bin/env python3
"""tsov — fetch the optional engines (not bundled in the repository).

Downloads and places the third-party components tsov can use for the full
experience (transcription + audio rendering):

  fluidsynth  FluidSynth 2.x DLLs             -> vendor/fluidsynth/bin/       (Windows)
  soundfont   FluidR3_GM.sf2 (~148 MB)        -> vendor/soundfonts/
  rmvpe       Dream-High/RMVPE + weights      -> vendor/RMVPE/                (humming)
  game        openvpi/GAME + venv + weights   -> vendor/GAME/                 (vocals)

Usage (run from the repository root):

  python scripts/fetch_engines.py                  # fetch everything
  python scripts/fetch_engines.py --only rmvpe     # subset:
                                                   #   fluidsynth soundfont rmvpe game
  python scripts/fetch_engines.py --check          # probe download sources only, no writes
  python scripts/fetch_engines.py --dest vendor    # custom target dir (default: ./vendor)

China mirrors (or just let it auto-fallback):
  --gh-mirror https://ghfast.top          # for GitHub release downloads / clones
  --hf-mirror https://hf-mirror.com       # for rmvpe.pt

Requires: git + Python 3.9+ (stdlib only). GAME setup additionally needs `uv`
and the tsov venv (tsov/.venv — see the README quickstart). After fetching, run
`tsov backends` and the smoke test in docs/setup-engines.md to verify.

Idempotent: existing files are kept (use --force to re-fetch).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO / "vendor"
UA = {"User-Agent": "tsov-fetch-engines/1.0"}

# Tested pins (see docs/setup-engines.md)
GAME_COMMIT = "4ad815c90dfe2442730f3fdc866fd23e737cbc97"
RMVPE_COMMIT = "a6db1cd7d26014aa739383367afd9bab57fc624c"
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
}

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
    tmp.replace(dest)
    log(f"    saved {dest.relative_to(REPO) if dest.is_relative_to(REPO) else dest} ({dest.stat().st_size:,} B)")


def try_download(urls: list[str], dest: Path, label: str, gh_mirror: str | None) -> bool:
    for i, url in enumerate(urls):
        u = mirror(url, gh_mirror)
        try:
            log(f"    downloading {label} from {u}")
            download(u, dest, label)
            return True
        except Exception as e:  # noqa: BLE001
            fail(f"    {label} source {i + 1}/{len(urls)} failed: {e}")
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


def tsov_venv_site_packages() -> Path | None:
    venv = REPO / "tsov" / ".venv"
    cand = venv / ("Lib/site-packages" if IS_WIN else "lib")
    if IS_WIN:
        return cand if cand.is_dir() else None
    pkgs = sorted((venv / "lib").glob("python*/site-packages"))
    return pkgs[0] if pkgs else None


def game_venv_python(game_dir: Path) -> Path:
    return game_dir / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")


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

    pt = rdir / "rmvpe.pt"
    if pt.is_file() and not args.force:
        log(f"  already present: {pt} ({pt.stat().st_size:,} B) — skipping")
        return True
    urls = URLS["rmvpe_pt"]
    if args.hf_mirror:  # user-forced mirror goes first
        urls = [urls[1].replace("https://hf-mirror.com", args.hf_mirror.rstrip("/")), urls[0]]
    return try_download(urls, pt, "rmvpe.pt", None)


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
        tsov_py = REPO / "tsov" / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
        if not tsov_py.is_file():
            fail("  tsov venv not found (tsov/.venv) — set it up first (README quickstart), then re-run")
            return False
        log(f"  creating venv: {gdir / '.venv'} (from tsov venv python)")
        p = run([str(tsov_py), "-m", "venv", "--system-site-packages", str(gdir / ".venv")])
        if p.returncode != 0 or not gpy.is_file():
            fail(f"  venv creation failed:\n{p.stderr[-1000:]}")
            return False

    # 5) dependencies (torch intentionally NOT installed here — it comes from the tsov venv via the .pth below)
    site = gpy.parent.parent / "Lib/site-packages" if IS_WIN else None
    if not IS_WIN:
        site = next(iter((gdir / ".venv").glob("lib/python*/site-packages")), None)
    if site is None:
        fail("  could not locate GAME venv site-packages")
        return False
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

    # 6) wire GAME's venv to the tsov venv site-packages (so it sees torch)
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


STEPS = {"fluidsynth": fetch_fluidsynth, "soundfont": fetch_soundfont, "rmvpe": fetch_rmvpe, "game": fetch_game}


def do_check(args) -> int:
    log("check: probing download sources (no writes)")
    urls = []
    urls.append(("fluidsynth (api)", URLS["fluidsynth_api"]))
    urls.append(("fluidsynth (pin)", URLS["fluidsynth_pinned"]))
    urls += [("soundfont", u) for u in URLS["soundfont"]]
    urls += [("rmvpe.pt", u) for u in URLS["rmvpe_pt"]]
    urls.append(("game weights", URLS["game_weights"]))
    rc = 0
    for label, u in urls:
        st, total, note = probe(mirror(u, args.gh_mirror))
        size = f"{total:,} B" if total else "?"
        mark = "OK " if st and st < 400 else "FAIL"
        if mark == "FAIL":
            rc = 1
        log(f"  [{mark}] {label:16s} status={st} size={size:>16s}  {u}  ({note})")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch the optional tsov engines (not bundled).")
    ap.add_argument("--only", nargs="+", choices=sorted(STEPS), default=sorted(STEPS), help="subset of engines to fetch")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="target vendor dir (default: ./vendor)")
    ap.add_argument("--check", action="store_true", help="probe download sources only; no writes")
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
    if failed:
        fail(f"done with failures: {', '.join(failed)}")
        return 1
    log("all done. verify with: tsov backends  (+ smoke test in docs/setup-engines.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
