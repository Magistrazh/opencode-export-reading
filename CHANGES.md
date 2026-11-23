# Changes

## 0.17.0

- Reads the v2 shape written by `opencode session export`: flat messages with
  `type`, `text` and a `content` array are normalized to the internal
  `{info, parts}` model on read, so every mode works with no pre-processing
  step. Message indices still match the file one-to-one, and v1 `{info, parts}`
  exports keep working unchanged.
- A v2 compaction carries its own summary and has no `tail_start_id`, so its
  boundary is positional: the message after it. `--json` reports
  `boundary_kind: "positional"`, `--compactions` prints `tail positional`, and
  `--before-tail`, `--digest` and `--check-summary` use that boundary instead of
  failing on a missing id.
- `--search` also matches the summary inside a compaction part (`field:
  summary`), so v2 summaries stay searchable like their v1 counterparts.
- Service messages (`system`, `synthetic`, `idle`, `agent-switched`,
  `location-switched`) keep their index slot and are counted as `system`, not as
  `assistant`; the `messages` object gains a `system` key only when the export
  has such rows.
- Docs: `opencode export` no longer exists in v2. README and SKILL.md name
  `opencode session export <session-id>`, and `references/schema.md` documents
  the flat shape.

## 0.16.0

- `--digest` lists the requests and the completed turn ends before the tail, one
  truncated line each with a message index for `--dump --from-index`. A turn end
  is structural: the last assistant message of a turn whose `finish` is `stop`,
  while an interrupted turn is never reported as one. `--json` returns
  `requests`, `turn_end`, the boundary index and totals. `--digest` always reads
  history up to the tail, so `--all`, `--offset` and `--count` are rejected
  instead of being silently ignored.
- `--check-summary [N]` compares compaction summary N with the messages after its
  own boundary and reports candidates: `ORDER` (the same ASCII identifiers in
  another order) and `VALUE` (a `--flag value` pair whose value differs). `--json`
  additionally lists tokens the tail never mentions. The patterns are ASCII and
  literal only, so prose is not checked and silence is not proof; the history
  wins, and a warning is meant to be confirmed with `--dump`. A missing summary,
  an empty summary and an unresolved `tail_start_id` are errors, not guesses.
- Both modes are registered, so usage records carry `digest` and `summary-check`
  instead of falling back to `overview`.

## 0.15.2

- Feedback and friction hints use the script path as invoked, without resolving
  junctions/symlinks. An installed skill now points at its own `feedback.py`,
  not at the repository target behind the junction.
- `SKILL.md`: `--queries-file` keeps commas; one line is one query, while
  `--queries` splits on commas.

## 0.15.1

- `SKILL.md` no longer links to repository-only docs (README, CHANGES,
  SECURITY); the installed payload points only at bundled files. Network URLs
  are not part of the skill contract.

## 0.15.0

- `--search`/`--queries` accept `--since` and `--until`: exact ISO-8601 time
  bounds, `--since` inclusive and `--until` exclusive; a bare date means local
  00:00, so `--since 2026-11-11 --until 2026-11-12` covers exactly one day.
- `--exclude-paths` now works for search as well as `--summary-grep`: matches
  inside filesystem-path-like tokens are skipped, while URLs are kept.
- `--counts` prints per-query totals and field distribution without snippets,
  so a wide batch fits in one screen; `--json` remains for machine parsing.
- `--with-context` prints a `dump:` hint for each shown hit, using that hit's
  index, instead of one hint for the first hit on the page.
- `SKILL.md` points to README/CHANGES/SECURITY by absolute GitHub URL, so the
  installed payload no longer references files it does not contain; a running
  tool-call warning is documented as an incomplete-live-export signal.

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
