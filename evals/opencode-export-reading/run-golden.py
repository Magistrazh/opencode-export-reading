#!/usr/bin/env python3
"""Golden CLI checks for opencode-export-reading.

Runs the real inspect-export.py against deterministic synthetic fixtures and
asserts the contracts recorded in fixtures/manifest.json.

Usage:
    python run-golden.py
    python run-golden.py --selftest
    python run-golden.py --real exports/a.json exports/b.json
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


_utf8_stdio()

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
MANIFEST = FIXTURES / "manifest.json"
SKILL_ROOT = ROOT.parents[1]
FEEDBACK_TMP = Path(tempfile.mkdtemp(prefix="opencode-export-reading-golden-"))
atexit.register(shutil.rmtree, FEEDBACK_TMP, ignore_errors=True)
INSPECT = SKILL_ROOT / "scripts" / "inspect-export.py"


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["SKILL_FEEDBACK_DIR"] = str(FEEDBACK_TMP)
    return subprocess.run(
        [sys.executable, str(INSPECT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env, check=False,
    )


def check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def check_fixture(name: str, expected: dict, failures: list[str]) -> None:
    path = FIXTURES / f"{name}.json"
    print(f"[{name}]")
    check(path.exists() and path.stat().st_size < 200_000,
          "fixture exists and is <200 KB", failures)
    if not path.exists():
        return
    try:
        json.loads(path.read_text(encoding="utf-8"))
        check(True, "fixture is valid JSON", failures)
    except (OSError, json.JSONDecodeError):
        check(False, "fixture is valid JSON", failures)
        return

    proc = run([str(path), "--json"])
    check(proc.returncode == 0, "overview --json exit 0", failures)
    try:
        overview = json.loads(proc.stdout)
    except json.JSONDecodeError:
        check(False, "overview --json parses", failures)
        overview = {}
    check(overview.get("messages", {}).get("total") == expected["messages"],
          f"message count {expected['messages']}", failures)

    proc = run([str(path), "--compactions", "--json"])
    check(proc.returncode == 0, "compactions --json exit 0", failures)
    try:
        comp = json.loads(proc.stdout)
    except json.JSONDecodeError:
        comp = {}
    check(comp.get("compactions") == expected["compactions"],
          f"compaction count {expected['compactions']}", failures)

    codes = expected["exit_codes"]
    check(run([str(path), "--timeline"]).returncode == codes["timeline"],
          "timeline exit code", failures)
    check(run([str(path), "--before-tail", "--search", "x"]).returncode == codes["before_tail"],
          "before-tail exit code", failures)
    check(run([str(path), "--show-summary", "1"]).returncode == codes["show_summary"],
          "summary exit code", failures)
    check(run([str(path), "--dump", "--from-index", "0", "--count", "1"]).returncode == codes["dump"],
          "dump exit code", failures)

    search = expected.get("search", {})
    if search.get("before_tail_hits") is not None:
        proc = run([str(path), "--search", search["query"], "--before-tail"])
        match = re.search(r"hits (\d+)", proc.stdout)
        check(proc.returncode == 0 and match and int(match.group(1)) == search["before_tail_hits"],
              f"before-tail search hits {search['before_tail_hits']}", failures)

    if name == "missing-tail-start-id":
        proc = run([str(path), "--search", "SQLite"])
        check("BEFORE-COMPACTION" not in proc.stdout,
              "unresolved boundary does not fake BEFORE-COMPACTION", failures)
    if name == "empty-summary":
        proc = run([str(path), "--show-summary", "1"])
        check(proc.stdout.strip() == "", "empty summary prints no text", failures)
        check("empty" in proc.stderr.lower(), "empty summary explains itself", failures)
    if name == "huge-tool-output":
        proc = run([str(path), "--dump", "--from-index", "1", "--count", "1", "--max-chars", "100"])
        check("[truncated" in proc.stdout, "large tool output is truncated", failures)
        proc = run([str(path), "--dump", "--from-index", "1", "--count", "1", "--only", "text"])
        check("no text parts" in proc.stderr, "empty --only text explains where the content is", failures)
    if name == "sanitized-export":
        check(bool(overview.get("sanitized")), "sanitized flag detected", failures)


def check_all(manifest_path: Path = MANIFEST) -> list[str]:
    failures: list[str] = []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest.items():
        check_fixture(name, expected, failures)
    return failures


def real(files: list[Path]) -> int:
    failures = 0
    for path in files:
        print(f"[real] {path.name}")
        if not path.exists():
            print("  FAIL  missing")
            failures += 1
            continue
        started = time.time()
        proc = run([str(path), "--timeline"], timeout=120)
        print(f"  timeline rc={proc.returncode} {time.time() - started:.2f}s bytes={len(proc.stdout)}")
        proc = run([str(path), "--compactions", "--json"], timeout=120)
        try:
            comp = json.loads(proc.stdout)
            print(f"  compactions={comp.get('compactions')} rc={proc.returncode}")
        except json.JSONDecodeError:
            print(f"  compactions JSON FAIL rc={proc.returncode}")
            failures += 1
        proc = run([str(path), "--queries", "решение,правило", "--limit", "5"], timeout=120)
        match = re.findall(r"hits (\d+)", proc.stdout)
        print(f"  batch rc={proc.returncode} hits={match} bytes={len(proc.stdout)}")
        proc = run([str(path), "--dump", "--from-index", "0", "--count", "3", "--ids"], timeout=120)
        print(f"  ids rc={proc.returncode} bytes={len(proc.stdout)}")
    return 1 if failures else 0



def build_report() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    records = []
    timing = {}
    for name, expected in manifest.items():
        path = FIXTURES / f"{name}.json"
        started = time.time()
        def add(mode, cond, expected_value, actual_value, evidence,
                name=name, started=started):
            records.append({
                "fixture": name, "mode": mode, "passed": bool(cond),
                "expected": expected_value, "actual": actual_value,
                "evidence": evidence,
                "duration_ms": int((time.time() - started) * 1000),
            })
        proc = run([str(path), "--json"])
        try:
            overview = json.loads(proc.stdout)
        except json.JSONDecodeError:
            overview = {}
        add("overview", overview.get("messages", {}).get("total") == expected["messages"],
            expected["messages"], overview.get("messages", {}).get("total"),
            f"rc={proc.returncode} stdout={len(proc.stdout)}B")
        proc = run([str(path), "--compactions", "--json"])
        try:
            comp = json.loads(proc.stdout)
        except json.JSONDecodeError:
            comp = {}
        add("compactions", comp.get("compactions") == expected["compactions"],
            expected["compactions"], comp.get("compactions"),
            f"rc={proc.returncode} stdout={len(proc.stdout)}B")
        for mode, args in (
            ("timeline", ["--timeline"]),
            ("before_tail", ["--before-tail", "--search", "x"]),
            ("show_summary", ["--show-summary", "1"]),
            ("dump", ["--dump", "--from-index", "0", "--count", "1"]),
        ):
            proc = run([str(path), *args])
            add(mode, proc.returncode == expected["exit_codes"][mode],
                expected["exit_codes"][mode], proc.returncode,
                f"rc={proc.returncode} stdout={len(proc.stdout)}B")
        search = expected.get("search", {})
        if search.get("before_tail_hits") is not None:
            proc = run([str(path), "--search", search["query"], "--before-tail"])
            match = re.search(r"hits (\d+)", proc.stdout)
            actual = int(match.group(1)) if match else None
            add("search", actual == search["before_tail_hits"],
                search["before_tail_hits"], actual,
                f"rc={proc.returncode} stdout={len(proc.stdout)}B")
        if name == "missing-tail-start-id":
            proc = run([str(path), "--search", "SQLite"])
            add("no_fake_marker", "BEFORE-COMPACTION" not in proc.stdout,
                "no BEFORE-COMPACTION", "present" if "BEFORE-COMPACTION" in proc.stdout else "absent",
                f"rc={proc.returncode}")
        if name == "empty-summary":
            proc = run([str(path), "--show-summary", "1"])
            add("empty_summary", proc.stdout.strip() == "", "empty", proc.stdout.strip()[:20],
                f"rc={proc.returncode}")
        if name == "huge-tool-output":
            proc = run([str(path), "--dump", "--from-index", "1", "--count", "1", "--max-chars", "100"])
            add("truncation", "[truncated" in proc.stdout, "truncated output", "present" if "[truncated" in proc.stdout else "absent",
                f"rc={proc.returncode} stdout={len(proc.stdout)}B")
            proc = run([str(path), "--dump", "--from-index", "1", "--count", "1", "--only", "text"])
            add("only_text_note", "no text parts" in proc.stderr, "note", "present" if "no text parts" in proc.stderr else "absent",
                f"rc={proc.returncode}")
        if name == "sanitized-export":
            add("sanitized", bool(overview.get("sanitized")), True, bool(overview.get("sanitized")),
                "overview JSON")
        timing[name] = int((time.time() - started) * 1000)
    passed = sum(1 for r in records if r["passed"])
    return {
        "fixtures": len(manifest),
        "assertions": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "records": records,
        "timing": timing,
    }

def selftest() -> int:
    """Prove the harness fails when an expectation is corrupted."""
    import copy
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    broken = copy.deepcopy(manifest["normal-compaction"])
    broken["search"]["before_tail_hits"] = 999
    failures: list[str] = []
    check_fixture("normal-compaction", broken, failures)
    ok = any("before-tail search hits 999" in f for f in failures)
    print(f"  {'PASS' if ok else 'FAIL'}  corrupted expectation is reported")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Golden checks for opencode-export-reading")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--write-report", type=Path)
    ap.add_argument("--real", nargs="+", type=Path)
    args = ap.parse_args(argv)
    if not INSPECT.exists():
        print(f"ERROR: inspect not found: {INSPECT}", file=sys.stderr)
        return 2
    if args.selftest:
        return selftest()
    if args.write_report:
        report = build_report()
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8", newline="\n")
        timing_path = args.write_report.with_name("timing.json")
        timing_path.write_text(json.dumps(report["timing"], ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
        print(f"report: {args.write_report} ({report['passed']}/{report['assertions']} passed)")
        return 0 if report["failed"] == 0 else 1
    if args.real:
        return real(args.real)
    failures = check_all()
    print(f"\n  PASS {len(json.loads(MANIFEST.read_text(encoding='utf-8'))) - len(failures)}"
          f" | FAIL {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
