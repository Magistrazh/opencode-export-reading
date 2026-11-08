# Contributing

## Development setup

Requirements: Python 3.10+, Git, and `ruff` for linting. The runtime scripts
are stdlib-only and have no third-party dependencies.

```bash
git clone https://github.com/Magistrazh/opencode-export-reading.git
cd opencode-export-reading
python scripts/inspect-export.py --selftest
python scripts/feedback.py --selftest
python scripts/validate.py --selftest
python scripts/validate.py
```

Eval dependencies are local:

```bash
python evals/opencode-export-reading/score-trigger.py --selftest
python evals/opencode-export-reading/run-golden.py --selftest
python evals/opencode-export-reading/run-golden.py
```

## Rules

- Do not add a real session export, transcript, benchmark run, or personal path.
- Add a test for every behavior change; keep tests deterministic and offline.
- Run `ruff check --no-cache scripts evals` and `python -m py_compile` before opening a PR.
- `run-trigger.sh` is a live model test. It is manual, costs money, and is not
  required for CI.
- Keep `SKILL.md` agent-facing and concise; put developer detail in `README.md`
  or `CONTRIBUTING.md`.
- Update `CHANGES.md` and the version in `SKILL.md` for a release.

## Reporting bugs

Use a synthetic fixture or a redacted description. Never paste a real export.
Security issues: see `SECURITY.md`.
