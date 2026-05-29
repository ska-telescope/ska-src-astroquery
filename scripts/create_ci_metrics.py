#!/usr/bin/env python3
"""
Generate SVG badge files from pytest and ruff JUnit XML reports.

Reads:
  build/reports/unit-tests.xml      — pytest JUnit output
  build/reports/code-coverage.xml   — pytest-cov Cobertura XML
  build/reports/linting.xml         — ruff JUnit output

Writes:
  build/badges/build_last_date.svg
  build/badges/tests_total.svg
  build/badges/tests_failures.svg
  build/badges/tests_errors.svg
  build/badges/tests_skipped.svg
  build/badges/coverage.svg
  build/badges/lint_total.svg
  build/badges/lint_failures.svg
  build/badges/lint_errors.svg
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import anybadge

BADGES_DIR = Path("build/badges")
REPORTS_DIR = Path("build/reports")
BADGES_DIR.mkdir(parents=True, exist_ok=True)


def _badge(label: str, value: str | int | float, color: str) -> None:
    path = BADGES_DIR / f"{label}.svg"
    anybadge.Badge(label.replace("_", " "), str(value), default_color=color).write_badge(
        str(path), overwrite=True
    )
    print(f"  {label}: {value}  [{color}]")


def _parse_junit(path: Path) -> tuple[int, int, int, int]:
    """Return (total, failures, errors, skipped) from a JUnit XML file."""
    if not path.exists():
        return 0, 0, 0, 0
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else (root.find("testsuite") or root)
    total    = int(suite.get("tests",    0))
    failures = int(suite.get("failures", 0))
    errors   = int(suite.get("errors",   0))
    skipped  = int(suite.get("skipped",  0))
    # ruff's junit: violations appear only as <failure> children — count them directly
    if total == 0:
        failures = len(root.findall(".//failure"))
        errors   = len(root.findall(".//error"))
        total    = len(root.findall(".//testcase"))
    return total, failures, errors, skipped


def _parse_coverage(path: Path) -> float:
    """Return line coverage percentage from a Cobertura XML file."""
    if not path.exists():
        return 0.0
    root = ET.parse(path).getroot()
    return round(float(root.get("line-rate", 0)) * 100, 1)


# ── Build date ────────────────────────────────────────────────────────────────
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
_badge("build_last_date", today, "blue")

# ── Test metrics ──────────────────────────────────────────────────────────────
total, failures, errors, skipped = _parse_junit(REPORTS_DIR / "unit-tests.xml")
_badge("tests_total",    total,    "blue")
_badge("tests_failures", failures, "green" if failures == 0 else "red")
_badge("tests_errors",   errors,   "green" if errors   == 0 else "red")
_badge("tests_skipped",  skipped,  "green" if skipped  == 0 else "yellow")

# ── Coverage ──────────────────────────────────────────────────────────────────
cov = _parse_coverage(REPORTS_DIR / "code-coverage.xml")
cov_color = "green" if cov >= 90 else "orange" if cov >= 75 else "red"
_badge("coverage", f"{cov}%", cov_color)

# ── Lint metrics ──────────────────────────────────────────────────────────────
lint_total, lint_failures, lint_errors, _ = _parse_junit(REPORTS_DIR / "linting.xml")
_badge("lint_total",    lint_total,    "blue")
_badge("lint_failures", lint_failures, "green" if lint_failures == 0 else "orange")
_badge("lint_errors",   lint_errors,   "green" if lint_errors   == 0 else "red")

print(f"\nBadges written to {BADGES_DIR}/")
