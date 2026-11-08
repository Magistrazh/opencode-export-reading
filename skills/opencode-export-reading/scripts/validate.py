#!/usr/bin/env python3
"""Structural validator for the opencode-export-reading skill.

Run from the repository root or anywhere:
    python skills/opencode-export-reading/scripts/validate.py
    python skills/opencode-export-reading/scripts/validate.py --selftest

No third-party packages. No new user-facing skill flags: this script is an
internal release gate.
"""
from __future__ import annotations

import argparse
import re
import shutil
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

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
SKILL_REL = Path("skills") / SKILL_ROOT.name
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FRONTMATTER = {"name", "description", "version", "license", "compatibility",
               "metadata", "allowed-tools", "allowed_tools"}
REQUIRED_REPO = [
    "README.md",
    "README.ru.md",
    "CHANGES.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".github/workflows/ci.yml",
    "ruff.toml",
]
REQUIRED_SKILL = [
    "SKILL.md",
    "scripts/inspect-export.py",
    "scripts/feedback.py",
    "scripts/validate.py",
    "references/schema.md",
]
EVAL_REQUIRED = [
    "evals/opencode-export-reading/trigger.tsv",
    "evals/opencode-export-reading/run-golden.py",
    "evals/opencode-export-reading/score-trigger.py",
    "evals/opencode-export-reading/fixtures/manifest.json",
]
FORBIDDEN = {"__pycache__", "node_modules", ".tmp", "runs", ".DS_Store",
             ".ruff_cache", ".pytest_cache", "_tmp-feedback"}


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def validate(root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    skill_root = root / SKILL_REL
    skill_md = skill_root / "SKILL.md"
    if not skill_md.exists():
        return [f"{SKILL_REL}/SKILL.md missing"], warnings
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    if not meta:
        errors.append("frontmatter missing or malformed")
    unknown = set(meta) - FRONTMATTER
    if unknown:
        warnings.append(f"unknown frontmatter keys: {sorted(unknown)}")
    for key in ("name", "description", "version"):
        if not meta.get(key):
            errors.append(f"frontmatter missing {key}")
    name = meta.get("name", "")
    if name and not KEBAB.match(name):
        errors.append(f"name is not kebab-case: {name!r}")
    if name and name != skill_root.name:
        warnings.append(f"name {name!r} != folder {skill_root.name!r} (clone folder may be renamed)")
    description = meta.get("description", "")
    if len(description) > 1024:
        warnings.append(f"description is {len(description)} chars (soft max 1024)")
    if len(description) > 1500:
        errors.append(f"description is {len(description)} chars (hard max 1500)")
    version = meta.get("version", "")
    if version and not SEMVER.match(version):
        errors.append(f"version is not X.Y.Z: {version!r}")
    changelog = root / "CHANGES.md"
    if version and changelog.exists() and f"## {version}" not in changelog.read_text(encoding="utf-8"):
        errors.append(f"CHANGES.md has no section for version {version}")
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    size = len(text.encode("utf-8"))
    if lines > 250 or size > 20_000:
        warnings.append(f"SKILL.md is {lines} lines / {size} bytes (soft max 250 / 20 KB)")
    if lines > 500 or size > 40_000:
        errors.append(f"SKILL.md is {lines} lines / {size} bytes (hard max 500 / 40 KB)")

    for rel in REQUIRED_REPO:
        if not (root / rel).exists():
            errors.append(f"required path missing: {rel}")

    for rel in REQUIRED_SKILL:
        if not (skill_root / rel).exists():
            errors.append(f"required skill path missing: {rel}")

    for rel in EVAL_REQUIRED:
        if not (root / rel).exists():
            errors.append(f"eval path missing: {rel}")

    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            errors.append(f"symlink is not allowed: {path.relative_to(root)}")
        if path.name in FORBIDDEN or path.suffix == ".pyc":
            errors.append(f"forbidden artifact: {path.relative_to(root)}")

    for md in sorted(root.rglob("*.md")):
        if ".git" in md.parts:
            continue
        for match in LINK.finditer(md.read_text(encoding="utf-8", errors="replace")):
            target = match.group(1).split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (md.parent / target).exists():
                errors.append(f"broken relative link in {md.relative_to(root)}: {target}")
    return errors, warnings


def print_result(label: str, errors: list[str], warnings: list[str]) -> bool:
    print(f"[{label}]")
    for warning in warnings:
        print(f"  WARN  {warning}")
    for error in errors:
        print(f"  FAIL  {error}")
    print(f"  {'PASS' if not errors else 'FAIL'}  {len(warnings)} warning(s), {len(errors)} error(s)")
    return not errors


def copy_repo(dst: Path) -> None:
    shutil.copytree(REPO_ROOT, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".tmp",
                                                  "runs", ".ruff_cache", ".pytest_cache",
                                                  "_tmp-feedback", ".git"))


def mutate_skill(root: Path, change: str) -> None:
    md = root / SKILL_REL / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    if change == "bad-name":
        text = re.sub(r"^name:.*$", "name: Wrong_Name", text, count=1, flags=re.MULTILINE)
    elif change == "bad-version":
        text = re.sub(r"^version:.*$", "version: nope", text, count=1, flags=re.MULTILINE)
    elif change == "no-description":
        text = re.sub(r"^description:.*$", "", text, count=1, flags=re.MULTILINE)
    elif change == "oversized":
        text += "\n" + "\n".join("x" * 80 for _ in range(520)) + "\n"
    elif change == "broken-link":
        text += "\n[missing](does-not-exist.md)\n"
    else:
        raise ValueError(change)
    md.write_text(text, encoding="utf-8")


def selftest() -> int:
    checks = failed = 0

    def ok(cond: bool, label: str) -> None:
        nonlocal checks, failed
        checks += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "valid" / "repo"
        copy_repo(base)
        errors, _warnings = validate(base)
        ok(not errors, "valid copy passes")
        for change in ("bad-name", "bad-version", "no-description", "oversized",
                       "broken-link"):
            case = Path(td) / change / "repo"
            shutil.copytree(base, case)
            mutate_skill(case, change)
            errs, _ = validate(case)
            ok(bool(errs), f"validator fails on {change}")
        case = Path(td) / "forbidden-case" / "repo"
        shutil.copytree(base, case)
        (case / "__pycache__").mkdir()
        (case / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
        errs, _ = validate(case)
        ok(any("forbidden artifact" in e for e in errs), "validator fails on forbidden artifact")

    print(f"\n  PASS {checks - failed} | FAIL {failed}")
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate opencode-export-reading layout")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    errors, warnings = validate(REPO_ROOT)
    return 0 if print_result("opencode-export-reading", errors, warnings) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
