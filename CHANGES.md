# Changes

## 0.14.0

- `--brief` overview: metadata, message counts, compaction map and warnings;
  cost/model/tool tables are omitted.
- `--search`/`--queries` accept `--with-context N`: one pass collects N messages
  before/after each hit; rejected with `--json`.
- `--summary-grep` accepts `--exclude-paths`: skips lines where every match sits
  inside a path-like token.
- Batch search prints the static 0-hit advice and reasoning hint once per batch,
  not once per query.

## 0.13.3

- `SKILL.md`: dedicated "Untrusted content" rules and a `SECURITY.md` section on
  prompt injection through exports. Addresses Snyk W011 (medium): the exposure
  is inherent to reading third-party session history; the rules make the data
  vs instruction boundary explicit.

## 0.13.2

- Triage events carry a `seq` (`time.time_ns()`) and sort by `(ts, seq)`, so
  two marks written in the same clock tick resolve deterministically. Fixes a
  Windows CI flake in `feedback.py --selftest`.

## 0.13.1

- Moved `validate.py` out of the skill payload to `scripts/` at the repository
  root: it is a repository gate, not a runtime capability, and it failed when
  the skill was installed standalone. New installs contain only
  `inspect-export.py` and `feedback.py`.
- README: static skills.sh badge and the `npx skills add` install hint.

## 0.13.0

- Repository layout: the skill payload (`SKILL.md`, `scripts/`, `references/`)
  moved to `skills/opencode-export-reading/`; docs, evals and CI stay at the
  repository root. This matches the `skills/*/SKILL.md` discovery used by
  `gh skill publish` and skills.sh.
- Install instructions now link `skills/opencode-export-reading` into the
  OpenCode skills directory.
- Added the skills.sh install badge to both READMEs.

## 0.12.0

- `--search` and `--queries` accept `--from-index`/`--to-index` to limit hits to
  an inclusive message-index range; `--from-id`/`--to-id` are rejected for
  search instead of being ignored.
- `--show-summary N` prints an explanatory note to stderr when the summary is
  empty; stdout stays empty and `--json` is unchanged.
- `SKILL.md`: tool input is JSON, so search a short fragment rather than a
  quoted or multi-line phrase.

## 0.11.0

- `feedback.py`: append-only triage ledger next to feedback records
  (`triage/<ts>_<uuid>.json`); the latest event per record wins, records stay
  immutable.
- `--mark ID --as fixed|accepted|duplicate|obsolete|open --reason ...` resolves
  a record by id or unique prefix; `--release` and `--commit` add evidence.
- `--open [N]` lists actionable unmarked records; `--list` exposes the record id
  and triage status; `--stats` shows triage counts and the open queue.
- Only notes and verdicts with problems, improve or `solved != yes` are
  actionable; clean `yes` verdicts stay out of the queue.

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
