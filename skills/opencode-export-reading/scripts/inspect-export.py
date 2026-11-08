#!/usr/bin/env python3
"""Summarise and validate an `opencode export` JSON.

Built for one large export (30+ MB), not for batches: the file is parsed as a
stream, so memory stays near the size of a single message instead of ~8x the
file. Costs and tokens are written at three levels (session, message, step),
tool outputs are what the model saw rather than the raw shell, and compacted
context is still inside the file. The summary answers what a big session is
usually opened for: where the money went, what failed, and what was repeated.

Usage:
    python inspect-export.py <export.json>
    python inspect-export.py --json <export.json> > summary.json
    python inspect-export.py --top 10 <export.json>
    python inspect-export.py --selftest

Exit code is 0 for a valid export (warnings allowed), 1 for a broken one.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


_utf8_stdio()

SCHEMA_VERSION = 1

KNOWN_PARTS = {
    "text", "reasoning", "tool", "file", "step-start", "step-finish",
    "compaction", "snapshot", "patch", "agent",
}
ROLES = {"user", "assistant"}
STATUSES = {"pending", "running", "completed", "error"}


class ExportError(RuntimeError):
    pass


def open_export_text(path: Path):
    """Open JSON written by PowerShell, including UTF-8 BOM and UTF-16."""
    with open(path, "rb") as probe:
        prefix = probe.read(4)
    if prefix.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    return open(path, "r", encoding=encoding)


class JsonStream:
    """Incremental reader for a single top-level JSON object.

    Keeps a sliding window over the file instead of building the whole document:
    peak memory is one decoded value (a message) plus a few MB of buffer.
    """

    def __init__(self, path: Path, chunk: int = 1 << 20):
        self._f = open_export_text(path)
        self._dec = json.JSONDecoder()
        self._chunk = chunk
        self._buf = ""
        self._pos = 0
        self._eof = False

    def close(self) -> None:
        self._f.close()

    def _fill(self) -> bool:
        chunk = self._f.read(self._chunk)
        if not chunk:
            self._eof = True
            return False
        if self._pos:
            self._buf = self._buf[self._pos:]
            self._pos = 0
        self._buf += chunk
        return True

    def _compact(self) -> None:
        if self._pos > 4 * self._chunk:
            self._buf = self._buf[self._pos:]
            self._pos = 0

    def _skip_ws(self) -> None:
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in " \t\r\n":
                self._pos += 1
            if self._pos < len(self._buf) or not self._fill():
                return

    def peek(self) -> str:
        self._skip_ws()
        return self._buf[self._pos] if self._pos < len(self._buf) else ""

    def expect(self, ch: str) -> None:
        self._skip_ws()
        if self._pos >= len(self._buf) or self._buf[self._pos] != ch:
            got = self._buf[self._pos : self._pos + 20] if self._pos < len(self._buf) else "<eof>"
            raise ExportError(f"ожидался {ch!r}, получено {got!r}")
        self._pos += 1
        self._compact()

    def value(self):
        while True:
            self._skip_ws()
            try:
                obj, end = self._dec.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError as exc:
                if not self._fill():
                    raise ExportError(f"не удалось разобрать JSON: {exc}") from exc
                continue
            self._pos = end
            self._compact()
            return obj


def stream_export(path: Path, on_message) -> dict:
    """Parse the export, calling on_message(idx, message) for each message.

    Return False from the callback to stop early. Returns session info (None if
    the scan was stopped before the info block was complete).
    """
    stream = JsonStream(path)
    try:
        stream.expect("{")
        info = None
        idx = 0
        stop = False
        while not stop:
            ch = stream.peek()
            if ch == "}":
                stream.expect("}")
                break
            key = stream.value()
            if not isinstance(key, str):
                raise ExportError("ожидался ключ верхнего уровня")
            stream.expect(":")
            if key == "info":
                info = stream.value()
            elif key == "messages":
                stream.expect("[")
                while stream.peek() != "]":
                    if on_message(idx, stream.value()) is False:
                        stop = True
                        break
                    idx += 1
                    if stream.peek() == ",":
                        stream.expect(",")
                    elif stream.peek() != "]":
                        raise ExportError("ожидался ',' или ']' внутри messages")
                if not stop:
                    stream.expect("]")
            else:
                stream.value()
            if not stop and stream.peek() == ",":
                stream.expect(",")
        if not stop and not isinstance(info, dict):
            raise ExportError("в экспорте нет info")
        return info
    finally:
        stream.close()


def brief(msg: dict) -> str:
    for p in msg.get("parts", []):
        if p.get("type") == "text" and p.get("text"):
            return " ".join(p["text"].split())[:80]
        if p.get("type") == "tool":
            state = p.get("state") or {}
            return f'{p.get("tool")}: {state.get("title") or ""}'.strip()[:80]
    return ""


class Aggregator:
    def __init__(self) -> None:
        self.parts_by_type: Counter = Counter()
        self.models: dict[tuple, dict] = defaultdict(
            lambda: {"messages": 0, "cost": 0.0, "input": 0, "output": 0,
                     "reasoning": 0, "cache_read": 0, "cache_write": 0}
        )
        self.tools: Counter = Counter()
        self.statuses: Counter = Counter()
        self.finish: Counter = Counter()
        self.compactions: Counter = Counter()
        self.files: list[dict] = []
        self.warnings: list[str] = []
        self.biggest: list[tuple[int, str, str]] = []
        self.cost_messages: list[tuple[float, str, str, int, str]] = []
        self.errors: Counter = Counter()
        self.repeats: Counter = Counter()
        self.repeat_display: dict[tuple, str] = {}
        self.agents: dict[str, dict] = defaultdict(lambda: {"messages": 0, "cost": 0.0})
        self.first_ms: int | None = None
        self.last_ms: int | None = None
        self.time_disorder = False
        self.cost_sum = 0.0
        self.tool_bytes = 0
        self.errors_count = 0
        self.users = 0
        self.idx = 0
        self.compaction_events: list[dict] = []
        self.last_compaction_idx: int | None = None
        self.sanitized_marker = False

    @staticmethod
    def _num(value, field: str, idx: int, cast=float):
        try:
            return cast(value or 0)
        except (TypeError, ValueError):
            raise ExportError(f"messages[{idx}]: {field} не число: {value!r}")

    def add(self, idx: int, msg: dict) -> None:
        self.idx = idx + 1
        if not isinstance(msg, dict):
            raise ExportError(f"messages[{idx}] не объект")
        mi, ps = msg.get("info"), msg.get("parts")
        if not isinstance(mi, dict):
            raise ExportError(f"messages[{idx}].info не объект")
        if not isinstance(ps, list):
            raise ExportError(f"messages[{idx}].parts не список")
        role = mi.get("role")
        if role not in ROLES:
            raise ExportError(f"messages[{idx}] неизвестная роль {role!r}")
        created = (mi.get("time") or {}).get("created")
        if not isinstance(created, int):
            raise ExportError(f"messages[{idx}] без time.created (мс)")

        if self.first_ms is None:
            self.first_ms = created
        if self.last_ms is not None and created < self.last_ms:
            self.time_disorder = True
        self.last_ms = created
        if role == "user":
            self.users += 1

        # Части разбираем у обеих ролей: у user лежат вложения и метки
        # компакции, и если считать только assistant — они молча исчезнут.
        for p in ps:
            ptype = p.get("type")
            self.parts_by_type[ptype] += 1
            if ptype not in KNOWN_PARTS:
                self.warnings.append(f"messages[{idx}]: неизвестный тип части {ptype!r}")
            if ptype == "text" and str(p.get("text") or "").startswith("[redacted:"):
                self.sanitized_marker = True
            if ptype == "tool":
                tool = p.get("tool") or "?"
                self.tools[tool] += 1
                state = p.get("state") or {}
                status = state.get("status")
                self.statuses[status] += 1
                if status not in STATUSES:
                    self.warnings.append(f"messages[{idx}]: tool {tool} со статусом {status!r}")
                out = state.get("output") or ""
                self.tool_bytes += len(out)
                self.biggest.append((len(out), tool, str(state.get("title") or "")))
                canon = json.dumps(state.get("input") or {}, sort_keys=True, ensure_ascii=False)[:300]
                repeat_key = (tool, canon)
                self.repeats[repeat_key] += 1
                if repeat_key not in self.repeat_display:
                    label = str(state.get("title") or "").strip() or canon
                    self.repeat_display[repeat_key] = label[:70]
                if status == "error":
                    err = str(state.get("error") or state.get("output") or "").strip()
                    if err:
                        self.errors[(tool, " ".join(err.split())[:120])] += 1
            elif ptype == "file":
                url = p.get("url") or ""
                payload = url.split(",", 1)[1] if url.startswith("data:") and "," in url else url
                approx = len(payload) * 3 // 4 if url.startswith("data:") else len(url)
                self.files.append({
                    "mime": p.get("mime") or "?",
                    "filename": p.get("filename") or "?",
                    "approx_bytes": approx,
                    "is_data_url": url.startswith("data:"),
                })
            elif ptype == "compaction":
                self.compactions["total"] += 1
                if p.get("auto"):
                    self.compactions["auto"] += 1
                if p.get("overflow"):
                    self.compactions["overflow"] += 1
                self.last_compaction_idx = idx
                self.compaction_events.append({
                    "n": len(self.compaction_events) + 1,
                    "idx": idx,
                    "time": created,
                    "auto": bool(p.get("auto")),
                    "overflow": bool(p.get("overflow")),
                    "tail_start_id": p.get("tail_start_id"),
                    "summary_chars": 0,
                    "summary_head": "",
                    "verified": False,
                })

        if role != "assistant":
            return

        key = (mi.get("providerID") or "?", mi.get("modelID") or "?", mi.get("variant") or "-")
        cost = self._num(mi.get("cost"), "cost", idx)
        self.cost_sum += cost
        bucket = self.models[key]
        bucket["messages"] += 1
        bucket["cost"] += cost
        tokens = mi.get("tokens") or {}
        for field in ("input", "output", "reasoning"):
            bucket[field] += self._num(tokens.get(field), f"tokens.{field}", idx, int)
        cache = tokens.get("cache") or {}
        bucket["cache_read"] += self._num(cache.get("read"), "tokens.cache.read", idx, int)
        bucket["cache_write"] += self._num(cache.get("write"), "tokens.cache.write", idx, int)

        if mi.get("mode") == "compaction" and self.compaction_events:
            last_event = self.compaction_events[-1]
            if last_event["idx"] != idx - 1:
                self.warnings.append(
                    f"messages[{idx}]: сводка не примыкает к событию #{last_event['n']}"
                )
            else:
                last_event["verified"] = True
            text = " ".join(
                p.get("text", "") for p in ps if p.get("type") == "text"
            ).strip()
            if text:
                last_event["summary_chars"] = len(text)
                last_event["summary_head"] = " ".join(text.split())[:90]

        agent = str(mi.get("agent") or "?")
        self.agents[agent]["messages"] += 1
        self.agents[agent]["cost"] += cost

        self.finish[mi.get("finish")] += 1
        if mi.get("error"):
            self.errors_count += 1

        steps = [p for p in ps if p.get("type") == "step-finish"]
        if steps:
            step_cost = sum(self._num(p.get("cost"), "step-finish.cost", idx) for p in steps)
            if abs(step_cost - cost) > 1e-9:
                self.warnings.append(
                    f"messages[{idx}] cost {cost} != сумма step-finish {step_cost}"
                )

        total_tokens = int(tokens.get("total") or 0) or sum(
            int(tokens.get(f) or 0) for f in ("input", "output", "reasoning")
        )
        if cost > 0:
            self.cost_messages.append((cost, str(mi.get("id")), f"{key[1]}@{key[2]}",
                                       total_tokens, brief(msg)))

    def result(self, info: dict, name: str, size: int | None, top: int = 5) -> dict:
        warnings = self.warnings
        if self.time_disorder:
            warnings.append("время сообщений не монотонно — порядок мог быть переписан")
        unfinished = self.statuses.get("running", 0) + self.statuses.get("pending", 0)
        if unfinished:
            warnings.append(
                f"tool-вызовов в работе: {unfinished} — экспорт снят во время выполнения, "
                "их вывод неполон"
            )
        if self.first_ms is None:
            warnings.append("в экспорте нет сообщений — сессия пустая или файл обрезан")
        for event in self.compaction_events:
            if not event["verified"]:
                warnings.append(
                    f"компакция #{event['n']} без примыкающей сводки — границы могут быть неточны"
                )

        sess_cost = float(info.get("cost") or 0)
        if abs(sess_cost - self.cost_sum) > max(0.01, abs(sess_cost) * 0.01):
            warnings.append(
                f"info.cost {sess_cost:.6f} != сумма по сообщениям {self.cost_sum:.6f} "
                "(доверять сумме по сообщениям)"
            )

        totals = {
            "cost": self.cost_sum,
            "input": sum(b["input"] for b in self.models.values()),
            "output": sum(b["output"] for b in self.models.values()),
            "reasoning": sum(b["reasoning"] for b in self.models.values()),
            "cache_read": sum(b["cache_read"] for b in self.models.values()),
            "cache_write": sum(b["cache_write"] for b in self.models.values()),
        }
        sess_tokens = info.get("tokens") or {}
        if sess_tokens:
            for field in ("input", "output", "reasoning"):
                if int(sess_tokens.get(field) or 0) != totals[field]:
                    warnings.append(
                        f"info.tokens.{field} {sess_tokens.get(field)} != сумма {totals[field]}"
                    )
            sess_cache = sess_tokens.get("cache") or {}
            for field, total in (("read", totals["cache_read"]), ("write", totals["cache_write"])):
                if sess_cache.get(field) is not None and int(sess_cache.get(field) or 0) != total:
                    warnings.append(
                        f"info.tokens.cache.{field} {sess_cache.get(field)} != сумма {total}"
                    )

        self.biggest.sort(reverse=True)
        self.cost_messages.sort(reverse=True)
        cost_by_agent = [
            {"agent": a, "messages": v["messages"], "cost": v["cost"]}
            for a, v in sorted(self.agents.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        ]
        return {
            "file": name,
            "size_bytes": size,
            "session": {
                "id": info.get("id"),
                "title": info.get("title"),
                "directory": info.get("directory"),
                "agent": info.get("agent"),
                "version": info.get("version"),
                "model_last": info.get("model"),
                "cost_reported": sess_cost,
                "tokens_reported": sess_tokens,
            },
            "messages": {
                "total": self.idx,
                "user": self.users,
                "assistant": self.idx - self.users,
                "with_error": self.errors_count,
                "finish": dict(self.finish),
            },
            "period": {
                "start_ms": self.first_ms,
                "end_ms": self.last_ms,
                "start": local_dt(self.first_ms).isoformat(timespec="seconds")
                          if self.first_ms is not None else None,
                "end": local_dt(self.last_ms).isoformat(timespec="seconds")
                        if self.last_ms else None,
            },
            "sanitized": str(info.get("title") or "").startswith("[redacted:")
                          or str(info.get("directory") or "").startswith("[redacted:")
                          or self.sanitized_marker,
            "models": [
                {"provider": k[0], "model": k[1], "variant": k[2], **v}
                for k, v in sorted(self.models.items(), key=lambda kv: kv[1]["messages"], reverse=True)
            ],
            "totals": totals,
            "parts": dict(self.parts_by_type),
            "tools": {
                "calls": sum(self.tools.values()),
                "statuses": dict(self.statuses),
                "output_bytes": self.tool_bytes,
                "counts": dict(self.tools.most_common()),
                "largest": [{"bytes": n, "tool": t, "title": title}
                            for n, t, title in self.biggest[:top]],
            },
            "files": self.files,
            "compactions": dict(self.compactions),
            "compaction_events": self.compaction_events,
            "last_compaction_idx": self.last_compaction_idx,
            "insights": {
                "top_cost": [
                    {"cost": c, "id": mid, "model": model, "tokens": tok, "brief": text}
                    for c, mid, model, tok, text in self.cost_messages[:top]
                ],
                "errors": [
                    {"tool": t, "error": e, "count": n}
                    for (t, e), n in self.errors.most_common(top)
                ],
                "repeated": [
                    {"tool": t, "call": self.repeat_display[(t, c)], "count": n}
                    for (t, c), n in sorted(self.repeats.items(),
                                            key=lambda item: item[1], reverse=True)[:top]
                    if n > 1
                ],
                "cost_by_agent": cost_by_agent,
            },
            "warnings": warnings,
        }


def analyse_data(data: dict, name: str = "<memory>", size: int | None = None,
                 top: int = 5) -> dict:
    if not isinstance(data, dict) or "info" not in data or "messages" not in data:
        raise ExportError(f"{name}: нет ключей info/messages — это не экспорт сессии")
    if not isinstance(data["messages"], list):
        raise ExportError(f"{name}: messages должен быть списком")
    agg = Aggregator()
    for i, msg in enumerate(data["messages"]):
        agg.add(i, msg)
    return agg.result(data["info"], name, size, top)


def analyse_file(path: Path, top: int = 5) -> dict:
    size = path.stat().st_size
    agg = Aggregator()
    info = stream_export(path, agg.add)
    if info is None:
        raise ExportError("в экспорте нет info — файл неполный")
    return agg.result(info, path.name, size, top)


def iter_fields(msg: dict):
    """Yield (field, text) for everything searchable inside a message."""
    for p in msg.get("parts", []):
        ptype = p.get("type")
        if ptype in ("text", "reasoning"):
            yield ptype, str(p.get("text") or "")
        elif ptype == "tool":
            state = p.get("state") or {}
            yield "tool-title", str(state.get("title") or "")
            yield "tool-input", json.dumps(state.get("input") or {}, ensure_ascii=False, sort_keys=True)
            yield "tool-output", str(state.get("output") or "")
            if state.get("error"):
                yield "tool-error", str(state["error"])
        elif ptype == "file":
            yield "file", str(p.get("filename") or "")


def scan_compactions(path: Path) -> list[dict]:
    """One pass: every compaction event, without resolving tail indexes."""
    events: list[dict] = []
    def on_message(idx: int, msg: dict) -> None:
        for p in msg.get("parts") or []:
            if p.get("type") == "compaction":
                events.append({
                    "n": len(events) + 1,
                    "event_idx": idx,
                    "tail_id": p.get("tail_start_id"),
                    "auto": bool(p.get("auto")),
                    "overflow": bool(p.get("overflow")),
                })
    stream_export(path, on_message)
    return events


def compaction_boundaries(path: Path) -> list[dict]:
    """Compatibility helper: events plus resolved tail indexes."""
    events = scan_compactions(path)
    tails = {e["tail_id"] for e in events if e.get("tail_id")}
    id_to_idx: dict[str, int] = {}
    if tails:
        def map_message(idx: int, msg: dict) -> None:
            mid = (msg.get("info") or {}).get("id")
            if mid in tails:
                id_to_idx[mid] = idx
        stream_export(path, map_message)
    for event in events:
        tail_id = event.get("tail_id")
        event["tail_resolved"] = bool(tail_id) and tail_id in id_to_idx
        event["tail_idx"] = id_to_idx.get(tail_id) if event["tail_resolved"] else None
    return events


def find_hits_many(path: Path, queries: list[str], regex: bool = False,
                   case: bool = False, limit: int = 20, context: int = 80,
                   roles: set[str] | None = None, fields: set[str] | None = None,
                   before_compaction: int | None = None,
                   before_tail: bool = False,
                   from_index: int | None = None,
                   to_index: int | None = None,
                   context_window: int = 0) -> list[dict]:
    """Search several queries in one pass; each gets its own hits and meta."""
    if not queries or any(not q for q in queries):
        raise ExportError("пустой запрос: укажите строку или regex")
    flags = 0 if case else re.IGNORECASE
    patterns = [re.compile(q if regex else re.escape(q), flags) for q in queries]
    hits_by: list[list[dict]] = [[] for _ in queries]
    totals = [0 for _ in queries]
    recent: list[tuple[int, dict]] = []
    pending: list[dict] = []
    cap = max(limit * 10, 200)
    events = scan_compactions(path)
    selected_tail: str | None = None
    selected_seen = False
    tail_id: str | None = None
    tail_idx: int | None = None
    last_event: int | None = None
    if before_compaction is not None:
        if not events:
            raise ExportError("в экспорте нет компакций")
        if before_compaction > len(events):
            raise ExportError(
                f"в экспорте только {len(events)} компакций, запрошена {before_compaction}")
        chosen = events[before_compaction - 1]
        selected_tail = chosen.get("tail_id")
        if not selected_tail:
            raise ExportError(
                f"компакция #{before_compaction}: tail_start_id отсутствует")
        last_event = events[-1]["event_idx"]
    elif before_tail:
        if not events:
            raise ExportError("в экспорте нет компакций — --before-tail неприменим")
        selected_tail = events[-1].get("tail_id")
        if not selected_tail:
            raise ExportError("последняя компакция: tail_start_id отсутствует")
        last_event = events[-1]["event_idx"]
    else:
        last = events[-1] if events else None
        last_event = last["event_idx"] if last else None
        tail_id = last.get("tail_id") if last else None

    def on_message(idx: int, msg: dict):
        nonlocal tail_idx, selected_seen
        if context_window:
            before = list(recent)
            if pending:
                still = []
                for hit in pending:
                    if idx > hit["idx"] and len(hit["context_after"]) < context_window:
                        hit["context_after"].append((idx, msg))
                    if len(hit["context_after"]) < context_window:
                        still.append(hit)
                pending[:] = still
            recent.append((idx, msg))
            if len(recent) > context_window:
                del recent[0]
        mi = msg.get("info") or {}
        mid = mi.get("id")
        if selected_tail is not None:
            if mid == selected_tail:
                selected_seen = True
                tail_idx = idx
                return False
        elif tail_id and mid == tail_id:
            tail_idx = idx
        role = mi.get("role")
        if roles and role not in roles:
            return
        if from_index is not None and idx < from_index:
            return
        if to_index is not None and idx > to_index:
            return
        for qi, pattern in enumerate(patterns):
            for field, text in iter_fields(msg):
                if fields and field not in fields:
                    continue
                match = pattern.search(text or "")
                if not match:
                    continue
                totals[qi] += 1
                if len(hits_by[qi]) < cap:
                    start = max(0, match.start() - context)
                    end = min(len(text), match.end() + context)
                    hit = {
                        "idx": idx,
                        "id": mid,
                        "time": (mi.get("time") or {}).get("created"),
                        "role": role,
                        "model": mi.get("modelID"),
                        "field": field,
                        "snippet": " ".join(text[start:end].split()),
                    }
                    if context_window:
                        hit["context_before"] = before[-context_window:] if before else []
                        hit["context_after"] = []
                        pending.append(hit)
                    hits_by[qi].append(hit)
                break

    stream_export(path, on_message)
    if selected_tail is not None and not selected_seen:
        raise ExportError(
            "tail_start_id выбранной компакции не найден — граница не определена")
    if selected_tail is not None or tail_id and tail_idx is not None:
        boundary = tail_idx
        last_boundary = tail_idx
    else:
        boundary = None
        last_boundary = None
    kind = ("compaction_tail" if before_compaction is not None
            else ("tail_start_id" if selected_tail is not None
                  else ("tail_start_id" if tail_id and tail_idx is not None
                        else ("missing_tail" if (tail_id or last_event is not None)
                              else None))))
    results = []
    for query, hits, total in zip(queries, hits_by, totals):
        for hit in hits:
            hit["before_last_compaction"] = (last_boundary is not None
                                             and hit["idx"] < last_boundary)
            if before_compaction is not None:
                hit["before_selected"] = (boundary is not None
                                          and hit["idx"] < boundary)
        results.append({
            "query": query,
            "hits": hits,
            "meta": {
                "total": total,
                "stored": len(hits),
                "capped": total > len(hits),
                "last_compaction": last_event,
                "boundary": boundary,
                "boundary_kind": kind,
                "selected_compaction": before_compaction,
                "before_tail": before_tail,
            },
        })
    return results


def find_hits(path: Path, query: str, regex: bool = False, case: bool = False,
              limit: int = 20, context: int = 80, roles: set[str] | None = None,
              fields: set[str] | None = None,
              before_compaction: int | None = None,
              before_tail: bool = False,
              from_index: int | None = None,
              to_index: int | None = None,
              context_window: int = 0) -> tuple[list[dict], dict]:
    result = find_hits_many(path, [query], regex=regex, case=case, limit=limit,
                            context=context, roles=roles, fields=fields,
                            before_compaction=before_compaction,
                            before_tail=before_tail,
                            from_index=from_index, to_index=to_index,
                            context_window=context_window)[0]
    return result["hits"], result["meta"]


def cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … [truncated {len(text) - limit} chars]"


def format_message(idx: int, msg: dict, max_chars: int = 3000,
                   ids_only: bool = False, only: set[str] | None = None) -> str:
    mi = msg.get("info") or {}
    created = (mi.get("time") or {}).get("created")
    when = stamp(created)
    head = f"--- #{idx} {mi.get('id') or '?'} {when} {mi.get('role')}"
    if mi.get("modelID"):
        head += f" · {mi.get('modelID')}"
    if mi.get("mode") == "compaction":
        head += " · COMPACTION SUMMARY"
    lines = [head]
    if ids_only:
        return head
    for p in msg.get("parts", []):
        ptype = p.get("type")
        if only and ptype not in only:
            continue
        if ptype in ("text", "reasoning"):
            text = cut(p.get("text") or "", max_chars)
            if text:
                lines.append(f"[{ptype}] {text}")
        elif ptype == "tool":
            state = p.get("state") or {}
            lines.append(f"[tool {p.get('tool')}] {state.get('status')} {state.get('title') or ''}")
            payload = json.dumps(state.get("input") or {}, ensure_ascii=False, sort_keys=True)
            if payload and payload != "{}":
                lines.append(f"  input: {cut(payload, max_chars)}")
            if state.get("output"):
                lines.append(f"  output: {cut(str(state['output']), max_chars)}")
            if state.get("error"):
                lines.append(f"  error: {cut(str(state['error']), max_chars)}")
        elif ptype == "file":
            lines.append(f"[file {p.get('mime')}] {p.get('filename')}")
        elif ptype == "compaction":
            lines.append(
                f"[compaction] tail_start_id={p.get('tail_start_id')} "
                f"auto={p.get('auto')} overflow={p.get('overflow')}"
            )
    return "\n".join(lines)


def dump_range(path: Path, from_id: str | None, to_id: str | None, count: int | None,
               max_chars: int = 3000, from_index: int | None = None,
               to_index: int | None = None, allow_all: bool = False,
               ids_only: bool = False, only: set[str] | None = None) -> list[str]:
    if from_index is None and from_id is None and count is None:
        raise ExportError(
            "укажите начало (--from-index/--from-id) или --count: "
            "иначе будет выгружен весь файл"
        )
    if (from_index is not None or from_id is not None) and count is None             and to_index is None and to_id is None and not allow_all:
        raise ExportError(
            "указано начало без конца: добавьте --count N, --to-* или --all"
        )
    if from_index is not None and to_index is not None and to_index < from_index:
        raise ExportError("--to-index меньше --from-index")
    out: list[str] = []
    started = from_index is None and from_id is None
    taken = 0

    def on_message(idx: int, msg: dict):
        nonlocal started, taken
        mid = (msg.get("info") or {}).get("id")
        if not started:
            if from_index is not None:
                if idx < from_index:
                    return
                started = True
            elif mid != from_id:
                return
            else:
                started = True
        out.append(format_message(idx, msg, max_chars, ids_only=ids_only, only=only))
        taken += 1
        if to_index is not None and idx >= to_index:
            return False
        if to_id and mid == to_id:
            return False
        if count is not None and taken >= count:
            return False
        return None

    stream_export(path, on_message)
    return out


def paginate(items: list, offset: int, limit: int) -> tuple[list, int]:
    total = len(items)
    start = max(0, offset)
    return items[start : start + limit], total


def build_timeline(path: Path) -> list[dict]:
    """Compact map of the session: one window per compaction boundary."""
    windows: list[dict] = []

    def blank() -> dict:
        return {"messages": 0, "user": 0, "assistant": 0, "cost": 0.0,
                "tools": Counter(), "first_user": "", "start": None, "end": None,
                "from_idx": None, "to_idx": None, "compaction": None}

    current = blank()

    def flush(compaction: dict | None) -> None:
        nonlocal current
        if current["messages"]:
            current["compaction"] = compaction
            windows.append(current)
        current = blank()

    def on_message(idx: int, msg: dict) -> None:
        mi = msg.get("info") or {}
        created = (mi.get("time") or {}).get("created")
        current["start"] = current["start"] or created
        current["end"] = created
        if current["from_idx"] is None:
            current["from_idx"] = idx
        current["to_idx"] = idx
        current["messages"] += 1
        role = mi.get("role")
        if role == "user":
            current["user"] += 1
            if not current["first_user"]:
                for p in msg.get("parts") or []:
                    if p.get("type") == "text" and p.get("text"):
                        current["first_user"] = " ".join(p["text"].split())[:80]
                        break
        else:
            current["assistant"] += 1
            current["cost"] += float(mi.get("cost") or 0)
        for p in msg.get("parts") or []:
            if p.get("type") == "tool":
                current["tools"][p.get("tool") or "?"] += 1
            elif p.get("type") == "compaction":
                flush({"auto": bool(p.get("auto")), "overflow": bool(p.get("overflow")),
                       "tail_start_id": p.get("tail_start_id")})

    stream_export(path, on_message)
    flush(None)
    return windows


def show_summary(path: Path, number: int) -> str:
    seen = 0
    text = ""

    def on_message(idx: int, msg: dict):
        nonlocal seen, text
        if (msg.get("info") or {}).get("mode") != "compaction":
            return
        seen += 1
        if seen != number:
            return
        text = "\n\n".join(
            p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"
        ).strip()
        return False

    stream_export(path, on_message)
    if seen < number:
        raise ExportError(f"в экспорте только {seen} сводок компакции, запрошена {number}")
    return text


def list_exports(directory: Path, as_json: bool = False) -> int:
    """List export candidates in a directory, newest first."""
    if not directory.exists():
        raise ExportError(f"каталог не найден: {directory}")
    if not directory.is_dir():
        raise ExportError(f"не каталог: {directory}")
    try:
        files = sorted(directory.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for path in files:
            st = path.stat()
            rows.append({
                "file": path.name,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            })
    except OSError as exc:
        raise ExportError(f"не удалось прочитать каталог: {exc}") from exc
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"exports in {directory}: {len(rows)}")
    for row in rows:
        print(f"  {row['file']}  {row['size_bytes'] / 1024**2:7.1f} MB  {row['mtime']}")
    return 0


def summary_outline(text: str) -> list[str]:
    """Heading-like lines of a compaction summary, with line numbers."""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if (s.startswith("#")
                or re.match(r"^\*\*[^*]{1,80}\*\*:?$", s)
                or re.match(r"^\d{1,2}[.)]\s+\S", s)):
            out.append(f"{i:4} {s}")
    if not out:
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if 0 < len(s) <= 80 and s.endswith(":"):
                out.append(f"{i:4} {s}")
    return out


def summary_grep(text: str, query: str, regex: bool = False, case: bool = False,
                 exclude_paths: bool = False) -> list[tuple[int, str]]:
    flags = 0 if case else re.IGNORECASE
    pattern = re.compile(query if regex else re.escape(query), flags)
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        matches = list(pattern.finditer(line))
        if not matches:
            continue
        if exclude_paths and all(match_in_path(line, m.start(), m.end())
                                 for m in matches):
            continue
        found.append((i, line))
    return found


def match_in_path(line: str, start: int, end: int) -> bool:
    """True when the match sits inside a path-like token (heuristic)."""
    for token in re.finditer(r"[^\s\"'`]+", line):
        if token.start() <= start and end <= token.end():
            text = token.group(0).strip("()[]{}<>,;:\"'`")
            return ("/" in text or "\\" in text
                    or re.match(r"^[A-Za-z]:", text) is not None)
    return False


def summary_diff(a: str, b: str, label_a: str = "A", label_b: str = "B") -> list[str]:
    return list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile=f"summary {label_a}", tofile=f"summary {label_b}",
        lineterm="",
    ))


def local_dt(ms: int) -> datetime:
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        dt = datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return dt.astimezone()
    except (OSError, OverflowError):
        return dt


def split_ref(value: str | None, index: int | None) -> tuple[str | None, int | None]:
    """'#209' / '209' → index; a real message id stays an id."""
    if index is not None or value is None:
        return value, index
    v = value.strip().removeprefix("#")
    if v.isdigit():
        return None, int(v)
    return value, None


def stamp(ms: int | None) -> str:
    if ms is None:
        return "?"
    return local_dt(ms).strftime("%Y-%m-%d %H:%M %z")


def skill_name() -> str:
    return Path(__file__).resolve().parents[1].name


def skill_meta() -> tuple[str, str]:
    """Release from SKILL.md frontmatter plus an exact content hash."""
    md = Path(__file__).resolve().parents[1] / "SKILL.md"
    release = "unknown"
    digest = "unknown"
    try:
        text = md.read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
        if match:
            release = match.group(1)
        digest = hashlib.sha256(md.read_bytes()).hexdigest()[:8]
    except OSError:
        pass
    return release, digest


def mode_name(args: argparse.Namespace) -> str:
    if args.search is not None or args.queries or args.queries_file:
        return "search"
    if args.around is not None:
        return "around"
    if args.dump:
        return "dump"
    if args.timeline:
        return "timeline"
    if args.compactions:
        return "compactions"
    if args.show_summary is not None:
        return "summary"
    if args.summary_outline is not None:
        return "summary-outline"
    if args.summary_grep is not None:
        return "summary-grep"
    if args.summary_diff:
        return "summary-diff"
    return "overview"


def record_usage(mode: str, path: Path, size: int, started: float,
                 root: Path | None = None, extra: dict | None = None) -> Path | None:
    """One small record per run: what was used, on which file, how long."""
    try:
        base = Path(root or os.environ.get(
            "SKILL_FEEDBACK_DIR",
            Path.home() / ".local" / "share" / "opencode" / "skill-feedback",
        ))
        folder = base / skill_name() / "usage"
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        # One process is one run. Do not deduplicate by file+mode: two distinct
        # searches within a minute are two runs, and the second may be the one
        # that hit zero and exposed friction.
        record = {
            "kind": "usage",
            "ts": now.astimezone().isoformat(timespec="seconds"),
            "skill": skill_name(),
            "schema_version": SCHEMA_VERSION,
            "skill_version": skill_meta()[0],
            "skill_hash": skill_meta()[1],
            "file": path.name,
            "mode": mode,
            "size_bytes": size,
            "duration_ms": int((time.time() - started) * 1000),
            "source_session_id": os.environ.get("OPENCODE_SESSION_ID") or None,
        }
        if extra:
            record.update(extra)
        out = folder / f"usage-{stamp}_{mode}_{uuid.uuid4().hex[:4]}.json"
        out.write_text(json.dumps(record, ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")
        return out
    except OSError as exc:
        print(f"usage: not recorded: {exc}", file=sys.stderr)
        return None


def feedback_hint(mode: str, path: Path) -> str:
    fb = Path(__file__).resolve().with_name("feedback.py")
    return (f'feedback: before you answer, run: python "{fb}" '
            f'--skill {skill_name()} --quick --solved yes')


def friction_of(extra: dict) -> list[str]:
    friction: list[str] = []
    if extra.get("zero_hits"):
        friction.append("0 hits")
    if extra.get("warnings"):
        friction.append(f"{extra['warnings']} warning(s)")
    if extra.get("status") == "error":
        friction.append(f"error {extra.get('error_type') or 'unknown'}")
    return friction


def friction_hint(mode: str, path: Path, friction: list[str]) -> str:
    fb = Path(__file__).resolve().with_name("feedback.py")
    if friction == ["0 hits"]:
        return ("0 hits: if expected (term absent), ignore this line; "
                "if unexpected, record it now:\n"
                f'  python "{fb}" --skill {skill_name()} --quick --note '
                f'--problems "what was wrong"')
    return (f"friction ({', '.join(friction)}): if unexpected, record it now:\n"
            f'  python "{fb}" --skill {skill_name()} --quick --note '
            f'--problems "what was wrong"')


def money(value: float) -> str:
    return f"${value:.4f}" if abs(value) < 10 else f"${value:.2f}"


def tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def safe_line(value: object) -> str:
    text = str(value)
    return "".join(ch if ch in "\n\t" or 31 < ord(ch) != 127 else "?" for ch in text)


def human(summary: dict, brief: bool = False) -> str:
    s = summary
    out: list[str] = []
    size = f" ({s['size_bytes'] / 1024**2:.1f} MB)" if s["size_bytes"] else ""
    out.append(f"file        {s['file']}{size}")
    sess = s["session"]
    out.append(f"session     {sess['id']}  {sess['title']!r}")
    out.append(f"directory   {sess['directory']}")
    out.append(f"agent/ver   {sess['agent']} / {sess['version']}")
    if s["period"]["start_ms"] is not None:
        out.append(f"period      {s['period']['start']} .. {s['period']['end']}")
    else:
        out.append("period      — (сообщений нет)")
    out.append(f"sanitized   {'yes — только счётчики и цифры' if s['sanitized'] else 'no'}")
    m = s["messages"]
    out.append(
        f"messages    {m['total']} (user {m['user']}, assistant {m['assistant']}, "
        f"errors {m['with_error']})"
    )
    if not brief:
        out.append(f"finish      {m['finish']}")
        out.append(f"models      {len(s['models'])} (session.model is only the last one)")
        for mod in s["models"]:
            out.append(
                f"  {mod['provider']}/{mod['model']}@{mod['variant']:<8} "
                f"{mod['messages']:>5} msg  {money(mod['cost']):>10}  "
                f"in {tokens(mod['input'])}  out {tokens(mod['output'])}  "
                f"reason {tokens(mod['reasoning'])}  cache-r {tokens(mod['cache_read'])}"
            )
        t = s["totals"]
        out.append(
            f"totals      {money(t['cost'])}  in {tokens(t['input'])}  out {tokens(t['output'])}  "
            f"reason {tokens(t['reasoning'])}  cache-r {tokens(t['cache_read'])}  "
            f"cache-w {tokens(t['cache_write'])}"
        )
        out.append(f"parts       {s['parts']}")
        tools = s["tools"]
        out.append(
            f"tools       {tools['calls']} calls, {tools['output_bytes'] / 1024**2:.1f} MB output, "
            f"statuses {tools['statuses']}"
        )
        out.append(f"  counts    {dict(list(tools['counts'].items())[:10])}")
        for item in tools["largest"][:3]:
            out.append(f"  big       {item['bytes'] / 1024:.0f} KB  {item['tool']}  {safe_line(item['title'])[:70]}")
        if s["files"]:
            mimes = Counter(f["mime"] for f in s["files"])
            total = sum(f["approx_bytes"] for f in s["files"])
            out.append(f"files       {len(s['files'])} ({total / 1024**2:.2f} MB decoded): {dict(mimes)}")
    events = s["compaction_events"]
    if events:
        out.append(f"compactions {len(events)} (последняя #{events[-1]['n']}; "
                   f"всё до неё в текущем контексте отсутствует)")
        for e in events:
            when = stamp(e["time"])
            kind = "auto" if e["auto"] else ("overflow" if e["overflow"] else "manual")
            out.append(
                f"  #{e['n']} {when} {kind:<8} summary {e['summary_chars'] / 1024:5.1f} KB  "
                f"tail {e['tail_start_id']}"
            )
        out.append("  история до последней компакции: --search QUERY --before-tail "
                   "/ --show-summary N")
    else:
        out.append("compactions none")
    if not brief:
        ins = s["insights"]
        if ins["top_cost"]:
            out.append("where the money went")
            for item in ins["top_cost"]:
                out.append(
                    f"  {money(item['cost']):>10}  {item['model']:<28} {tokens(item['tokens']):>6} tok  "
                    f"{safe_line(item['brief'])}"
                )
        if ins["errors"]:
            out.append("tool errors")
            for item in ins["errors"]:
                out.append(f"  {item['count']:>3}x {item['tool']:<10} {safe_line(item['error'])[:90]}")
        if ins["repeated"]:
            out.append("repeated calls")
            for item in ins["repeated"]:
                out.append(f"  {item['count']:>3}x {item['tool']:<10} {safe_line(item['call'])[:90]}")
        if len(ins["cost_by_agent"]) > 1:
            agents = ", ".join(
                f"{a['agent']} {money(a['cost'])} ({a['messages']})" for a in ins["cost_by_agent"][:5]
            )
            out.append(f"by agent    {agents}")
    if s["warnings"]:
        out.append("warnings")
        for w in s["warnings"]:
            out.append(f"  - {w}")
    else:
        out.append("warnings    none")
    return "\n".join(out)


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
        except ExportError:
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label} (ошибки не было)")

    info = {
        "id": "ses_test", "title": "t", "directory": "C:/x", "agent": "build", "version": "1.19.5",
        "cost": 0.03,
        "tokens": {"input": 300, "output": 60, "reasoning": 10, "cache": {"read": 100, "write": 0}},
        "model": {"id": "m2", "providerID": "p", "variant": "max"},
    }

    def assistant(mid, cost, model, extra_parts=(), **extra):
        tokens = {"total": 100, "input": 150, "output": 30, "reasoning": 5,
                  "cache": {"read": 50, "write": 0}}
        base = {"id": mid, "role": "assistant", "time": {"created": 1000 + len(mid)},
                "cost": cost, "tokens": tokens, "modelID": model, "providerID": "p",
                "finish": "tool-calls", "agent": "OpenAgent"}
        base.update(extra)
        parts = [{"type": "step-start"}, *extra_parts, {"type": "step-finish", "cost": cost}]
        return {"info": base, "parts": parts}

    data = {
        "info": info,
        "messages": [
            {"info": {"id": "u1", "role": "user", "time": {"created": 900}},
             "parts": [{"type": "text", "text": "hi"}]},
            assistant("a1", 0.01, "m1", extra_parts=[
                {"type": "reasoning", "text": "r"},
                {"type": "tool", "tool": "bash",
                 "state": {"status": "completed", "output": "x" * 100, "input": {"command": "ls"},
                           "time": {}}},
            ]),
            assistant("a2", 0.02, "m2", extra_parts=[
                {"type": "text", "text": "second answer"},
                {"type": "file", "mime": "image/png", "filename": "i.png",
                 "url": "data:image/png;base64," + "A" * 400},
            ], error={"name": "Aborted"}),
        ],
    }
    s = analyse_data(data, "test.json", size=1234)
    ok(s["messages"] == {"total": 3, "user": 1, "assistant": 2, "with_error": 1,
                         "finish": {"tool-calls": 2}}, "счётчики сообщений и ошибок")
    ok(len(s["models"]) == 2 and s["models"][0]["messages"] == 1, "разбивка по моделям")
    ok(abs(s["totals"]["cost"] - 0.03) < 1e-9, "сумма cost сходится с session.info")
    ok(s["tools"]["calls"] == 1 and s["tools"]["output_bytes"] == 100, "tool: вызов и байты вывода")
    ok(s["tools"]["statuses"] == {"completed": 1}, "tool: статусы")
    ok(len(s["files"]) == 1 and s["files"][0]["mime"] == "image/png", "вложение распознано")
    ok(s["files"][0]["approx_bytes"] == 300, "оценка размера data URL (base64 → байты)")
    ok(s["compactions"] == {}, "без компакций — пустая карта")
    ok(s["warnings"] == [], "валидный экспорт без предупреждений")
    ok(len(s["insights"]["top_cost"]) == 2 and s["insights"]["top_cost"][0]["cost"] == 0.02,
       "самые дорогие сообщения отсортированы")
    ok(s["insights"]["top_cost"][0]["brief"] == "second answer",
       "у дорогого сообщения есть краткое описание")
    full = human(s)
    brief = human(s, brief=True)
    ok("models" in full and "totals" in full and "tools" in full
       and "where the money went" in full, "full overview keeps cost/model/tool tables")
    ok("models" not in brief and "totals" not in brief and "tools" not in brief
       and "where the money went" not in brief,
       "brief overview drops cost/model/tool tables")
    ok("messages" in brief and "compactions" in brief and "warnings" in brief,
       "brief overview keeps metadata, compactions and warnings")

    raises(lambda: analyse_data({"info": {}, "messages": {}}), "messages не список — отказ")
    raises(lambda: analyse_data({"messages": []}), "нет info — отказ")
    bad_role = {"info": info, "messages": [{"info": {"id": "x", "role": "system",
                                                     "time": {"created": 1}}, "parts": []}]}
    raises(lambda: analyse_data(bad_role), "неизвестная роль — отказ")
    bad_time = {"info": info, "messages": [{"info": {"id": "x", "role": "user"},
                                            "parts": []}]}
    raises(lambda: analyse_data(bad_time), "нет time.created — отказ")
    mismatch_msg = assistant("a1", 0.05, "m1")
    mismatch_msg["parts"][-1]["cost"] = 0.01
    mismatch_summary = analyse_data({"info": info, "messages": [mismatch_msg]})
    ok(any("step-finish" in w for w in mismatch_summary["warnings"]),
       "cost != сумма step-finish — предупреждение, не отказ")
    unknown = {"info": info, "messages": [assistant("a1", 0.01, "m1",
                                                     extra_parts=[{"type": "mystery"}])]}
    ok(any("неизвестный тип" in w for w in analyse_data(unknown)["warnings"]),
       "неизвестный тип части — предупреждение, не отказ")
    stale = {"info": dict(info, cost=9.0), "messages": [assistant("a1", 0.01, "m1")]}
    ok(any("info.cost" in w for w in analyse_data(stale)["warnings"]),
       "расхождение session.cost — предупреждение")

    empty = {"info": dict(info, cost=0, tokens={}), "messages": []}
    empty_summary = analyse_data(empty)
    ok(empty_summary["messages"]["total"] == 0
       and any("нет сообщений" in w for w in empty_summary["warnings"]),
       "пустая сессия — предупреждение, а не отказ")
    sanitized = {"info": dict(info, title="[redacted:session-title:ses_test]"),
                 "messages": [assistant("a1", 0.01, "m1")]}
    ok(analyse_data(sanitized)["sanitized"] is True, "санитизированный экспорт распознан")
    running = {"info": info, "messages": [assistant("a1", 0.01, "m1", extra_parts=[
        {"type": "tool", "tool": "bash",
         "state": {"status": "running", "output": "", "input": {}, "time": {}}},
    ])]}
    ok(any("в работе" in w for w in analyse_data(running)["warnings"]),
       "незавершённый tool-вызов — предупреждение о неполном выводе")

    # --- потоковое чтение: тот же результат, что и разбор готового объекта ---
    with tempfile.TemporaryDirectory() as td:
        cli_fb = Path(td) / "cli-feedback"

        def run_cli(argv):
            old = os.environ.get("SKILL_FEEDBACK_DIR")
            os.environ["SKILL_FEEDBACK_DIR"] = str(cli_fb)
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        rc = main(argv)
                    except SystemExit as exc:
                        rc = int(exc.code or 0)
            finally:
                if old is None:
                    os.environ.pop("SKILL_FEEDBACK_DIR", None)
                else:
                    os.environ["SKILL_FEEDBACK_DIR"] = old
            return rc, out.getvalue(), err.getvalue()

        path = Path(td) / "synthetic.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        streamed = analyse_file(path, top=5)
        ok({k: v for k, v in streamed.items() if k not in ("file", "size_bytes")}
           == {k: v for k, v in s.items() if k not in ("file", "size_bytes")},
           "потоковый разбор == разбор из памяти")
        ok(streamed["messages"]["total"] == 3, "потоковое чтение видит все сообщения")
        bom = Path(td) / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8"))
        ok(analyse_file(bom)["messages"]["total"] == 3, "UTF-8 BOM читается")
        utf16 = Path(td) / "utf16.json"
        utf16.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-16"))
        ok(analyse_file(utf16)["messages"]["total"] == 3, "UTF-16 экспорт читается")
        ok(local_dt(10**22).year == 1970, "громадный timestamp не роняет CLI")
        raises(lambda: find_hits(path, "hi", before_tail=True),
               "before-tail без компакций — отказ")

        broken = Path(td) / "broken.json"
        broken.write_text('{"info": {"id": "x"}, "messages": [{"info": ', encoding="utf-8")
        raises(lambda: analyse_file(broken), "обрезанный JSON — отказ")

        not_export = Path(td) / "other.json"
        not_export.write_text('{"a": 1}', encoding="utf-8")
        raises(lambda: analyse_file(not_export), "не экспорт — отказ")

        big = Path(td) / "many.json"
        messages = [
            {"info": {"id": f"m{i}", "role": "assistant", "time": {"created": 1000 + i},
                      "cost": 0.0, "tokens": {"total": 1}, "modelID": "m", "providerID": "p",
                      "finish": "stop"},
             "parts": [{"type": "text", "text": "x" * 200}]}
            for i in range(5000)
        ]
        big.write_text(json.dumps({"info": {**info, "cost": 0}, "messages": messages}),
                       encoding="utf-8")
        ok(analyse_file(big)["messages"]["total"] == 5000, "5000 сообщений читаются потоком")
        many_hits, many_meta = find_hits(big, "x", limit=5)
        ok(len(many_hits) == 200 and many_meta["total"] == 5000
           and many_meta["capped"] is True,
           "cap: total точный, хранится только первые N")

        # --- компакции: карта, поиск, дамп, сводка ---
        comp = Path(td) / "comp.json"
        comp_data = {
            "info": dict(info, cost=0),
            "messages": [
                {"info": {"id": "m0", "role": "user", "time": {"created": 1}},
                 "parts": [{"type": "text", "text": "решение: использовать SQLite"}]},
                {"info": {"id": "m1", "role": "assistant", "time": {"created": 2},
                          "cost": 0, "tokens": {}, "modelID": "m", "providerID": "p"},
                 "parts": [{"type": "step-start"}, {"type": "text", "text": "принято"},
                           {"type": "step-finish", "cost": 0}]},
                {"info": {"id": "m2", "role": "user", "time": {"created": 3}},
                 "parts": [{"type": "compaction", "auto": True, "tail_start_id": "m3"}]},
                {"info": {"id": "m3", "role": "assistant", "mode": "compaction",
                          "time": {"created": 4}, "cost": 0, "tokens": {},
                          "modelID": "m", "providerID": "p"},
                 "parts": [{"type": "step-start"}, {"type": "text", "text": "сводка: SQLite"},
                           {"type": "step-finish", "cost": 0}]},
                {"info": {"id": "m4", "role": "assistant", "time": {"created": 5},
                          "cost": 0, "tokens": {}, "modelID": "m", "providerID": "p"},
                 "parts": [{"type": "step-start"}, {"type": "text", "text": "позже: Postgres"},
                           {"type": "step-finish", "cost": 0}]},
            ],
        }
        comp.write_text(json.dumps(comp_data, ensure_ascii=False), encoding="utf-8")
        comp_summary = analyse_file(comp)
        ok(len(comp_summary["compaction_events"]) == 1
           and comp_summary["compaction_events"][0]["summary_chars"] > 0,
           "карта компакций: событие и размер сводки")
        ok(comp_summary["compaction_events"][0]["verified"] is True,
           "сводка подтверждена как примыкающая к событию")
        ok(comp_summary["compactions"] == {"total": 1, "auto": 1}, "компакция учтена")
        ok(comp_summary["last_compaction_idx"] == 2, "индекс последней компакции")
        hits, _ = find_hits(comp, "SQLite")
        ok(any(h["idx"] == 0 and h["before_last_compaction"] for h in hits),
           "поиск находит знание до компакции и помечает его")
        ok(any(h["idx"] == 3 and not h["before_last_compaction"] for h in hits),
           "сводка компакции помечена как после компакции")
        later, _ = find_hits(comp, "Postgres")
        ok(len(later) == 1 and not later[0]["before_last_compaction"],
           "поиск после компакции не помечается как потерянный")
        ranged = find_hits_many(comp, ["SQLite"], from_index=0, to_index=1)
        ok(len(ranged[0]["hits"]) == 1 and ranged[0]["hits"][0]["idx"] == 0,
           "search range: inclusive index bounds")
        ranged = find_hits_many(comp, ["SQLite"], from_index=4)
        ok(ranged[0]["hits"] == [], "search range: lower bound excludes earlier hits")
        ctx_hits, _ = find_hits(comp, "SQLite", context_window=1)
        first = next(h for h in ctx_hits if h["idx"] == 0)
        ok([i for i, _ in first["context_before"]] == []
           and [i for i, _ in first["context_after"]] == [1],
           "search context: after-context collected")
        last = next(h for h in ctx_hits if h["idx"] == 3)
        ok([i for i, _ in last["context_before"]] == [2]
           and [i for i, _ in last["context_after"]] == [4],
           "search context: before/after collected")
        dumped = "\n".join(dump_range(comp, "m0", "m1", None))
        ok("SQLite" in dumped and "принято" in dumped and "Postgres" not in dumped,
           "дамп диапазона берёт ровно нужные сообщения")
        ok(show_summary(comp, 1).startswith("сводка"), "сводка компакции читается")
        empty_path = Path(td) / "empty-summary.json"
        empty_path.write_text(json.dumps({"info": dict(info, cost=0), "messages": [
            {"info": {"id": "e0", "role": "user", "time": {"created": 1}},
             "parts": [{"type": "text", "text": "x"}]},
            {"info": {"id": "e1", "role": "user", "time": {"created": 2}},
             "parts": [{"type": "compaction", "auto": True, "tail_start_id": "e2"}]},
            {"info": {"id": "e2", "role": "assistant", "mode": "compaction",
                      "time": {"created": 3}, "cost": 0, "tokens": {},
                      "modelID": "m", "providerID": "p"},
             "parts": [{"type": "text", "text": ""}]},
        ]}, ensure_ascii=False), encoding="utf-8")
        ok(show_summary(empty_path, 1) == "", "empty summary has no text")
        rc, out, err = run_cli([str(empty_path), "--show-summary", "1"])
        ok(rc == 0 and out.strip() == "" and "empty" in err,
           "empty summary explains itself on stderr")
        far_summary = json.loads(json.dumps(comp_data))
        far_summary["messages"].insert(3, {"info": {"id": "mx", "role": "assistant",
                                                    "time": {"created": 3},
                                                    "cost": 0, "tokens": {},
                                                    "modelID": "m", "providerID": "p"},
                                           "parts": [{"type": "text", "text": "x"}]})
        far_path = Path(td) / "far.json"
        far_path.write_text(json.dumps(far_summary, ensure_ascii=False), encoding="utf-8")
        ok(any("не примыкает" in w for w in analyse_file(far_path)["warnings"]),
           "не примыкающая сводка — предупреждение")

        raises(lambda: show_summary(comp, 2), "несуществующая сводка — отказ")

        summary_text = "## Rules\nalpha rule\n## Notes\nbeta: gamma\n"
        ok(len(summary_outline(summary_text)) == 2, "summary outline: заголовки найдены")
        ok(summary_grep(summary_text, "beta") == [(4, "beta: gamma")],
           "summary grep: строка с термином")
        ok(len(summary_diff("a\nb", "a\nc")) >= 3, "summary diff: различия видны")
        path_text = ("see C:\\repo\\alpha.txt\n"
                     "alpha rule\n"
                     "both C:\\alpha and alpha rule\n"
                     "https://example.com/alpha\n")
        ok(len(summary_grep(path_text, "alpha")) == 4,
           "summary grep: paths match by default")
        ok([n for n, _ in summary_grep(path_text, "alpha", exclude_paths=True)] == [2, 3],
           "summary grep: --exclude-paths skips path-only lines")

        ids_only = format_message(0, {"info": {"id": "m0", "role": "user",
                                               "time": {"created": 1}},
                                      "parts": [{"type": "text", "text": "hello"}]},
                                  ids_only=True)
        ok(ids_only.startswith("--- #0") and "hello" not in ids_only,
           "dump --ids: только заголовок")
        text_only = format_message(0, {"info": {"id": "m0", "role": "assistant",
                                                "time": {"created": 1}},
                                       "parts": [{"type": "reasoning", "text": "r"},
                                                 {"type": "text", "text": "answer"}]},
                                   only={"text"})
        ok("[text] answer" in text_only and "[reasoning]" not in text_only,
           "dump --only text: без reasoning")

        two_path = Path(td) / "two-comp.json"
        two_path.write_text(json.dumps({"info": dict(info, cost=0), "messages": [
            {"info": {"id": "a0", "role": "user", "time": {"created": 1}},
             "parts": [{"type": "text", "text": "old one"}]},
            {"info": {"id": "t1", "role": "user", "time": {"created": 2}},
             "parts": [{"type": "text", "text": "tail one"}]},
            {"info": {"id": "c1", "role": "user", "time": {"created": 3}},
             "parts": [{"type": "compaction", "auto": True, "tail_start_id": "t1"}]},
            {"info": {"id": "s1", "role": "assistant", "mode": "compaction",
                      "time": {"created": 4}, "cost": 0, "tokens": {},
                      "modelID": "m", "providerID": "p"},
             "parts": [{"type": "text", "text": "summary one"}]},
            {"info": {"id": "a4", "role": "user", "time": {"created": 5}},
             "parts": [{"type": "text", "text": "middle"}]},
            {"info": {"id": "t2", "role": "user", "time": {"created": 6}},
             "parts": [{"type": "text", "text": "tail two"}]},
            {"info": {"id": "c2", "role": "user", "time": {"created": 7}},
             "parts": [{"type": "compaction", "auto": True, "tail_start_id": "t2"}]},
            {"info": {"id": "s2", "role": "assistant", "mode": "compaction",
                      "time": {"created": 8}, "cost": 0, "tokens": {},
                      "modelID": "m", "providerID": "p"},
             "parts": [{"type": "text", "text": "summary two"}]},
            {"info": {"id": "a8", "role": "user", "time": {"created": 9}},
             "parts": [{"type": "text", "text": "after"}]},
        ]}, ensure_ascii=False), encoding="utf-8")
        events = compaction_boundaries(two_path)
        ok(len(events) == 2 and events[0]["tail_idx"] == 1 and events[1]["tail_idx"] == 5,
           "compaction boundaries: обе границы")
        old, _ = find_hits(two_path, "middle", before_compaction=2)
        ok(len(old) == 1 and old[0]["before_selected"], "before-compaction: хит до границы")
        after, _ = find_hits(two_path, "after", before_compaction=2)
        ok(after == [], "before-compaction: хит после границы не попадает")
        multi = find_hits_many(two_path, ["middle", "after"], before_compaction=2)
        ok(len(multi) == 2 and len(multi[0]["hits"]) == 1 and multi[1]["hits"] == [],
           "batch search: два запроса за один проход")
        windows = build_timeline(two_path)
        ok(windows[0]["from_idx"] == 0 and windows[0]["to_idx"] == 2,
           "timeline: диапазон индексов окна")
        raises(lambda: find_hits(two_path, "x", before_compaction=3),
               "before-compaction: нет такой компакции — отказ")

        usage_root = Path(td) / "fb"
        rec = record_usage("timeline", path, 123, time.time(), root=usage_root)
        ok(rec is not None and rec.exists(), "usage-запись создаётся")
        if rec:
            rec_data = json.loads(rec.read_text(encoding="utf-8"))
            ok(rec_data.get("mode") == "timeline" and rec_data.get("size_bytes") == 123,
               "usage: mode и размер на месте")
            ok(rec_data.get("schema_version") == 1, "usage: schema_version")

        by_index = dump_range(comp, None, None, 2, from_index=3)
        ok(len(by_index) == 2 and by_index[0].startswith("--- #3"),
           "дамп по индексу: начало с нужного сообщения")
        ok(split_ref("#3", None) == (None, 3) and split_ref("209", None) == (None, 209)
           and split_ref("msg_x", None) == ("msg_x", None),
           "'#N' трактуется как индекс, id остаётся id")
        raises(lambda: dump_range(comp, None, None, None),
               "дамп без границ отвергнут (иначе выгрузит весь файл)")

        raises(lambda: dump_range(comp, None, None, None, from_index=0),
               "начало без конца отвергнуто (--count/--to/--all)")
        ok("[truncated" in format_message(0, comp_data["messages"][0], max_chars=5),
           "обрезка помечена в тексте")

        page, total = paginate(list(range(10)), 3, 2)
        ok(page == [3, 4] and total == 10, "пагинация: смещение и лимит")
        windows = build_timeline(comp)
        ok(len(windows) == 2, "timeline: два окна вокруг компакции")
        ok(windows[0]["messages"] == 3 and windows[0]["compaction"] is not None,
           "timeline: окно до компакции заканчивается событием")
        ok(windows[1]["messages"] == 2 and windows[1]["compaction"] is None,
           "timeline: хвост после компакции")
        around = dump_range(comp, None, None, 3, from_index=2)
        ok(len(around) == 3 and around[0].startswith("--- #2"),
           "--around: диапазон вокруг индекса")

        # --- CLI: main(), exit codes, JSON, usage ---
        rc, out, _ = run_cli([])
        ok(rc == 2, "CLI: без файла — код 2")
        rc, out, _ = run_cli([str(path)])
        ok(rc == 0 and "messages" in out, "CLI: overview")
        rc, out, _ = run_cli([str(path), "--json"])
        ok(rc == 0 and json.loads(out)["messages"]["total"] == 3, "CLI: overview JSON")
        rc, out, _ = run_cli([str(two_path), "--search", "middle"])
        ok(rc == 0 and "hits 1" in out and "a4" in out, "CLI: поиск и id")
        rc, out, _ = run_cli([str(two_path), "--search", "middle", "--json"])
        payload = json.loads(out)
        ok(rc == 0 and payload["total"] == 1 and payload["stored"] == 1, "CLI: поиск JSON")
        rc, out, _ = run_cli([str(comp), "--search", "SQLite", "--from-index", "4"])
        ok(rc == 0 and "hits 0" in out, "CLI: search range")
        rc, _, err = run_cli([str(comp), "--search", "SQLite",
                              "--from-index", "3", "--to-index", "1"])
        ok(rc == 2 and "--to-index" in err, "CLI: inverted search range rejected")
        rc, _, err = run_cli([str(comp), "--search", "SQLite", "--from-id", "m0"])
        ok(rc == 2 and "--from-index" in err, "CLI: search rejects id bounds")
        rc, out, _ = run_cli([str(comp), "--search", "SQLite",
                              "--with-context", "1"])
        ok(rc == 0 and "context:" in out and "принято" in out and "Postgres" in out,
           "CLI: search with context")
        rc, _, _ = run_cli([str(comp), "--with-context", "1"])
        ok(rc == 2, "CLI: --with-context requires search")
        rc, _, _ = run_cli([str(comp), "--search", "SQLite",
                            "--with-context", "1", "--json"])
        ok(rc == 2, "CLI: --with-context + json rejected")
        rc, out, _ = run_cli([str(comp), "--show-summary", "1", "--json"])
        ok(rc == 0 and json.loads(out)["summary"] == 1, "CLI: summary JSON")
        rc, out, _ = run_cli([str(comp), "--compactions", "--json"])
        ok(rc == 0 and json.loads(out)["compactions"] == 1, "CLI: compactions JSON")
        rc, out, _ = run_cli([str(comp), "--dump", "--from-index", "0", "--count", "1"])
        ok(rc == 0 and "--- #0" in out, "CLI: dump")
        rc, _, _ = run_cli([str(path), "--json", "--dump"])
        ok(rc == 2, "CLI: --json + dump отклонён")
        rc, _, _ = run_cli([str(path), "--search", "x", "--fields", "bogus"])
        ok(rc == 2, "CLI: --fields валидируется")
        rc, _, _ = run_cli([str(path), "--dump", "--only", "bogus"])
        ok(rc == 2, "CLI: --only валидируется")
        rc, _, _ = run_cli([str(path), "--around", "-1"])
        ok(rc == 2, "CLI: --around не отрицательный")
        rc, _, _ = run_cli([str(path), "--search", "x", "--queries", "y"])
        ok(rc == 2, "CLI: search + queries отклонён")
        rc, _, hint = run_cli([str(two_path), "--queries", "middle,old", "--regex"])
        ok(rc == 2 and "literal-only" in hint,
           "CLI: --queries rejects regex, points to --queries-file")
        rc, out, _ = run_cli([str(two_path), "--queries", "middle,old"])
        ok(rc == 0 and "query 1/2" in out, "CLI: literal --queries still splits")
        rc, out, _ = run_cli([str(two_path), "--queries", "absent1,absent2"])
        ok(rc == 0 and out.count("first retry") == 1,
           "CLI: batch 0-hit advice printed once")
        reason_path = Path(td) / "reason.json"
        reason_path.write_text(json.dumps({"info": dict(info, cost=0), "messages": [
            {"info": {"id": "r0", "role": "assistant", "time": {"created": 1},
                      "cost": 0, "tokens": {}, "modelID": "m", "providerID": "p"},
             "parts": [{"type": "reasoning", "text": "alpha"}]},
            {"info": {"id": "r1", "role": "assistant", "time": {"created": 2},
                      "cost": 0, "tokens": {}, "modelID": "m", "providerID": "p"},
             "parts": [{"type": "reasoning", "text": "alpha"}]},
        ]}, ensure_ascii=False), encoding="utf-8")
        rc, out, _ = run_cli([str(reason_path), "--queries", "alpha,missing"])
        ok(rc == 0 and out.count("some hits are in reasoning") == 1,
           "CLI: batch reasoning hint printed once")
        rc, out, _ = run_cli([str(comp), "--summary-outline", "1"])
        ok(rc == 0 and "outline" in out, "CLI: standalone summary-outline")
        rc, out, _ = run_cli([str(comp), "--summary-grep", "1", "beta"])
        ok(rc == 0 and "matching lines" in out, "CLI: standalone summary-grep")
        rc, _, _ = run_cli([str(comp), "--summary-grep", "1"])
        ok(rc == 2, "CLI: summary-grep requires N and TERM")
        rc, _, err = run_cli([str(path), "--search", "x", "--before-tail"])
        ok(rc == 1 and "компакц" in err, "CLI: before-tail без компакций — код 1")
        rc, out, _ = run_cli([str(two_path), "--search", "middle",
                              "--before-compaction", "2"])
        ok(rc == 0 and "hits 1" in out, "CLI: before-compaction")
        rc, out, _ = run_cli(["--list-exports", str(td)])
        ok(rc == 0 and "synthetic.json" in out, "CLI: list-exports")
        rc, _, _ = run_cli(["--list-exports", str(path)])
        ok(rc == 1, "CLI: list-exports на файле — код 1")
        rc, _, _ = run_cli(["--list-exports", str(td), "--search", "x"])
        ok(rc == 2, "CLI: list-exports + search отклонён")

    print(f"\n  PASS {checks - failed} | FAIL {failed}")
    return 0 if failed == 0 else 1


def positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("нужно целое ≥ 1")
    return n


def non_negative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("нужно целое ≥ 0")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Разбор экспорта opencode")
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--list-exports", nargs="?", const=".", metavar="DIR",
                    help="список *.json в каталоге: размер и дата")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--brief", action="store_true",
                    help="overview без cost/model/tool-таблиц")
    ap.add_argument("--exclude-paths", action="store_true",
                    help="только для --summary-grep: пропускать строки, где все "
                         "совпадения внутри путей")
    ap.add_argument("--top", type=non_negative, default=5, help="сколько строк в каждом разборе")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--compactions", action="store_true", help="карта компакций")
    mode.add_argument("--show-summary", type=positive, metavar="N", help="текст N-й сводки компакции")
    mode.add_argument("--summary-outline", type=positive, metavar="N",
                      help="только заголовки N-й сводки")
    mode.add_argument("--summary-grep", nargs=2, metavar=("N", "TERM"),
                      help="строки N-й сводки, содержащие TERM")
    mode.add_argument("--summary-diff", nargs=2, type=positive, metavar=("A", "B"),
                      help="diff двух сводок компакции")
    mode.add_argument("--search", metavar="QUERY", help="поиск по полной истории")
    ap.add_argument("--queries", nargs="+", default=[],
                    help="несколько запросов (через запятую или пробелами), один проход; "
                         "с --regex каждый аргумент — один шаблон")
    ap.add_argument("--queries-file", type=Path,
                    help="файл с запросами, по одному на строку")
    ap.add_argument("--regex", action="store_true", help="regex вместо literal")
    ap.add_argument("--case", action="store_true", help="учитывать регистр")
    ap.add_argument("--limit", type=positive, default=20, help="хитов на страницу")
    ap.add_argument("--with-context", type=positive, metavar="N",
                    help="search: N сообщений до/после каждого хита")
    ap.add_argument("--context", type=non_negative, default=80,
                    help="символов вокруг совпадения")
    ap.add_argument("--roles", default="", help="user,assistant")
    ap.add_argument("--fields", default="",
                    help="только для --search: text,reasoning,tool-input,tool-output,"
                         "tool-title,tool-error,file")
    ap.add_argument("--before-tail", action="store_true",
                    help="только то, что срезала последняя компакция")
    ap.add_argument("--before-compaction", type=positive, metavar="N",
                    help="только то, что было до границы N-й компакции")
    ap.add_argument("--ids", action="store_true",
                    help="для --dump/--around: только заголовки сообщений, без частей")
    ap.add_argument("--only", default="",
                    help="только для --dump/--around: text,reasoning,tool,file,compaction")
    mode.add_argument("--dump", action="store_true", help="выгрузить диапазон сообщений")
    ap.add_argument("--from-id", help="id сообщения; '#209' или '209' трактуется как индекс")
    ap.add_argument("--to-id")
    ap.add_argument("--from-index", type=non_negative)
    ap.add_argument("--to-index", type=non_negative)
    mode.add_argument("--around", type=non_negative, metavar="N",
                    help="выгрузить сообщения вокруг индекса N; --max-chars режет "
                         "каждую часть, для контекста добавьте --only text")
    ap.add_argument("--window", type=non_negative, default=2,
                    help="сколько сообщений до/после для --around")
    mode.add_argument("--timeline", action="store_true", help="карта сессии по окнам компакций")
    ap.add_argument("--offset", type=non_negative, default=0, help="пропустить первые N хитов")
    ap.add_argument("--count", type=positive)
    ap.add_argument("--max-chars", type=positive, default=3000,
                    help="лимит на часть (не на сообщение) при --dump/--around")
    ap.add_argument("--all", action="store_true",
                    help="разрешить дамп до конца файла (нужен --from-* или --count)")
    ap.add_argument("--selftest", action="store_true")
    return ap


def validate_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.list_exports is not None:
        conflicts = any((
            args.files, args.search is not None, args.queries, args.queries_file,
            args.timeline, args.dump, args.around is not None, args.compactions,
            args.show_summary is not None, args.summary_diff,
            args.summary_outline, args.summary_grep, args.ids, args.only,
            args.before_tail, args.before_compaction, args.fields, args.roles,
            args.from_id, args.to_id, args.from_index is not None,
            args.to_index is not None, args.count, args.all,
            args.regex, args.case, args.offset, args.window != 2,
            args.max_chars != 3000, args.limit != 20, args.context != 80,
            args.top != 5, args.brief,
        ))
        if conflicts:
            ap.error("--list-exports не сочетается с файлами и другими режимами")
        return
    if args.brief:
        if args.json:
            ap.error("--brief не сочетается с --json")
        if any((
            args.search is not None, args.queries, args.queries_file, args.timeline,
            args.dump, args.around is not None, args.compactions,
            args.show_summary is not None, args.summary_diff,
            args.summary_outline is not None, args.summary_grep is not None,
        )):
            ap.error("--brief работает только с overview")
    if args.summary_grep is not None:
        try:
            number = int(args.summary_grep[0])
        except ValueError:
            ap.error("--summary-grep: первое значение должно быть номером сводки")
        if number < 1:
            ap.error("--summary-grep: номер сводки должен быть >= 1")
    if args.exclude_paths and args.summary_grep is None:
        ap.error("--exclude-paths работает только с --summary-grep")
    if args.json and (args.dump or args.around is not None):
        ap.error("--json не поддерживается для --dump/--around: это текстовый вывод")
    if args.search is not None and (args.queries or args.queries_file):
        ap.error("--search не сочетается с --queries/--queries-file")
    if (args.search is not None or args.queries or args.queries_file) and (
            args.from_id or args.to_id):
        ap.error("--search: используйте --from-index/--to-index; id не поддерживается")
    if (args.from_index is not None and args.to_index is not None
            and args.to_index < args.from_index):
        ap.error("--to-index не может быть меньше --from-index")
    if args.queries and args.regex:
        ap.error("--queries literal-only: для regex используйте --queries-file")
    if args.queries and args.queries_file:
        ap.error("--queries и --queries-file взаимоисключающие")
    if (args.queries or args.queries_file) and (
            args.timeline or args.dump or args.around is not None
            or args.compactions or args.show_summary is not None
            or args.summary_outline is not None or args.summary_grep is not None
            or args.summary_diff):
        ap.error("--queries/--queries-file не сочетаются с другими режимами")
    if args.ids and not (args.dump or args.around is not None):
        ap.error("--ids работает только с --dump/--around")
    if args.with_context is not None and not (
            args.search is not None or args.queries or args.queries_file):
        ap.error("--with-context работает только с --search/--queries")
    if args.with_context is not None and args.json:
        ap.error("--with-context не сочетается с --json")
    if args.only and not (args.dump or args.around is not None
                          or args.with_context is not None):
        ap.error("--only работает только с --dump/--around/--with-context")
    if args.only:
        allowed = {"text", "reasoning", "tool", "file", "compaction"}
        bad = {x.strip() for x in args.only.split(",") if x.strip()} - allowed
        if bad:
            ap.error(f"--only: неизвестные поля {sorted(bad)}; допустимо {sorted(allowed)}")
    if args.before_tail and args.before_compaction:
        ap.error("--before-tail и --before-compaction взаимоисключающие")
    if (args.before_tail or args.before_compaction) and not (
            args.search is not None or args.queries or args.queries_file):
        ap.error("--before-tail/--before-compaction работают только с поиском")
    if args.fields and not (args.search is not None or args.queries or args.queries_file):
        ap.error("--fields работает только с поиском")
    if args.roles and not (args.search is not None or args.queries or args.queries_file):
        ap.error("--roles работает только с поиском")
    if args.fields:
        allowed_fields = {"text", "reasoning", "tool-input", "tool-output",
                          "tool-title", "tool-error", "file"}
        bad = {x.strip() for x in args.fields.split(",") if x.strip()} - allowed_fields
        if bad:
            ap.error(f"--fields: неизвестные поля {sorted(bad)}; допустимо {sorted(allowed_fields)}")
    if args.roles:
        allowed_roles = {"user", "assistant"}
        bad = {x.strip() for x in args.roles.split(",") if x.strip()} - allowed_roles
        if bad:
            ap.error(f"--roles: неизвестные роли {sorted(bad)}; допустимо {sorted(allowed_roles)}")
    if len(args.files) > 1:
        ap.error("укажите один экспорт: несколько позиционных файлов не поддерживаются")
    if args.around is not None and (args.from_id or args.to_id or args.from_index is not None
                                    or args.to_index is not None or args.count or args.all):
        ap.error("--around не сочетается с --from-*/--to-*/--count/--all")


def run_with_telemetry(path: Path, mode: str, started: float, fn) -> int:
    extra: dict = {"status": "ok"}
    try:
        rc, result_extra = fn()
        extra.update(result_extra)
        return rc
    except BrokenPipeError:
        extra.update(status="ok", broken_pipe=True)
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except re.error as exc:
        extra.update(status="error", error_type="RegexError")
        print(f"ERROR: regex: {exc}", file=sys.stderr)
        return 1
    except ExportError as exc:
        extra.update(status="error", error_type="ExportError")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        extra.update(status="error", error_type="UnicodeDecodeError")
        print(f"ERROR: файл не в UTF-8: {exc}. Перекодируй экспорт в UTF-8.", file=sys.stderr)
        return 1
    except OSError as exc:
        extra.update(status="error", error_type=type(exc).__name__)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        extra.update(status="error", error_type=type(exc).__name__)
        print(f"ERROR: повреждённый экспорт: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        extra.setdefault("status", "ok")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        record_usage(mode, path, size, started, extra=extra)
        try:
            sys.stdout.flush()
        except OSError:
            pass
        try:
            if not extra.get("broken_pipe"):
                friction = friction_of(extra)
                if friction:
                    print(friction_hint(mode, path, friction), file=sys.stderr)
                elif mode in {"overview", "timeline", "compactions"}:
                    print("\n" + feedback_hint(mode, path), file=sys.stderr)
        except OSError:
            pass




def cmd_search(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    queries: list[str] = []
    if args.search is not None:
        if not args.search:
            raise ExportError("пустой --search: укажите строку или regex")
        queries.append(args.search)
    for chunk in args.queries:
        chunk = chunk.strip()
        if not chunk:
            continue
        queries += [q.strip() for q in chunk.split(",") if q.strip()]
    if args.queries_file:
        try:
            lines = args.queries_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ExportError(f"не удалось прочитать --queries-file: {exc}")
        queries += [line.strip() for line in lines
                    if line.strip() and not line.strip().startswith("#")]
    queries = list(dict.fromkeys(queries))
    if not queries:
        raise ExportError("пустой список запросов")
    roles = {r.strip() for r in args.roles.split(",") if r.strip()} or None
    fields = {f.strip() for f in args.fields.split(",") if f.strip()} or None
    only = {f.strip() for f in args.only.split(",") if f.strip()} or None
    want = args.offset + args.limit
    results = find_hits_many(path, queries, regex=args.regex, case=args.case,
                             limit=want, context=args.context,
                             roles=roles, fields=fields,
                             before_compaction=args.before_compaction,
                             before_tail=args.before_tail,
                             from_index=args.from_index, to_index=args.to_index,
                             context_window=args.with_context or 0)
    pages = []
    for result in results:
        hits = result["hits"]
        page, stored = paginate(hits, args.offset, args.limit)
        meta = result["meta"]
        display_total = meta["total"]
        pages.append((result, page, stored, display_total))
    extra = {
        "hits": sum(display_total for _, _, _, display_total in pages),
        "queries": len(queries),
        "zero_hits": sum(1 for _, _, _, display_total in pages
                         if display_total == 0),
        "capped": any(result["meta"]["capped"] for result, _, _, _ in pages),
    }
    if args.json:
        items = []
        for result, page, stored, display_total in pages:
            meta = result["meta"]
            items.append({
                "query": result["query"],
                "total": display_total,
                "total_exact": True,
                "stored": stored,
                "offset": args.offset,
                "limit": args.limit,
                "capped": meta["capped"],
                "complete": not meta["capped"],
                "boundary": meta["boundary"],
                "boundary_kind": meta["boundary_kind"],
                "hits": page,
            })
        payload = items[0] if len(items) == 1 else {"queries": items}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        shown_zero_advice = False
        shown_reasoning_advice = False
        shown_no_compactions = False
        for n, (result, page, stored, display_total) in enumerate(pages, 1):
            meta = result["meta"]
            if len(queries) > 1:
                print(f"query {n}/{len(queries)}: {result['query']!r}")
            shown_from = args.offset + 1 if page else 0
            shown_to = args.offset + len(page) if page else 0
            print(f"hits {display_total} (shown {shown_from}..{shown_to})")
            if display_total == 0 and not shown_zero_advice:
                print("  note: 0 hits — first retry without --roles/--fields, "
                      "then a shorter stem or --regex")
                print("  note: if 0 is expected (term absent), no --note is needed")
                shown_zero_advice = True
            for h in page:
                when = stamp(h["time"])
                if args.before_compaction:
                    mark = (f"  BEFORE-COMPACTION #{args.before_compaction}"
                            if h.get("before_selected") else "")
                else:
                    mark = ("  BEFORE-COMPACTION"
                            if h["before_last_compaction"] else "")
                mid = h.get("id") or "?"
                print(f"  #{h['idx']} {mid} {when} {h['role']:<9} "
                      f"[{h['field']}]{mark}")
                print(f"    {h['snippet']}")
                if args.with_context:
                    neighbors = (h.get("context_before", [])
                                 + h.get("context_after", []))
                    if neighbors:
                        print("    context:")
                        for cidx, cmsg in neighbors:
                            block = format_message(cidx, cmsg,
                                                   max_chars=args.max_chars,
                                                   only=only)
                            for line in block.splitlines():
                                print(f"      {line}")
            if page and not fields and not shown_reasoning_advice and any(
                    h["field"] == "reasoning" for h in page):
                print("  note: some hits are in reasoning — add --fields text for the answer")
                shown_reasoning_advice = True
            if page:
                print(f"  dump: --dump --from-index {page[0]['idx']} --count 3")
            if not page and display_total > 0 and args.offset >= stored:
                print(f"  note: --offset {args.offset} is beyond the {stored} "
                      f"stored hits; narrow the query or lower the offset")
            elif shown_to < stored:
                print(f"  next: --offset {shown_to}")
            if meta["capped"]:
                if args.before_tail:
                    print("  note: hit cap reached; some matches before the tail "
                          "may be omitted")
                elif stored < display_total:
                    print(f"  note: showing the first {stored} of {display_total} "
                          f"matches — narrow the query for more")
            if args.before_tail and meta["last_compaction"] is None and not shown_no_compactions:
                print("  note: в экспорте нет компакций — --before-tail ничего не отсекает")
                shown_no_compactions = True
            if n < len(pages):
                print()
    return 0, extra

def cmd_timeline(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    windows = build_timeline(path)
    extra = {"windows": len(windows)}
    if args.json:
        print(json.dumps(windows, ensure_ascii=False, indent=2))
    elif not windows:
        print("timeline: сообщений нет")
    else:
        first_start = next((w["start"] for w in windows if w["start"]), None)
        zone = local_dt(first_start).strftime("%z") if first_start else "+0000"
        print(f"timeline {len(windows)} windows (local time {zone})")
        for n, w in enumerate(windows):
            tools = ", ".join(f"{t} {c}" for t, c in w["tools"].most_common(3))
            start = local_dt(w["start"]).strftime("%d.%m %H:%M") if w["start"] else "?"
            end = local_dt(w["end"]).strftime("%d.%m %H:%M") if w["end"] else "?"
            kind = ""
            if w["compaction"]:
                kind = "auto" if w["compaction"]["auto"] else "manual"
                kind = f"  end: {kind} compaction"
            span = (f" [{w.get('from_idx')}..{w.get('to_idx')}]"
                    if w.get("from_idx") is not None else "")
            print(f"  #{n}{span} {start} .. {end}  {w['messages']} msgs  "
                  f"{money(w['cost'])}  {tools}{kind}")
            if w["first_user"]:
                print(f"      {w['first_user']}")
    return 0, extra

def cmd_slice(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    from_id, from_index = split_ref(args.from_id, args.from_index)
    to_id, to_index = split_ref(args.to_id, args.to_index)
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    if args.around is not None:
        from_index = max(0, args.around - args.window)
        count = 2 * args.window + 1
        lines = dump_range(path, None, None, count, args.max_chars,
                           from_index=from_index, ids_only=args.ids,
                           only=only)
    else:
        lines = dump_range(path, from_id, to_id, args.count, args.max_chars,
                           from_index=from_index, to_index=to_index,
                           allow_all=args.all, ids_only=args.ids,
                           only=only)
    extra = {"messages": len(lines)}
    if not lines:
        extra.update(status="error", error_type="RangeNotFound")
        print("ERROR: диапазон не найден", file=sys.stderr)
        return 1, extra
    if only == {"text"} and not args.ids and not any("[text]" in line for line in lines):
        print("  note: no text parts in this range; try --only tool or --only file",
              file=sys.stderr)
    print(("\n" if args.ids else "\n\n").join(lines))
    return 0, extra

def cmd_summary(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    text = show_summary(path, args.show_summary)
    extra = {"chars": len(text)}
    if args.json:
        print(json.dumps({"summary": args.show_summary, "chars": len(text),
                          "text": text}, ensure_ascii=False, indent=2))
    else:
        if not text:
            print(f"note: summary {args.show_summary} is empty "
                  "(compaction without summary text)", file=sys.stderr)
        print(text)
    return 0, extra


def cmd_summary_outline(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    text = show_summary(path, args.summary_outline)
    lines = summary_outline(text)
    extra = {"chars": len(text), "headings": len(lines)}
    if args.json:
        print(json.dumps({"summary": args.summary_outline, "count": len(lines),
                          "outline": lines}, ensure_ascii=False, indent=2))
    else:
        print(f"summary {args.summary_outline} outline: {len(lines)} headings")
        print("\n".join(lines) if lines else "  (заголовков не найдено)")
    return 0, extra


def cmd_summary_grep(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    number = int(args.summary_grep[0])
    term = args.summary_grep[1]
    text = show_summary(path, number)
    if "|" in term and not args.regex:
        print("  note: summary-grep treats the term literally; "
              "add --regex for alternation", file=sys.stderr)
    matched = summary_grep(text, term, args.regex, args.case, args.exclude_paths)
    extra = {"chars": len(text), "matches": len(matched),
             "exclude_paths": bool(args.exclude_paths)}
    if args.json:
        print(json.dumps({"summary": number, "query": term, "count": len(matched),
                          "exclude_paths": bool(args.exclude_paths),
                          "matches": [{"line": n, "text": line}
                                      for n, line in matched]},
                         ensure_ascii=False, indent=2))
    else:
        suffix = " (paths excluded)" if args.exclude_paths else ""
        print(f"summary {number}: {len(matched)} matching lines{suffix}")
        for n, line in matched:
            print(f"{n:4} {line}")
    return 0, extra


def cmd_summary_diff(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    a_text = show_summary(path, args.summary_diff[0])
    b_text = show_summary(path, args.summary_diff[1])
    diff = summary_diff(a_text, b_text, args.summary_diff[0], args.summary_diff[1])
    extra = {"chars": len(a_text) + len(b_text), "diff_lines": len(diff)}
    if args.json:
        print(json.dumps({"a": args.summary_diff[0], "b": args.summary_diff[1],
                          "count": len(diff), "diff": diff},
                         ensure_ascii=False, indent=2))
    else:
        print(f"summary diff {args.summary_diff[0]}..{args.summary_diff[1]}: "
              f"{len(diff)} lines")
        print("\n".join(diff) if diff else "  (нет различий)")
    return 0, extra

def cmd_compactions(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    summary = analyse_file(path)
    events = summary["compaction_events"]
    extra = {"events": len(events),
             "unverified": sum(1 for e in events if not e["verified"])}
    if args.json:
        print(json.dumps({"compactions": len(events), "events": events},
                         ensure_ascii=False, indent=2))
    else:
        print(f"compactions {len(events)}")
        for e in events:
            when = stamp(e["time"])
            kind = ("auto" if e.get("auto")
                else ("overflow" if e.get("overflow") else "manual"))
            print(f"  #{e['n']} {when} {kind:<8} summary {e['summary_chars'] / 1024:5.1f} KB  "
                  f"tail {e['tail_start_id']}")
            print(f"    {safe_line(e.get('summary_head', ''))}")
    return 0, extra

def cmd_overview(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    summary = analyse_file(path, top=args.top)
    extra = {"warnings": len(summary["warnings"]),
             "events": len(summary["compaction_events"])}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(human(summary, brief=args.brief))
    return 0, extra

def dispatch(path: Path, args: argparse.Namespace) -> tuple[int, dict]:
    if args.search is not None or args.queries or args.queries_file:
        return cmd_search(path, args)
    if args.timeline:
        return cmd_timeline(path, args)
    if args.dump or args.around is not None:
        return cmd_slice(path, args)
    if args.show_summary is not None:
        return cmd_summary(path, args)
    if args.summary_outline is not None:
        return cmd_summary_outline(path, args)
    if args.summary_grep is not None:
        return cmd_summary_grep(path, args)
    if args.summary_diff:
        return cmd_summary_diff(path, args)
    if args.compactions:
        return cmd_compactions(path, args)
    return cmd_overview(path, args)


def main(argv: list[str]) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    validate_args(ap, args)
    if args.list_exports is not None:
        started = time.time()
        target = Path(args.list_exports)
        try:
            rc = list_exports(target, as_json=args.json)
            record_usage("list", target, 0, started, extra={"status": "ok"})
            return rc
        except ExportError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if not args.files:
        ap.print_help()
        return 2

    path = args.files[0]
    started = time.time()
    mode = mode_name(args)
    return run_with_telemetry(path, mode, started, lambda: dispatch(path, args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
