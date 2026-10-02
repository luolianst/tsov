# Contributing to tsov

Thanks for wanting to help! tsov is a **solo project** with a big ambition — here's how we can work together without either of us burning out.

## The short version

- **Issues are welcome** — bug reports and well-scoped ideas. Write in English or Chinese, whichever you're comfortable with.
- **Small PRs are welcome** — typo fixes, bug fixes, docs improvements, and translations get merged happily.
- **Big changes: open an issue first** so we can agree on the approach before you build.
- **No SLA** — this is a best-effort project. Responses may take days or weeks.

## Translation fast-lane

The UI is currently Chinese-only. **English UI (i18n) is planned for v0.2**, and translation help is the single most valuable contribution right now. Open an issue with `translation` in the title, or send a PR once the i18n groundwork lands.

## Development setup

Requires **Python 3.12** and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Create the dev environment (it lives at tsov/.venv)
uv venv tsov/.venv --python 3.12
uv pip install --python tsov/.venv -e .

# 2. Run the web host shell
tsov/.venv/Scripts/tsov.exe web --host 127.0.0.1 --port 8790   # Windows
# tsov/.venv/bin/tsov web --host 127.0.0.1 --port 8790          # macOS / Linux

# 3. Run the tests
tsov/.venv/Scripts/python.exe -m pytest                          # Windows
```

Notes:

- Some features (GAME / RMVPE transcription backends) additionally need PyTorch and model weights that are not in the lockfile — see the docs for backend setup.
- The visual host shell runs at <http://127.0.0.1:8790> once started.

## Ground rules for code

- New features must go through the **command layer** (`Project.apply_batch` / EditBatch) — no side-channel writes to project files. This is an architecture rule; see `docs/decisions/0017-共享动作路径.md`.
- Keep the test suite green before opening a PR.

## License

By contributing, you agree that your contributions are licensed under the **AGPL-3.0** (see [`LICENSE`](LICENSE)).
