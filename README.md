# opencode-export-reading

[Русская версия](README.ru.md)

[![skills.sh](https://skills.sh/b/Magistrazh/opencode-export-reading)](https://skills.sh/Magistrazh/opencode-export-reading)

OpenCode compaction leaves a summary in the live context and drops the messages
before `tail_start_id`. The session export still contains them. This tool reads
that export locally and prints the lost messages with their ids.

It is a Python CLI, also packaged as an OpenCode skill. The runtime has no
third-party dependencies.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/Magistrazh/opencode-export-reading.git
cd opencode-export-reading

# Linux / macOS
mkdir -p ~/.config/opencode/skills
ln -s "$PWD/skills/opencode-export-reading" ~/.config/opencode/skills/opencode-export-reading
```

On Windows, create the skills directory and then copy the skill folder or
create a symlink in Developer Mode:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.config\opencode\skills" | Out-Null
New-Item -ItemType SymbolicLink `
  -Path "$HOME\.config\opencode\skills\opencode-export-reading" `
  -Target (Join-Path (Get-Location) "skills\opencode-export-reading")
```

The skill itself lives in `skills/opencode-export-reading/`; the repository
root holds docs, evals and CI.

Restart OpenCode after changing the skill directory. The command examples use
Bash; on Windows, run them in Git Bash or use `$env:SKILL_DIR` in PowerShell.
To check the installed copy:

```bash
SKILL_DIR=~/.config/opencode/skills/opencode-export-reading
python "$SKILL_DIR/scripts/inspect-export.py" --selftest
```

## Usage

Export a session and point the tool at it:

```bash
opencode export <session-id> > session.json

SKILL_DIR=~/.config/opencode/skills/opencode-export-reading
EXPORT=/path/to/session.json
```

Start with the compaction map and the surviving summary:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --compactions
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --show-summary 2
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --summary-outline 2
```

An empty `--compactions` output means the session was never compacted.

Search the history:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "decision" --before-tail
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "decision" --from-index 100 --to-index 300
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "0\.2\.1-rc" --regex --before-tail
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --queries "rule,decision,rejected option" --before-tail
```

`--before-tail` limits results to the part removed by the last compaction;
`--from-index`/`--to-index` limit them to an inclusive message-index range.
`--queries` matches literal strings; use `--queries-file` for a batch of regexes.

Read the surrounding messages when a hit needs more context:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --dump --from-index 209 --count 4
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --around 1258 --window 2
```

`--dump` needs `--count`, or a start (`--from-index`/`--from-id`) together with `--count`, `--to-*` or `--all`. `--only text` keeps the output readable.
`--json` is available for overview, timeline, compactions, search, summaries and
`--list-exports`.

## Why not just grep

An export can be tens of megabytes. `grep` returns isolated lines without the
message id or role, and opening the file in an editor is slower than streaming
it. The script makes a short pre-scan for compaction boundaries, then one streaming
pass over the messages, keeps one hit per message and reports the exact number
of matches.

## Local data

Each run writes a small usage record under
`~/.local/share/opencode/skill-feedback/opencode-export-reading/usage/`: mode,
file name, size, duration, skill version, an optional opaque session id, a hash
of `SKILL.md`, and result counters. Query text and transcript content are not
stored. Set `SKILL_FEEDBACK_DIR` to use another location. Feedback
verdicts written by `feedback.py` are optional and also stay local.

Maintainer triage (`feedback.py --open`, `--mark`) is an append-only ledger
under `triage/` next to the records: the latest event per record wins, and no
record is ever rewritten.

## Limitations

The tool only reads an export. It cannot put messages back into a live session
or change the export. If `tail_start_id` is missing or cannot be resolved, it
exits with an error instead of guessing a boundary.

Exports can contain credentials, private text and prompt-injection content.
They are best kept local; issues, chats and benchmark reports are not a safe
place for them.

## Tests

```bash
python skills/opencode-export-reading/scripts/inspect-export.py --selftest
python skills/opencode-export-reading/scripts/feedback.py --selftest
python skills/opencode-export-reading/scripts/validate.py --selftest
python evals/opencode-export-reading/run-golden.py
```

Validate the trigger corpus without calling a model:

```bash
bash evals/opencode-export-reading/run-trigger.sh --dry-run
```

A live run needs an installed/linked skill and calls a model. It costs money and
is not part of CI:

```bash
STAMP=local RUNS=2 bash evals/opencode-export-reading/run-trigger.sh
```

## Files

- `skills/opencode-export-reading/scripts/inspect-export.py` — export parser and CLI
- `skills/opencode-export-reading/scripts/feedback.py` — local feedback records
- `skills/opencode-export-reading/scripts/validate.py` — repository checks
- `skills/opencode-export-reading/references/schema.md` — export fields used by the parser
- [CHANGES.md](CHANGES.md) — release history
- [evals/opencode-export-reading/README.md](evals/opencode-export-reading/README.md) — eval harness

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
