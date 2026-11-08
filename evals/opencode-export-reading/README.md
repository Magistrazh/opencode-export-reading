# opencode-export-reading evals

Small, repeatable checks for the things `--selftest` cannot measure:

- **Trigger corpus** — does a realistic prompt actually invoke the skill?
- **Golden fixtures** — does the CLI recover the known fact and handle the
  documented failure modes?

## Trigger corpus

`trigger.tsv` has 24 fixed cases: 12 positive, 12 negative, 8 Russian.
`prompts/trigger-corpus.md` is the same list for humans.

Install/link the repository root into `~/.config/opencode/skills/` first; the live runner tests the installed skill.

```bash
bash evals/opencode-export-reading/run-trigger.sh --dry-run
STAMP=baseline-0.8.11 RUNS=2 bash evals/opencode-export-reading/run-trigger.sh
python evals/opencode-export-reading/score-trigger.py \
  evals/opencode-export-reading/runs/baseline-0.8.11 \
  --write-baseline evals/opencode-export-reading/baselines/trigger-0.8.11.tsv
python evals/opencode-export-reading/score-trigger.py   evals/opencode-export-reading/runs/baseline-0.8.11 --write-benchmark
```

`--write-benchmark` writes `benchmark.json` and `benchmark.md` in the run
directory: recall plus per-case duration, tokens and cost parsed from
`step_finish` events. It does not re-run agents.

The runner uses `opencode run --pure --format json`, model `opencode-go/deepseek-v4.1-flash`
and agent `build` by default; override with `MODEL`/`AGENT`. Model, agent,
repetitions, timestamp and opencode version are written to
`runs/<stamp>/metadata.json` and copied next to the baseline as
`trigger-0.8.11.meta.json`.

Every run uses a temporary `SKILL_FEEDBACK_DIR`; nothing is written to the
shared feedback folder. `runs/` is gitignored; the baseline TSV is committed.

After changing `SKILL.md`, run the same corpus into a new stamp and compare:

```bash
python evals/opencode-export-reading/score-trigger.py \
  evals/opencode-export-reading/runs/after-shrink \
  --compare evals/opencode-export-reading/baselines/trigger-0.8.11.tsv
```

`run-golden.py` locates `scripts/inspect-export.py` in this repository and does not need an installed copy.

Gate: positive recall may drop by at most one case; negative false-positive
rate must stay at or below 0.25. `--compare` also prints duration/token/cost
deltas when the baseline benchmark exists.

Scoring detects a `tool_use` event with `part.tool == "skill"` and
`part.state.input.name == "opencode-export-reading"`. It falls back to the
`<skill_content name="opencode-export-reading">` marker. The detector is tested
by `score-trigger.py --selftest`, including a case that must **not** fire.

## Golden fixtures

`fixtures/` contains six synthetic exports and `manifest.json`:

| Fixture | Covers |
|---|---|
| `normal-compaction` | one boundary, lost decision, retained tail |
| `missing-tail-start-id` | unresolved boundary must fail, not guess |
| `empty-summary` | compaction event with no summary text |
| `multiple-compactions` | last boundary is authoritative |
| `huge-tool-output` | `--max-chars` truncation |
| `sanitized-export` | redacted text, structure preserved |

Rebuild them with:

```bash
python evals/opencode-export-reading/fixtures/build_fixtures.py
```

Each fixture is under 200 KB, contains no real session data, and the manifest
records expected counts, summary state, search hits and exit codes.

Run the golden harness:

```bash
python evals/opencode-export-reading/run-golden.py
python evals/opencode-export-reading/run-golden.py --selftest
python evals/opencode-export-reading/run-golden.py --write-report evals/opencode-export-reading/runs/golden-0.10.1/report.json
python evals/opencode-export-reading/run-golden.py --real \
  exports/session-a.json exports/session-b.json
```

`--real` is record-only: it does not fail on counts from real exports, but
prints wall time, message/hit counts and JSON-key checks. `--write-report`
writes per-assertion evidence plus a sibling `timing.json`; a failing assertion
returns exit 1.

Structural gate:

```bash
python scripts/validate.py
python scripts/validate.py --selftest
```

## Rules

- Do not commit `runs/`; it may contain model output.
- Do not put export contents, paths or secrets into eval reports.
- Do not use a real session as a golden fixture; build from
  `references/schema.md`.
- A check that has never failed is not a check: `score-trigger.py --selftest`
  includes a deliberate no-skill transcript, and `run-golden.py --selftest`
  corrupts one expectation to prove the harness reports it.
