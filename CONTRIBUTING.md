# Contributing

## Development setup

Requirements: Python 3.10+, Git, and `ruff` for linting. The runtime scripts
are stdlib-only and have no third-party dependencies.

```bash
git clone https://github.com/Magistrazh/opencode-export-reading.git
cd opencode-export-reading
python skills/opencode-export-reading/scripts/inspect-export.py --selftest
python skills/opencode-export-reading/scripts/feedback.py --selftest
python skills/opencode-export-reading/scripts/validate.py --selftest
python skills/opencode-export-reading/scripts/validate.py
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
- Run `ruff check --no-cache skills evals` and `python -m py_compile` before opening a PR.
- Keep the skill payload in `skills/opencode-export-reading/`; docs, evals and CI
  stay at the repository root.
- `run-trigger.sh` is a live model test. It is manual, costs money, and is not
  required for CI.
- Keep `SKILL.md` agent-facing and concise; put developer detail in `README.md`
  or `CONTRIBUTING.md`.
- Update `CHANGES.md` and the version in `SKILL.md` for a release.

## Feedback triage

Feedback records are append-only and never rewritten. Maintainer decisions live
in a separate ledger under `triage/`:

```bash
python skills/opencode-export-reading/scripts/feedback.py --stats
python skills/opencode-export-reading/scripts/feedback.py --open 20
python skills/opencode-export-reading/scripts/feedback.py --mark <id|prefix> --as fixed --reason "..." --release 0.13.0
```

`--open` lists actionable unmarked records only; clean `yes` verdicts are not
actionable. Statuses: `fixed`, `accepted`, `duplicate`, `obsolete`, `open`
(reopen). `--list` exposes each record's id and triage status.

## Reporting bugs

Use a synthetic fixture or a redacted description. Never paste a real export.
Security issues: see `SECURITY.md`.
