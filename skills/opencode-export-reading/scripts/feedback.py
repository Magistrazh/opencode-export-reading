#!/usr/bin/env python3
"""Leave and read short feedback about a skill, written by the agent that used it.

The consumer of a skill is an LLM, so the only moment it can tell us how it went
is right after the task. This tool makes that one call: a few words about what
was asked, whether it worked, what got in the way, and what would help. Records
go to a shared, local folder so every agent on the machine can add to it and a
human can review them later.

Usage records (kind=usage) are written by the skill's own script on every run,
so adoption is captured without asking. Quality records (kind=suggestion) are
short JSON files, one per run: concurrent agents never rewrite each other, and a
corrupt record cannot take the folder down. No conversation content, no paths, no
keys — only the skill's own usefulness.

Usage:
    python feedback.py --task "<what you did>" --solved yes \
        --problems "" --improve "" --modes search,dump
    python feedback.py --task "<what you did>" --solved partial --problems "search missed Russian stemming" \
        --improve "add --stem" --extended "Longer note when it went very well or very badly"
    python feedback.py --stats
    python feedback.py --list 10
    python feedback.py --selftest
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


_utf8_stdio()

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_NAME = SKILL_DIR.name
DEFAULT_ROOT = Path(
    os.environ.get("SKILL_FEEDBACK_DIR", Path.home() / ".local" / "share" / "opencode" / "skill-feedback")
)
SOLVED = ("yes", "partial", "no")
TRIAGE_STATUSES = ("fixed", "accepted", "duplicate", "obsolete", "open")
SCHEMA_VERSION = 1
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LIMITS = {"task": 200, "problems": 300, "improve": 300, "extended": 2000,
          "agent": 60, "modes": 120, "trigger": 60, "reason": 300,
          "release": 20, "commit": 40}
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}"                      # OpenAI-style
    r"|gh[opsu]_[A-Za-z0-9]{20,}"                 # GitHub classic/oauth
    r"|github_pat_[A-Za-z0-9_]{20,}"            # GitHub fine-grained
    r"|sk-(?:ant|proj)-[A-Za-z0-9_-]{10,}"      # Anthropic/project keys
    r"|glpat-[A-Za-z0-9_-]{16,}"                # GitLab
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"            # Slack
    r"|AIza[0-9A-Za-z_-]{35}"                   # Google API key
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"       # AWS
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWT
    r"|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|-----BEGIN [A-Z ]+PRIVATE KEY-----"
    r"|password\s*[=:]\s*\S+"
    r"|[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"           # email
    r"|[A-Za-z]:\\"                             # absolute Windows path
    r"|\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9._-]+"  # UNC path
    r"|/(home|Users)/[A-Za-z0-9._-]+)",         # absolute Unix home path
    re.IGNORECASE,
)


class FeedbackError(RuntimeError):
    pass


def version_info() -> tuple[str, str]:
    """Release from SKILL.md frontmatter plus an exact content hash."""
    skill_md = SKILL_DIR / "SKILL.md"
    release = "unknown"
    digest = "unknown"
    if skill_md.exists():
        try:
            text = skill_md.read_text(encoding="utf-8")
            match = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
            if match:
                release = match.group(1)
            digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()[:8]
        except OSError:
            pass
    return release, digest


def clean(value: str, field: str) -> str:
    text = " ".join((value or "").split()) if field != "extended" else (value or "").strip()
    limit = LIMITS[field]
    if len(text) > limit:
        raise FeedbackError(f"{field}: {len(text)} символов, максимум {limit}")
    if SECRET_RE.search(text):
        raise FeedbackError(f"{field}: похоже на секрет или пароль — уберите")
    return text


def dest_dir(skill: str, root: Path | None = None) -> Path:
    if not SKILL_RE.match(skill) or skill in (".", ".."):
        raise FeedbackError(f"недопустимое имя скилла: {skill!r}")
    base = (root or DEFAULT_ROOT).resolve()
    folder = (base / skill).resolve()
    if not folder.is_relative_to(base):
        raise FeedbackError(f"путь выходит за пределы каталога отзывов: {skill!r}")
    return folder


def write_feedback(args: argparse.Namespace) -> Path | None:
    if args.solved not in SOLVED:
        raise FeedbackError(f"solved: ожидалось одно из {SOLVED}")
    task = clean(args.task, "task")
    if not task:
        raise FeedbackError("task не может быть пустым: одна строка о том, что делали")
    modes = clean(",".join(sorted({m.strip() for m in (args.modes or "").split(",")
                                    if m.strip()})), "modes")
    note = bool(getattr(args, "note", False))
    problems = clean(args.problems, "problems")
    improve = clean(args.improve, "improve")
    if note and not (problems or improve):
        raise FeedbackError("--note требует --problems или --improve: одна строка о том, что пошло не так")
    source = getattr(args, "source_session_id", "") or ""
    usage = session_usage(args.skill, source, args.root)
    if source and not usage:
        raise FeedbackError("нет usage-записей для этой сессии — скрипт не запускался")
    release, digest = version_info()
    if release != "unknown" and not SEMVER_RE.match(release):
        raise FeedbackError(f"skill_version не X.Y.Z: {release!r}")
    trigger = clean(getattr(args, "trigger", "") or "", "trigger")
    if not trigger:
        trigger = "agent" if source else "manual"
    record = {
        "kind": "note" if note else "suggestion",
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "skill": args.skill,
        "skill_version": release,
        "skill_hash": digest,
        "agent": clean(args.agent, "agent"),
        "task": task,
        "solved": args.solved,
        "problems": problems,
        "improve": improve,
        "extended": clean(args.extended, "extended"),
        "modes": modes,
        "trigger": trigger,
        "source_session_id": source or None,
        "used_tool": bool(usage),
        "telemetry": telemetry_summary(usage),
    }
    folder = dest_dir(args.skill, args.root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H%M%S")
    path = folder / f"{stamp}_{uuid.uuid4().hex[:4]}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def load_entries(skill: str, root: Path | None = None) -> list[tuple[str, dict]]:
    """Feedback records with stable ids (file stems); triage/ is not a record."""
    folder = dest_dir(skill, root)
    if not folder.exists():
        return []
    entries = []
    for path in sorted(folder.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(record, dict):
            entries.append((path.stem, record))
    return sorted(entries, key=lambda item: str(item[1].get("ts") or ""))


def load_records(skill: str, root: Path | None = None) -> list[dict]:
    return [record for _record_id, record in load_entries(skill, root)]


def triage_dir(skill: str, root: Path | None = None) -> Path:
    return dest_dir(skill, root) / "triage"


def load_triage(skill: str, root: Path | None = None) -> list[dict]:
    """Append-only triage events; invalid or corrupt files are skipped."""
    folder = triage_dir(skill, root)
    if not folder.exists():
        return []
    events = []
    for path in sorted(folder.glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(event, dict) or event.get("kind") != "triage":
            continue
        if event.get("status") not in TRIAGE_STATUSES:
            continue
        if not RECORD_ID_RE.match(str(event.get("record") or "")):
            continue
        events.append(event)
    return sorted(events, key=lambda event: (str(event.get("ts") or ""),
                                             int(event.get("seq") or 0)))


def resolve_triage(events: list[dict]) -> dict[str, dict]:
    """Last event per record wins; a record without an event is open."""
    state: dict[str, dict] = {}
    for event in events:
        state[str(event["record"])] = event
    return state


def is_actionable(record: dict) -> bool:
    """Only records that ask for a decision; clean 'yes' verdicts are noise."""
    if record.get("kind") == "note":
        return True
    if record.get("kind") == "usage":
        return False
    return bool(record.get("problems") or record.get("improve")
                or record.get("solved") != "yes")


def resolve_record_id(value: str, entries: list[tuple[str, dict]]) -> str:
    record_id = value.strip().removesuffix(".json")
    if not record_id:
        raise FeedbackError("id записи не может быть пустым")
    known = [known_id for known_id, _record in entries]
    if record_id in known:
        return record_id
    matches = [known_id for known_id in known if known_id.startswith(record_id)]
    if not matches:
        raise FeedbackError(f"нет записи с id {record_id!r}")
    if len(matches) > 1:
        shown = ", ".join(matches[:5])
        raise FeedbackError(f"id {record_id!r} неоднозначен: {len(matches)} совпадений ({shown})")
    return matches[0]


def write_triage(args: argparse.Namespace) -> Path:
    status = (args.mark_status or "").strip()
    if status not in TRIAGE_STATUSES:
        raise FeedbackError(f"--as: ожидалось одно из {TRIAGE_STATUSES}")
    reason = clean(args.reason, "reason")
    if not reason:
        raise FeedbackError("--reason обязателен: что сделали и почему")
    release = clean(args.release, "release")
    commit = clean(args.commit, "commit")
    entries = load_entries(args.skill, args.root)
    record_id = resolve_record_id(args.mark, entries)
    stamp = datetime.now().astimezone()
    seq = time.time_ns()
    event = {
        "kind": "triage",
        "schema_version": SCHEMA_VERSION,
        "ts": stamp.isoformat(timespec="microseconds"),
        "seq": seq,
        "skill": args.skill,
        "record": record_id,
        "status": status,
        "reason": reason,
        "release": release or None,
        "commit": commit or None,
    }
    folder = triage_dir(args.skill, args.root)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{stamp.strftime('%Y-%m-%dT%H%M%S_%f')}_{seq}_{uuid.uuid4().hex[:4]}.json"
    path = folder / name
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def load_usage(skill: str, root: Path | None = None) -> list[dict]:
    folder = dest_dir(skill, root) / "usage"
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(out, key=lambda r: r.get("ts", ""))


def quick_defaults(skill: str, root: Path | None = None,
                   window_min: int = 10,
                   source_session_id: str = "") -> dict | None:
    """Task and modes from this session's usage records, so --quick needs no flags."""
    usage = load_usage(skill, root)
    if not usage:
        return None
    cutoff = datetime.now().astimezone() - timedelta(minutes=window_min)
    recent = []
    for u in usage:
        if source_session_id:
            if u.get("source_session_id") != source_session_id:
                continue
            recent.append(u)
            continue
        try:
            dt = datetime.fromisoformat(str(u.get("ts")))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=cutoff.tzinfo)
        if dt >= cutoff:
            recent.append(u)
    if not recent:
        return None
    last = recent[-1]
    mode = str(last.get("mode") or "analysis")
    return {"task": f"inspect export via {mode}", "modes": mode}


def session_usage(skill: str, source_session_id: str,
                  root: Path | None = None) -> list[dict]:
    if not source_session_id:
        return []
    return [u for u in load_usage(skill, root)
            if u.get("source_session_id") == source_session_id]


def telemetry_summary(usage: list[dict]) -> dict:
    return {
        "runs": len(usage),
        "modes": sorted({str(u.get("mode")) for u in usage if u.get("mode")}),
        "zero_hits": sum(1 for u in usage if u.get("zero_hits")),
        "warnings": sum(int(u.get("warnings") or 0) for u in usage),
        "errors": sum(1 for u in usage if u.get("status") == "error"),
        "unverified": sum(int(u.get("unverified") or 0) for u in usage),
        "duration_ms": sum(int(u.get("duration_ms") or 0) for u in usage),
    }


def stats(skill: str, root: Path | None = None) -> dict:
    entries = load_entries(skill, root)
    records = [record for _record_id, record in entries]
    notes = [record for record in records if record.get("kind") == "note"]
    quality = [record for record in records if record.get("kind") != "note"]
    usage = load_usage(skill, root)
    events = load_triage(skill, root)
    known = {record_id for record_id, _record in entries}
    state = {record_id: event for record_id, event in resolve_triage(events).items()
             if record_id in known}
    actionable = [(record_id, record) for record_id, record in entries
                  if is_actionable(record)]
    open_ids = {record_id for record_id, _record in actionable
                if state.get(record_id, {}).get("status") in (None, "open")}
    status_counts = Counter(str(event.get("status")) for event in state.values())
    by_version: dict[str, Counter] = {}
    problems: Counter = Counter()
    improve: Counter = Counter()
    for r in quality:
        v = str(r.get("skill_version") or "?")
        by_version.setdefault(v, Counter())[str(r.get("solved") or "?")] += 1
    for r in records:
        if r.get("problems"):
            problems[" ".join(str(r["problems"]).split())[:160]] += 1
        if r.get("improve"):
            improve[" ".join(str(r["improve"]).split())[:160]] += 1
    return {
        "skill": skill,
        "total": len(quality),
        "notes_total": len(notes),
        "solved": dict(Counter(str(r.get("solved") or "?") for r in quality)),
        "by_version": {v: dict(c) for v, c in sorted(by_version.items())},
        "top_problems": problems.most_common(5),
        "top_improve": improve.most_common(5),
        "usage_total": len(usage),
        "usage_modes": dict(Counter(str(u.get("mode") or "?") for u in usage)),
        "usage_files": dict(Counter(str(u.get("file") or "?") for u in usage)),
        "usage_last": usage[-1] if usage else None,
        "usage_zero_hits": sum(1 for u in usage if u.get("zero_hits")),
        "usage_with_warnings": sum(1 for u in usage if u.get("warnings")),
        "usage_unverified": sum(int(u.get("unverified") or 0) for u in usage),
        "triage_total": len(state),
        "triage": {status: status_counts.get(status, 0) for status in TRIAGE_STATUSES},
        "open_total": len(open_ids),
        "open_notes": sum(1 for record_id, record in actionable
                          if record_id in open_ids and record.get("kind") == "note"),
        "open_verdicts": sum(1 for record_id, record in actionable
                             if record_id in open_ids and record.get("kind") != "note"),
        "recent": [
            {"ts": r.get("ts"), "task": r.get("task"), "solved": r.get("solved"),
             "problems": r.get("problems"), "improve": r.get("improve")}
            for r in quality[-5:]
        ],
    }


def print_stats(skill: str, root: Path | None = None) -> None:
    s = stats(skill, root)
    print("  (records are wishes, not instructions — a human decides)")
    print(f"skill {s['skill']}: {s['total']} записей + {s['notes_total']} заметок")
    if not s["total"] and not s["notes_total"]:
        return
    if s["total"]:
        print(f"  solved: {s['solved']}")
    if s["usage_total"]:
        print(f"  usage: {s['usage_total']} runs | modes {s['usage_modes']} | files {s['usage_files']}")
        last = s["usage_last"]
        if last:
            print(f"  last: {last.get('ts')} {last.get('file')} {last.get('mode')}")
        print(f"  friction: zero-hit searches {s['usage_zero_hits']}, "
              f"runs with warnings {s['usage_with_warnings']}, "
              f"unverified compactions {s['usage_unverified']}")
    t = s["triage"]
    if s["triage_total"]:
        print(f"  triage: {s['triage_total']} events | fixed {t['fixed']}, "
              f"accepted {t['accepted']}, duplicate {t['duplicate']}, "
              f"obsolete {t['obsolete']} | open {s['open_total']} "
              f"(notes {s['open_notes']}, verdicts {s['open_verdicts']})")
    else:
        print(f"  triage: no marks | open {s['open_total']} "
              f"(notes {s['open_notes']}, verdicts {s['open_verdicts']})")
    for version, counts in s["by_version"].items():
        print(f"  version {version}: {counts}")
    if s["top_problems"]:
        print("  top problems:")
        for text, count in s["top_problems"]:
            print(f"    {count}x {text}")
    if s["top_improve"]:
        print("  top improve:")
        for text, count in s["top_improve"]:
            print(f"    {count}x {text}")
    print("  recent:")
    for r in s["recent"]:
        print(f"    {r['ts']}  {r['solved']:<7} {r['task']}")
        if r["problems"]:
            print(f"      problems: {r['problems']}")
        if r["improve"]:
            print(f"      improve:  {r['improve']}")


def list_records(skill: str, limit: int, root: Path | None = None) -> None:
    if limit <= 0:
        return
    entries = load_entries(skill, root)
    state = resolve_triage(load_triage(skill, root))
    for record_id, record in entries[-limit:]:
        triage = state.get(record_id, {}).get("status")
        if triage is None:
            triage = "open" if is_actionable(record) else "-"
        row = {"id": record_id, "triage": triage}
        row.update(record)
        print(json.dumps(row, ensure_ascii=False))


def print_open(skill: str, limit: int, root: Path | None = None) -> None:
    if limit <= 0:
        return
    entries = load_entries(skill, root)
    state = resolve_triage(load_triage(skill, root))
    pending = [(record_id, record) for record_id, record in entries
               if is_actionable(record)
               and state.get(record_id, {}).get("status") in (None, "open")]
    notes = sum(1 for _record_id, record in pending if record.get("kind") == "note")
    print(f"open {len(pending)} (notes {notes}, verdicts {len(pending) - notes}); "
          f"showing {min(limit, len(pending))}")
    for record_id, record in pending[:limit]:
        kind = record.get("kind") or "?"
        version = record.get("skill_version") or "?"
        print(f"{record.get('ts')}  {kind}  v{version}")
        print(f"  id: {record_id}")
        if record.get("task"):
            print(f"  task: {record['task']}")
        if kind != "note" and record.get("solved"):
            print(f"  solved: {record['solved']}")
        if record.get("problems"):
            print(f"  problems: {record['problems']}")
        if record.get("improve"):
            print(f"  improve: {record['improve']}")


def selftest() -> int:
    checks = failed = 0

    def ok(cond: bool, label: str) -> None:
        nonlocal checks, failed
        checks += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    def raises(fn, label: str) -> None:
        nonlocal checks, failed
        checks += 1
        try:
            fn()
        except (FeedbackError, SystemExit):
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label} (ошибки не было)")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def args(**kw):
            base = {"skill": "test-skill", "task": "recover a fact", "solved": "yes",
                    "problems": "", "improve": "", "extended": "", "modes": "search,dump",
                    "agent": "OpenAgent", "note": False, "root": root}
            base.update(kw)
            return argparse.Namespace(**base)

        path = write_feedback(args())
        ok(path.exists(), "запись создана")
        record = json.loads(path.read_text(encoding="utf-8"))
        ok(record["skill"] == "test-skill" and record["solved"] == "yes", "поля на месте")
        ok(record["kind"] == "suggestion", "запись помечена как пожелание, не команда")
        ok(record["schema_version"] == 1, "schema_version записан")
        ok(record["trigger"] == "manual" and record["source_session_id"] is None,
           "trigger default manual, source_session_id nullable")
        ok(bool(record["skill_version"]) and record["skill_version"] != "unknown",
           "версия скилла зафиксирована")
        ok(record["modes"] == "dump,search", "режимы нормализованы")

        ok(len(load_records("test-skill", root)) == 1, "запись читается обратно")
        empty = stats("other-skill", root)
        ok(empty["total"] == 0, "пустой скилл — пустая статистика")

        raises(lambda: write_feedback(args(solved="maybe")), "недопустимый solved отвергнут")
        raises(lambda: write_feedback(args(task="")), "пустая задача отвергнута")
        raises(lambda: write_feedback(args(problems="x" * 301)), "слишком длинный текст отвергнут")
        raises(lambda: write_feedback(args(improve="sk-ABCDEFGH12345678")),
               "похожее на ключ отвергнуто")
        raises(lambda: write_feedback(args(skill="../../escaped")),
               "traversal в --skill отвергнут")
        raises(lambda: write_feedback(args(problems="Bearer abcdefghijklmnopqrstuvwxyz")),
               "Bearer-токен отвергнут")
        raises(lambda: write_feedback(args(problems="mail me at a@example.com")),
               "email отвергнут")
        raises(lambda: write_feedback(args(task=r"check C:\data\file.txt")),
               "абсолютный путь отвергнут")
        raises(lambda: dest_dir(".."), "«..» как имя скилла отвергнут")

        for i in range(3):
            write_feedback(args(solved="partial", problems="search missed a synonym",
                                improve="add stemming"))
        s = stats("test-skill", root)
        ok(s["total"] == 4, "статистика считает все записи")
        ok(s["solved"].get("partial") == 3 and s["solved"].get("yes") == 1,
           "распределение solved верное")
        ok(s["top_problems"][0][1] == 3, "повторяющаяся проблема агрегируется")
        usage_dir = dest_dir("test-skill", root) / "usage"
        usage_dir.mkdir(parents=True, exist_ok=True)
        (usage_dir / "usage-2026-01-01T000000_timeline_abcd.json").write_text(
            json.dumps({"kind": "usage",
                        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "mode": "timeline", "file": "x.json", "zero_hits": True,
                        "warnings": 2, "unverified": 1,
                        "source_session_id": "ses_test"}), encoding="utf-8")
        usage_stats = stats("test-skill", root)
        ok(usage_stats["usage_total"] == 1, "usage-записи считаются отдельно")
        ok(usage_stats["usage_zero_hits"] == 1 and usage_stats["usage_with_warnings"] == 1
           and usage_stats["usage_unverified"] == 1, "телеметрия трения агрегируется")
        sourced = args(source_session_id="ses_test", task="inspect x.json via timeline",
                        modes="timeline")
        sourced_rec = write_feedback(sourced)
        sourced_data = json.loads(sourced_rec.read_text(encoding="utf-8"))
        ok(sourced_data.get("used_tool") is True
           and sourced_data.get("telemetry", {}).get("runs") == 1,
           "телеметрия сессии в отзыве")
        ok(sourced_data.get("trigger") == "agent", "trigger default agent при source")
        triggered = write_feedback(args(trigger="observer", task="inspect x via timeline"))
        triggered_data = json.loads(triggered.read_text(encoding="utf-8"))
        ok(triggered_data["trigger"] == "observer", "trigger сохраняется")
        ok(bool(SEMVER_RE.match("0.9.0")), "semver X.Y.Z принимается")
        ok(not SEMVER_RE.match("0.9") and SEMVER_RE.match("unknown") is None,
           "не-X.Y.Z и unknown не считаются версией")
        raises(lambda: write_feedback(args(source_session_id="ses_missing")),
               "отзыв без usage-записей отвергнут")
        note_rec = write_feedback(args(note=True, source_session_id="ses_test",
                                       task="inspect x.json via search", modes="search",
                                       problems="0 hits on the first query"))
        note_data = json.loads(note_rec.read_text(encoding="utf-8"))
        ok(note_data.get("kind") == "note", "note: отдельный вид записи")
        note_stats = stats("test-skill", root)
        ok(note_stats["total"] == 6 and note_stats["notes_total"] == 1,
           "note не попадает в статистику вердиктов")
        raises(lambda: write_feedback(args(note=True, source_session_id="ses_test")),
               "note без текста отвергнут")
        legacy_root = Path(td) / "legacy-skill"
        legacy_dir = dest_dir("legacy-skill", legacy_root)
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "old.json").write_text(json.dumps({
            "kind": "suggestion", "task": "old", "solved": "yes",
            "source_session_id": "", "skill_version": "139cc43e",
        }), encoding="utf-8")
        legacy_stats = stats("legacy-skill", legacy_root)
        ok(legacy_stats["total"] == 1 and legacy_stats["notes_total"] == 0,
           "legacy-запись без schema/trigger/session читается")
        qd = quick_defaults("test-skill", root, source_session_id="ses_test")
        ok(qd is not None and qd["task"] == "inspect export via timeline"
           and qd["modes"] == "timeline",
           "--quick подставляет режим текущего прогона")
        ok(quick_defaults("no-such-skill", root) is None, "--quick без usage возвращает None")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            list_records("test-skill", 0, root)
        ok(buf.getvalue() == "", "--list 0 не печатает записи")

        # --- triage ledger: append-only, records stay immutable ---
        tcase = Path(td) / "triage-case"
        tdir = dest_dir("triage-skill", tcase)
        tdir.mkdir(parents=True, exist_ok=True)

        def put(record_id: str, payload: dict) -> None:
            (tdir / f"{record_id}.json").write_text(json.dumps(payload), encoding="utf-8")

        put("2026-01-01T000000_aaaa", {"kind": "note", "ts": "2026-01-01T00:00:00+00:00",
                                       "skill_version": "0.10.1", "solved": "yes",
                                       "problems": "0 hits", "task": "inspect via search"})
        put("2026-01-01T000001_bbbb", {"kind": "suggestion", "ts": "2026-01-01T00:00:01+00:00",
                                       "skill_version": "0.10.1", "solved": "yes",
                                       "problems": "", "improve": "", "task": "clean yes"})
        put("2026-01-01T000002_cccc", {"kind": "suggestion", "ts": "2026-01-01T00:00:02+00:00",
                                       "skill_version": "0.10.1", "solved": "partial",
                                       "problems": "missed", "improve": "add", "task": "partial"})

        tstats = stats("triage-skill", tcase)
        ok(tstats["total"] == 2 and tstats["notes_total"] == 1,
           "triage: notes and verdicts counted separately")
        ok(tstats["open_total"] == 2 and tstats["open_notes"] == 1
           and tstats["open_verdicts"] == 1, "triage: clean yes is not actionable")
        ok(resolve_record_id("2026-01-01T000002_cccc.json",
                             load_entries("triage-skill", tcase)) == "2026-01-01T000002_cccc",
           "triage: .json suffix accepted")

        def mark_args(record: str, status: str, reason: str) -> argparse.Namespace:
            return argparse.Namespace(skill="triage-skill", mark=record,
                                      mark_status=status, reason=reason,
                                      release="", commit="", root=tcase)

        tpath = write_triage(mark_args("2026-01-01T000000", "fixed",
                                       "fixed in 0.11.0"))
        ok(tpath.exists(), "triage: event written")
        tstats = stats("triage-skill", tcase)
        ok(tstats["triage"]["fixed"] == 1 and tstats["open_total"] == 1,
           "triage: fixed record leaves the queue")

        write_triage(mark_args("2026-01-01T000000_aaaa", "accepted", "by design"))
        state = resolve_triage(load_triage("triage-skill", tcase))
        ok(state["2026-01-01T000000_aaaa"]["status"] == "accepted",
           "triage: last event wins")

        write_triage(mark_args("2026-01-01T000000_aaaa", "open", "reproduced again"))
        ok(stats("triage-skill", tcase)["open_total"] == 2,
           "triage: explicit open reopens")

        raises(lambda: write_triage(mark_args("no-such-id", "fixed", "x")),
               "triage: unknown id rejected")
        raises(lambda: write_triage(mark_args("2026-01-01T00000", "fixed", "x")),
               "triage: ambiguous prefix rejected")
        raises(lambda: write_triage(mark_args("2026-01-01T000000", "done", "x")),
               "triage: invalid status rejected")
        raises(lambda: write_triage(mark_args("2026-01-01T000000", "fixed", "")),
               "triage: empty reason rejected")
        raises(lambda: write_triage(mark_args("2026-01-01T000000", "fixed",
                                              "see C:\\temp\\x")),
               "triage: path in reason rejected")

        triage_dir("triage-skill", tcase).mkdir(parents=True, exist_ok=True)
        (triage_dir("triage-skill", tcase) / "broken.json").write_text(
            "{not json", encoding="utf-8")
        (triage_dir("triage-skill", tcase) / "bad-status.json").write_text(
            json.dumps({"kind": "triage", "record": "2026-01-01T000001_bbbb",
                        "status": "done", "ts": "2026-01-01T00:00:03+00:00"}),
            encoding="utf-8")
        ok(len(load_triage("triage-skill", tcase)) == 3,
           "triage: corrupt and invalid events ignored")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            list_records("triage-skill", 10, tcase)
        rows = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        ok(bool(rows) and rows[0]["id"] and rows[0]["triage"] in TRIAGE_STATUSES,
           "triage: --list exposes id and status")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_open("triage-skill", 10, tcase)
        text = buf.getvalue()
        ok("open 2" in text and "2026-01-01T000000_aaaa" in text,
           "triage: --open lists the queue")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_open("triage-skill", 0, tcase)
        ok(buf.getvalue() == "", "triage: --open 0 prints nothing")

        same_ts = "2026-01-01T00:00:00+00:00"
        for seq, status, name in ((1, "fixed", "z-old.json"), (2, "open", "a-new.json")):
            (triage_dir("triage-skill", tcase) / name).write_text(
                json.dumps({"kind": "triage", "schema_version": 1, "ts": same_ts,
                            "seq": seq, "record": "2026-01-01T000002_cccc",
                            "status": status, "reason": "same tick"}),
                encoding="utf-8")
        state = resolve_triage(load_triage("triage-skill", tcase))
        ok(state["2026-01-01T000002_cccc"]["status"] == "open",
           "triage: seq breaks same-timestamp ties")

    print(f"\n  PASS {checks - failed} | FAIL {failed}")
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Короткая обратная связь по скиллу")
    ap.add_argument("--skill", default=SKILL_NAME)
    ap.add_argument("--task", default="", help="одна строка: что делали")
    ap.add_argument("--solved", default="yes", help="yes | partial | no")
    ap.add_argument("--problems", default="")
    ap.add_argument("--improve", default="")
    ap.add_argument("--extended", default="", help="развёрнуто, если очень хорошо или очень плохо")
    ap.add_argument("--modes", default="", help="какие режимы использовались")
    ap.add_argument("--agent", default="")
    ap.add_argument("--trigger", default="",
                    help="как скилл был выбран: agent, observer, eval, manual")
    ap.add_argument("--source-session-id", default="",
                    help="сессия-источник; по умолчанию OPENCODE_SESSION_ID")
    ap.add_argument("--note", action="store_true",
                    help="короткая заметка о трении, не итоговый вердикт")
    ap.add_argument("--quick", action="store_true",
                    help="подставить task/modes из последних usage-записей")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list", type=int, metavar="N")
    ap.add_argument("--open", nargs="?", const=20, type=int, metavar="N",
                    help="очередь непомеченных записей (по умолчанию 20)")
    ap.add_argument("--mark", metavar="ID",
                    help="пометить запись: id или уникальный префикс")
    ap.add_argument("--as", dest="mark_status", default="",
                    help="fixed|accepted|duplicate|obsolete|open")
    ap.add_argument("--reason", default="", help="что сделали и почему")
    ap.add_argument("--release", default="", help="версия, где исправлено")
    ap.add_argument("--commit", default="", help="коммит с исправлением")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    modes = (args.stats, args.list is not None, args.open is not None,
             args.mark is not None)
    if sum(modes) > 1:
        ap.error("--stats, --list, --open и --mark взаимоисключающие")
    if args.mark is None and (args.mark_status or args.reason
                              or args.release or args.commit):
        ap.error("--as/--reason/--release/--commit работают только с --mark")
    if args.mark is not None and not args.mark_status:
        ap.error("--mark требует --as fixed|accepted|duplicate|obsolete|open")
    if not args.source_session_id:
        args.source_session_id = os.environ.get("OPENCODE_SESSION_ID", "")
    try:
        if args.quick and args.mark is None:
            defaults = quick_defaults(args.skill, args.root,
                                      source_session_id=args.source_session_id)
            if not defaults:
                raise FeedbackError("--quick: нет usage-записей, укажите --task")
            if not args.task:
                args.task = defaults["task"]
            if not args.modes:
                args.modes = defaults["modes"]
        if args.stats:
            print_stats(args.skill, args.root)
        elif args.list is not None:
            list_records(args.skill, args.list, args.root)
        elif args.open is not None:
            print_open(args.skill, args.open, args.root)
        elif args.mark is not None:
            path = write_triage(args)
            print(f"ok: {path}")
        else:
            path = write_feedback(args)
            print(f"ok: {path}")
        return 0
    except FeedbackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
