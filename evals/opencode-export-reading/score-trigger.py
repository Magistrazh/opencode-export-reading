#!/usr/bin/env python3
"""Score the opencode-export-reading trigger corpus.

The runner writes one `opencode run --format json` transcript per case and
repetition. This script detects whether the transcript contains a `skill` tool
call for `opencode-export-reading` and reports positive recall / negative
false-positive rate.

Usage:
    python score-trigger.py --validate
    python score-trigger.py --selftest
    python score-trigger.py runs/baseline-0.8.11 --write-baseline baselines/trigger-0.8.11.tsv
    python score-trigger.py runs/after-shrink --compare baselines/trigger-0.8.11.tsv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import tempfile
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
CORPUS = ROOT / "trigger.tsv"
SKILL_NAME = "opencode-export-reading"


def read_corpus() -> list[dict]:
    with CORPUS.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    required = {"id", "expect", "language", "category", "prompt"}
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"corpus: missing columns {sorted(missing)}")
        if row["expect"] not in {"yes", "no"}:
            raise SystemExit(f"corpus: bad expect in {row['id']}: {row['expect']!r}")
        if row["language"] not in {"en", "ru"}:
            raise SystemExit(f"corpus: bad language in {row['id']}")
        if not row["prompt"].strip():
            raise SystemExit(f"corpus: empty prompt in {row['id']}")
    return rows


def validate_corpus(rows: list[dict]) -> None:
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("corpus: duplicate ids")
    pos = sum(1 for r in rows if r["expect"] == "yes")
    neg = len(rows) - pos
    ru = sum(1 for r in rows if r["language"] == "ru")
    if len(rows) < 24:
        raise SystemExit(f"corpus: expected >=24 cases, got {len(rows)}")
    if pos < 12 or neg < 12:
        raise SystemExit(f"corpus: expected >=12 positive and >=12 negative, got {pos}/{neg}")
    if ru < 8:
        raise SystemExit(f"corpus: expected >=8 Russian cases, got {ru}")
    print(f"corpus OK: {len(rows)} cases, {pos} positive, {neg} negative, {ru} Russian")


def parse_case(path: Path) -> tuple[str, int] | None:
    match = re.match(r"(.+)\.r(\d+)\.json$", path.name)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def transcript_metrics(path: Path) -> dict:
    invoked = False
    timestamps: list[int] = []
    tokens = 0
    cost = 0.0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = event.get("timestamp")
            if isinstance(ts, int):
                timestamps.append(ts)
            if event.get("type") == "tool_use":
                part = event.get("part") or {}
                if part.get("tool") == "skill":
                    name = ((part.get("state") or {}).get("input") or {}).get("name")
                    if name == SKILL_NAME:
                        invoked = True
            elif event.get("type") == "step_finish":
                part = event.get("part") or {}
                tok = part.get("tokens") or {}
                total = int(tok.get("total") or 0)
                if not total:
                    total = sum(int(tok.get(k) or 0) for k in
                                ("input", "output", "reasoning"))
                tokens += total
                cost += float(part.get("cost") or 0)
        if not invoked:
            invoked = f'<skill_content name="{SKILL_NAME}"' in path.read_text(
                encoding="utf-8", errors="replace")
    except OSError:
        pass
    duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
    return {"invoked": invoked, "duration_ms": duration,
            "tokens": tokens, "cost": cost}


def skill_invoked(path: Path) -> bool:
    return bool(transcript_metrics(path)["invoked"])


def collect(run_dir: Path) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for path in sorted(run_dir.glob("*.json")):
        parsed = parse_case(path)
        if not parsed:
            continue
        cid, rep = parsed
        metrics = transcript_metrics(path)
        entry = cases.setdefault(cid, {"reps": {}, "invoked": False})
        entry["reps"][rep] = metrics
        entry["invoked"] = entry["invoked"] or metrics["invoked"]
    return cases


def summary(rows: list[dict], cases: dict[str, dict]) -> dict:
    pos = [r for r in rows if r["expect"] == "yes"]
    neg = [r for r in rows if r["expect"] == "no"]
    tp = sum(1 for r in pos if cases.get(r["id"], {}).get("invoked"))
    fn = [r["id"] for r in pos if not cases.get(r["id"], {}).get("invoked")]
    fp = [r["id"] for r in neg if cases.get(r["id"], {}).get("invoked")]
    missing = [r["id"] for r in rows if r["id"] not in cases]
    reps = [m for c in cases.values() for m in c["reps"].values()]
    durations = [m["duration_ms"] for m in reps if m.get("duration_ms")]
    return {
        "positive": len(pos), "negative": len(neg),
        "tp": tp, "fn": fn, "fp": fp, "missing": missing,
        "positive_recall": tp / len(pos) if pos else 0.0,
        "negative_fp_rate": len(fp) / len(neg) if neg else 0.0,
        "runs": len(reps),
        "duration_ms_total": sum(durations),
        "duration_ms_median": statistics.median(durations) if durations else 0,
        "tokens": sum(int(m.get("tokens") or 0) for m in reps),
        "cost": sum(float(m.get("cost") or 0) for m in reps),
    }


def print_summary(s: dict) -> None:
    print(f"positive recall: {s['tp']}/{s['positive']} = {s['positive_recall']:.2f}")
    print(f"negative false positives: {len(s['fp'])}/{s['negative']} = {s['negative_fp_rate']:.2f}")
    if s["fn"]:
        print("missed positives: " + ", ".join(s["fn"]))
    if s["fp"]:
        print("false positives: " + ", ".join(s["fp"]))
    if s["missing"]:
        print("missing runs: " + ", ".join(s["missing"]))
    if s.get("runs"):
        print(f"runs: {s['runs']} | duration total/median: "
              f"{s['duration_ms_total']} / {s['duration_ms_median']} ms")
        print(f"tokens: {s['tokens']} | cost: ${s['cost']:.6f}")


def write_baseline(run_dir: Path, path: Path) -> None:
    rows = read_corpus()
    cases = collect(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["id", "expect", "observed", "reps", "invoked"])
        for row in rows:
            entry = cases.get(row["id"], {"reps": {}, "invoked": False})
            writer.writerow([row["id"], row["expect"], "yes" if entry["invoked"] else "no",
                             len(entry["reps"]), int(entry["invoked"])])
    meta = run_dir / "metadata.json"
    if meta.exists():
        target = path.with_suffix(".meta.json")
        target.write_text(meta.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"baseline written: {path}")


def write_benchmark(run_dir: Path) -> None:
    rows = read_corpus()
    cases = collect(run_dir)
    s = summary(rows, cases)
    payload = {
        "stamp": run_dir.name,
        "summary": s,
        "cases": [
            {
                "id": row["id"],
                "expect": row["expect"],
                "invoked": bool(cases.get(row["id"], {}).get("invoked")),
                "reps": [
                    {"rep": rep, **metrics}
                    for rep, metrics in sorted(
                        cases.get(row["id"], {}).get("reps", {}).items())
                ],
            }
            for row in rows
        ],
    }
    (run_dir / "benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    lines = [
        f"# Trigger benchmark: {run_dir.name}", "",
        f"- positive recall: {s['tp']}/{s['positive']} = {s['positive_recall']:.2f}",
        f"- negative false positives: {len(s['fp'])}/{s['negative']} = {s['negative_fp_rate']:.2f}",
        f"- runs: {s['runs']}",
        f"- duration total/median: {s['duration_ms_total']} / {s['duration_ms_median']} ms",
        f"- tokens: {s['tokens']}",
        f"- cost: ${s['cost']:.6f}", "",
        "| id | expect | invoked | reps | duration ms | tokens | cost |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for case in payload["cases"]:
        duration = sum(r["duration_ms"] for r in case["reps"])
        tokens = sum(r["tokens"] for r in case["reps"])
        cost = sum(r["cost"] for r in case["reps"])
        lines.append(f"| {case['id']} | {case['expect']} | "
                     f"{'yes' if case['invoked'] else 'no'} | {len(case['reps'])} | "
                     f"{duration} | {tokens} | {cost:.6f} |")
    (run_dir / "benchmark.md").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8", newline="\n")
    print(f"benchmark written: {run_dir / 'benchmark.json'} and .md")


def compare(run_dir: Path, baseline: Path) -> int:
    current = summary(read_corpus(), collect(run_dir))
    base_rows = {}
    with baseline.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            base_rows[row["id"]] = row
    base_pos = [r for r in read_corpus() if r["expect"] == "yes"]
    base_tp = sum(1 for r in base_pos if base_rows.get(r["id"], {}).get("observed") == "yes")
    base_recall = base_tp / len(base_pos) if base_pos else 0.0
    print_summary(current)
    print(f"baseline positive recall: {base_recall:.2f}")
    delta = current["positive_recall"] - base_recall
    print(f"delta: {delta:+.2f}")
    run_name = "baseline-" + baseline.stem.removeprefix("trigger-")
    base_bench = baseline.parent.parent / "runs" / run_name / "benchmark.json"
    if base_bench.exists():
        base_summary = json.loads(base_bench.read_text(encoding="utf-8"))["summary"]
        print(f"baseline duration/tokens: {base_summary.get('duration_ms_total', 0)} ms / "
              f"{base_summary.get('tokens', 0)}")
        print(f"duration delta: {current['duration_ms_total'] - base_summary.get('duration_ms_total', 0):+d} ms | "
              f"tokens delta: {current['tokens'] - base_summary.get('tokens', 0):+d} | "
              f"cost delta: ${current['cost'] - base_summary.get('cost', 0):+.6f}")
    if delta < -1 / len(base_pos if base_pos else [1]):
        print("GATE FAIL: positive recall dropped by more than one case")
        return 1
    if current["negative_fp_rate"] > 0 and current["negative_fp_rate"] > 0.25:
        print("GATE FAIL: negative false-positive rate above 0.25")
        return 1
    print("GATE PASS")
    return 0


def selftest() -> int:
    checks = failed = 0

    def ok(cond: bool, label: str) -> None:
        nonlocal checks, failed
        checks += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill = root / "trig-01.r1.json"
        skill.write_text("\n".join([
            json.dumps({"type": "tool_use", "timestamp": 1000,
                        "part": {"tool": "skill",
                                 "state": {"input": {"name": SKILL_NAME}}}}),
            json.dumps({"type": "step_finish", "timestamp": 1200,
                        "part": {"tokens": {"total": 10}, "cost": 0.001}}),
            json.dumps({"type": "step_finish", "timestamp": 1500,
                        "part": {"tokens": {"total": 20}, "cost": 0.002}}),
        ]) + "\n", encoding="utf-8")
        no_skill = root / "trig-02.r1.json"
        no_skill.write_text(json.dumps({
            "type": "tool_use",
            "part": {"tool": "bash", "state": {"input": {"command": "ls"}}},
        }) + "\n", encoding="utf-8")
        ok(skill_invoked(skill) is True, "detector finds the skill tool")
        metrics = transcript_metrics(skill)
        ok(metrics["duration_ms"] == 500 and metrics["tokens"] == 30
           and abs(metrics["cost"] - 0.003) < 1e-9,
           "transcript metrics: duration, tokens, cost")
        ok(skill_invoked(no_skill) is False, "detector does not fire on another tool")
        ok(parse_case(skill) == ("trig-01", 1), "run filename parses")
        ok(parse_case(Path("bad.json")) is None, "bad filename ignored")
        rows = [{"id": "trig-01", "expect": "yes"}, {"id": "trig-02", "expect": "no"}]
        cases = collect(root)
        s = summary(rows, cases)
        ok(s["positive_recall"] == 1.0 and s["negative_fp_rate"] == 0.0,
           "summary: one positive, no false positive")
        no_hit = root / "trig-03.r1.json"
        no_hit.write_text(json.dumps({"type": "text", "part": {"text": "hi"}}) + "\n",
                          encoding="utf-8")
        rows2 = [{"id": "trig-03", "expect": "yes"}]
        ok(summary(rows2, collect(root))["positive_recall"] == 0.0,
           "summary: a non-invoking positive is a miss")

    print(f"\n  PASS {checks - failed} | FAIL {failed}")
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Score trigger-corpus runs")
    ap.add_argument("runs", nargs="?", type=Path)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--write-benchmark", action="store_true")
    ap.add_argument("--compare", type=Path)
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    rows = read_corpus()
    if args.validate:
        validate_corpus(rows)
        return 0
    if not args.runs or not args.runs.is_dir():
        ap.error("runs directory is required")
    if args.write_baseline:
        write_baseline(args.runs, args.write_baseline)
        return 0
    if args.write_benchmark:
        write_benchmark(args.runs)
        return 0
    if args.compare:
        return compare(args.runs, args.compare)
    s = summary(rows, collect(args.runs))
    print_summary(s)
    return 0 if not s["missing"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
