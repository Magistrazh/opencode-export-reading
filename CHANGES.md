# Changes

## 0.10.1

- Added `scripts/validate.py`: structural validator for frontmatter, version,
  required paths, relative links, forbidden artifacts and eval fixtures.
- Trigger eval now writes `benchmark.json` / `benchmark.md` with duration,
  tokens and cost from `step_finish` events (`--write-benchmark`).
- Golden eval now writes a structured assertion report with evidence, output
  sizes and exit codes (`--write-report PATH`).
- Added a quick-reference command table to `SKILL.md`.
- Added standalone MIT repository layout, CI, `SECURITY.md` and
  `CONTRIBUTING.md`; moved evals into the repository.

## 0.10.0

- Added standalone `--summary-outline N` and `--summary-grep N TERM`.
- `--queries` is literal-only; regex batches go through `--queries-file`.
- Ambiguous summary-flag combinations are rejected.

## 0.9.2

- Regex comma hint and the `--only text` empty-output note.
- `feedback.py` defaults `trigger` to `agent` / `manual`.

## 0.9.1

- Split command dispatch; no user-facing flag changes.

## 0.9.0

- Trigger and golden evals added.
- `SKILL.md` reduced to 172 lines; feedback schema `1` with `trigger`.
- Optional observer plugin; no cache/index.
