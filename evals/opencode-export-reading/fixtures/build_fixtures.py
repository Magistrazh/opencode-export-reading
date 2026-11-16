#!/usr/bin/env python3
"""Build deterministic synthetic exports for the golden set.

No real session data is used. Run from the repository root:

    python evals/opencode-export-reading/fixtures/build_fixtures.py

The generated JSON files are committed; this script exists so they can be
rebuilt and checked without reverse-engineering them.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOW = 1_700_000_000_000


def info(name: str, sanitized: bool = False) -> dict:
    title = "[redacted:session-title:ses_fixture]" if sanitized else f"fixture {name}"
    directory = "[redacted:directory:/fixture]" if sanitized else "fixture/project"
    return {
        "id": "ses_fixture",
        "title": title,
        "directory": directory,
        "agent": "build",
        "version": "1.19.5",
        "time": {"created": NOW, "updated": NOW + 9000},
        "cost": 0,
    }


def user(mid: str, idx: int, text: str) -> dict:
    return {
        "info": {"id": mid, "role": "user", "time": {"created": NOW + idx * 100}},
        "parts": [{"type": "text", "text": text}],
    }


def assistant(mid: str, idx: int, text: str = "") -> dict:
    parts = [{"type": "step-start"}]
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({"type": "step-finish", "cost": 0})
    return {
        "info": {
            "id": mid, "role": "assistant", "time": {"created": NOW + idx * 100},
            "cost": 0, "tokens": {}, "modelID": "fixture-model",
            "providerID": "fixture-provider", "finish": "stop",
        },
        "parts": parts,
    }


def compaction(mid: str, idx: int, tail_id: str | None) -> dict:
    part = {"type": "compaction", "auto": True}
    if tail_id is not None:
        part["tail_start_id"] = tail_id
    return {
        "info": {"id": mid, "role": "user", "time": {"created": NOW + idx * 100}},
        "parts": [part],
    }


def summary(mid: str, idx: int, text: str) -> dict:
    msg = assistant(mid, idx, text)
    msg["info"]["mode"] = "compaction"
    return msg


def write(name: str, messages: list[dict], sanitized: bool = False) -> None:
    data = {"info": info(name, sanitized=sanitized), "messages": messages}
    (ROOT / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    write("normal-compaction", [
        user("m0", 0, "Decision before compaction: use SQLite because it is embedded."),
        user("tail1", 1, "The retained tail starts here."),
        compaction("c1", 2, "tail1"),
        summary("s1", 3, "## Summary\nSQLite was chosen for the project."),
        user("m4", 4, "After compaction: keep SQLite."),
    ])

    write("missing-tail-start-id", [
        user("m0", 0, "Lost fact: use SQLite."),
        user("tail1", 1, "Retained tail."),
        compaction("c1", 2, "missing-id"),
        summary("s1", 3, "## Summary\nA summary with a broken boundary."),
    ])

    write("empty-summary", [
        user("m0", 0, "Lost fact: use SQLite."),
        user("tail1", 1, "Retained tail."),
        compaction("c1", 2, "tail1"),
        summary("s1", 3, ""),
    ])

    write("multiple-compactions", [
        user("m0", 0, "First lost fact: SQLite."),
        user("tail1", 1, "First retained tail."),
        compaction("c1", 2, "tail1"),
        summary("s1", 3, "## Summary\nFirst compaction kept SQLite."),
        user("m4", 4, "Second lost fact: Postgres was rejected."),
        user("tail2", 5, "Second retained tail."),
        compaction("c2", 6, "tail2"),
        summary("s2", 7, "## Summary\nSecond compaction kept the tail."),
        user("m8", 8, "After the second compaction."),
    ])

    partial = assistant("t1", 1, "Intermediate step.")
    partial["info"]["finish"] = "tool-calls"
    cut = assistant("t4", 4, "This turn never finished.")
    cut["info"]["finish"] = "tool-calls"
    write("turns-digest", [
        user("t0", 0, "First request before compaction."),
        partial,
        assistant("t2", 2, "Final answer before compaction."),
        user("t3", 3, "Second request before compaction."),
        cut,
        compaction("c1", 5, "tail1"),
        summary("s1", 6, "## Summary\nBoth requests are covered here."),
        user("tail1", 7, "Retained tail."),
        assistant("t8", 8, "Answer in the tail."),
    ])

    write("contradictory-summary", [
        user("m0", 0, "Work before compaction."),
        compaction("c1", 1, "tail1"),
        summary("s1", 2, "## Summary\nColumns were name,status,created; ran with --count 20."),
        assistant("tail1", 3, "The real order is name,created,status."),
        user("tail2", 4, "Rerun with --count 50."),
    ])

    huge = assistant("m1", 1)
    huge["parts"].append({
        "type": "tool", "tool": "bash",
        "state": {"status": "completed", "title": "generate output",
                  "input": {"command": "fixture"}, "output": "x" * 120_000,
                  "time": {}},
    })
    write("huge-tool-output", [
        user("m0", 0, "Tool output fixture."),
        huge,
    ])

    write("sanitized-export", [
        user("m0", 0, "[redacted:text:prt_fixture]"),
        assistant("m1", 1, "[redacted:text:prt_fixture]"),
    ], sanitized=True)

    manifest = {
        "normal-compaction": {
            "purpose": "one compaction with a lost decision and a retained tail",
            "messages": 5, "compactions": 1,
            "summary_state": "present",
            "search": {"query": "SQLite", "before_tail_hits": 1},
            "exit_codes": {"timeline": 0, "before_tail": 0, "show_summary": 0, "dump": 0},
        },
        "missing-tail-start-id": {
            "purpose": "compaction whose tail_start_id cannot be resolved",
            "messages": 4, "compactions": 1,
            "summary_state": "present",
            "search": {"query": "SQLite", "before_tail_hits": None},
            "exit_codes": {"timeline": 0, "before_tail": 1, "show_summary": 0,
                           "dump": 0, "check_summary": 1},
        },
        "empty-summary": {
            "purpose": "compaction event with an empty summary",
            "messages": 4, "compactions": 1,
            "summary_state": "empty",
            "search": {"query": "SQLite", "before_tail_hits": 1},
            "exit_codes": {"timeline": 0, "before_tail": 0, "show_summary": 0,
                           "dump": 0, "check_summary": 1},
        },
        "multiple-compactions": {
            "purpose": "two compactions; the last boundary is authoritative",
            "messages": 9, "compactions": 2,
            "summary_state": "two present",
            "search": {"query": "lost", "before_tail_hits": 2},
            "exit_codes": {"timeline": 0, "before_tail": 0, "show_summary": 0, "dump": 0},
        },
        "turns-digest": {
            "purpose": "completed and interrupted turns across a compaction boundary",
            "messages": 9, "compactions": 1,
            "summary_state": "present",
            "search": {"query": "First", "before_tail_hits": 1},
            "digest": {"requests": 2, "turn_end": 1, "boundary_index": 7},
            "exit_codes": {"timeline": 0, "before_tail": 0, "show_summary": 0, "dump": 0},
        },
        "contradictory-summary": {
            "purpose": "summary disagrees with the retained tail on order and a flag value",
            "messages": 5, "compactions": 1,
            "summary_state": "present",
            "search": {"query": "order", "before_tail_hits": 0},
            "check_summary": {"warnings": 2, "sequences": 1},
            "exit_codes": {"timeline": 0, "before_tail": 0, "show_summary": 0,
                           "dump": 0, "check_summary": 0},
        },
        "huge-tool-output": {
            "purpose": "one very large tool output for max-chars/truncation",
            "messages": 2, "compactions": 0,
            "summary_state": "none",
            "search": {"query": "Tool", "before_tail_hits": None},
            "exit_codes": {"timeline": 0, "before_tail": 1, "show_summary": 1, "dump": 0},
        },
        "sanitized-export": {
            "purpose": "redacted text with structure retained",
            "messages": 2, "compactions": 0,
            "summary_state": "none",
            "search": {"query": "redacted", "before_tail_hits": None},
            "exit_codes": {"timeline": 0, "before_tail": 1, "show_summary": 1, "dump": 0},
        },
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(list(ROOT.glob('*.json'))) - 1} fixtures + manifest to {ROOT}")


if __name__ == "__main__":
    main()
