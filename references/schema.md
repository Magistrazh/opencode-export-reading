# Export schema reference

Observed on opencode 1.18.35–1.19.5. Fields marked **optional** do not appear on
every message or part — absent means "not set", not zero. `mode`, tool names and
part types are open enums: treat an unknown value as a warning, not an error.

## Top level

| Field | Type | Notes |
|---|---|---|
| `info` | object | session row |
| `messages` | array | ordered by time |

## `info` (session)

| Field | Type | Notes |
|---|---|---|
| `id` | string | `ses_…` |
| `slug` | string | random slug |
| `projectID` | string | project hash |
| `directory` | string | working directory, absolute |
| `path` | string | may be empty |
| `title` | string | first user text or a generated title |
| `agent` | string | agent that created the session |
| `model` | object | `{id, providerID, variant}` — **last assistant message only** |
| `version` | string | opencode version at creation |
| `summary` | object | `{additions, deletions, files}` |
| `cost` | number | session total, local reconstruction |
| `tokens` | object | `{input, output, reasoning, cache:{read,write}}` — no `total` |
| `permission` | array | permission rules captured at creation |
| `time` | object | `{created, updated}` ms |

## `messages[].info`

Common: `id`, `sessionID`, `role` (`user` | `assistant`), `time.created`.

### user

| Field | Notes |
|---|---|
| `agent` | agent selected for the turn |
| `model` | `{providerID, modelID, variant}` |
| `path` | **optional**, usually null |
| `summary` | **optional**, `{diffs: []}` |

### assistant

| Field | Notes |
|---|---|
| `parentID` | id of the user message this answers |
| `mode` | `OpenAgent`, `build`, `compaction` (synthetic turn) |
| `agent` | agent name |
| `modelID`, `providerID` | the model that actually answered |
| `variant` | **optional**, e.g. `max`, `default` |
| `path` | working directory for the call |
| `cost` | equals sum of this message's `step-finish.cost` |
| `tokens` | `{total, input, output, reasoning, cache:{read,write}}` — has `total` |
| `finish` | **optional**, `tool-calls` \| `stop` \| null |
| `error` | **optional**, error object (abort, provider error) |
| `summary` | **optional**, `{diffs: []}` |

## Parts

Every part has `id`, `messageID`, `sessionID`, `type`.

### text

`text` — prose. In a sanitized export it becomes `[redacted:text:<id>]`.

### reasoning

`text`, `time.start`, `time.end` (ms).

### tool

| Field | Notes |
|---|---|
| `callID` | provider call id |
| `tool` | `bash`, `edit`, `read`, `write`, `grep`, `task`, `webfetch`, `deepsearch`, … |
| `state.status` | `pending` \| `running` \| `completed` \| `error` |
| `state.input` | arguments the model sent |
| `state.output` | what the model received — **possibly truncated** |
| `state.title` | short label (path, command, query) |
| `state.metadata` | tool-specific: `truncated`, `preview`, `display`, `loaded`, … |
| `state.time` | `{start, end}` ms |

`state.metadata.truncated` is tool-specific. It does not mean the export lost
data; it means the tool returned a shortened result to the model.

### step-start / step-finish

Markers of one model call. `step-finish` carries `cost`, `tokens`, `reason`
(e.g. `tool-calls`). One per assistant message in the observed versions, but
code that assumes exactly one is fragile — count them instead.

### file

| Field | Notes |
|---|---|
| `mime` | `image/png`, `text/plain`, … |
| `filename` | display name; may be an absolute path |
| `url` | `data:<mime>;base64,<payload>` |

Decode the payload after the first comma. Base64 length × 3/4 approximates the
decoded size.

### compaction

| Field | Notes |
|---|---|
| `auto` | **optional**, compaction triggered automatically. Absence does **not** mean «manual» — the field may simply be unset |
| `overflow` | **optional**, context overflow caused it |
| `tail_start_id` | message id that begins the retained tail |

Appears on user messages as well as assistant ones. `tail_start_id` is the first
message of the retained tail: earlier messages were replaced by the summary. They
were in the model's context before the compaction; they are not in it now — which
is different from "the model never read them".

### snapshot / patch / agent

Seen in some versions for file state snapshots and diffs. Treat unknown part
types as a warning, not a parse failure — the schema evolves.

## Reconciliation rules

1. `sum(assistant.info.cost) ≈ info.cost` (tolerance 1 % or $0.01).
2. `info.cost == sum(step-finish.cost)` per assistant message (exact).
3. `sum(assistant.tokens.*) ≈ info.tokens.*`.
4. Times are monotonic in message order.

Violations mean the export is inconsistent or the version changed semantics.
Show both values, say which one you are quoting as a local reconstruction, and do
not present either as billing truth.

## Sanitized exports

`opencode export --sanitize` keeps ids, roles, models, costs, tokens and part types; replaces
text, titles, directories and paths with `[redacted:<kind>:<id>]`. Structure
checks and cost arithmetic still work; the original wording is unrecoverable.
Detection is heuristic, and unredacted fields may remain — do not treat the file
as fully safe.


## Compaction pairs

A compaction is two adjacent messages, not one part:

1. **user** message with a `compaction` part
   (`auto`, optional `overflow`, `tail_start_id`);
2. **assistant** message with `info.mode == "compaction"`, whose `text` parts are
   the summary that replaced the older context.

`tail_start_id` is the id of the first message that stayed after the cut.
Everything before that message is absent from the live context; the compact
event and its summary are appended after the retained tail. The summary is a
paraphrase of the removed context, not a copy.

Several compactions can occur in one session. Only the last one describes the
current context; earlier summaries are themselves history.

## Search fields (`--fields`)

| Field | Source |
|---|---|
| `text` | `text` parts (user and assistant) |
| `reasoning` | `reasoning` parts |
| `tool-title` | `tool.state.title` |
| `tool-input` | `tool.state.input`, canonical JSON |
| `tool-output` | `tool.state.output` — what the model saw |
| `tool-error` | `tool.state.error` |
| `file` | `file.filename` |

Hits are reported with message index, id, timestamp, role, field and a snippet.
`before_last_compaction` is true when the hit index is before the last
`tail_start_id`. If that id is missing or unresolved, the flag is false rather
than a guess.

## Dump format (`--dump`)

```
--- #472 msg_… 2026-10-11 16:03 +0300 user
[compaction] tail_start_id=msg_… auto=True overflow=False

--- #473 2026-10-11 16:03 assistant · space-bunny-free · COMPACTION SUMMARY
[text] ## Primary Request and Intent …
```

`--around N --window W` prints messages `N-W .. N+W` — the usual way to read a
hit in context. Search pagination is `--offset` + `--limit`; the command prints
the next offset.

Range can be given by index (`--from-index` / `--to-index`) or by message id
(`--from-id` / `--to-id`); `--from-id` also accepts `#209` or `209` and treats it
as an index. With no start and no `--count`, the command refuses to run.

Parts are truncated at `--max-chars` (default 3000). Tool entries print status,
title, input, output and error.

## Feedback records

Feedback lives in `~/.local/share/opencode/skill-feedback/opencode-export-reading/`
(one JSON per run; `usage/` for automatic records).

| Field | Notes |
|---|---|
| `schema_version` | `1`; independent from `skill_version` |
| `kind` | `usage`, `suggestion` (verdict), or `note` (friction) |
| `skill_version` | `X.Y.Z` semver from `SKILL.md`; `unknown` tolerated |
| `skill_hash` | exact `SKILL.md` content hash |
| `source_session_id` | opaque session id; **nullable** when no session was attached |
| `trigger` | `agent` (default with session), `manual` (default without), `observer`, or `eval` |
| `telemetry` | quality only: runs, modes, zero_hits, warnings, errors, duration_ms |

Legacy records may lack `schema_version`, `trigger`, telemetry or a non-empty
`source_session_id`; readers must load them without error. Do not rename or
remove existing fields.
