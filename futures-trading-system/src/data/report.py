"""Data quality reporting.

Every stage of the pipeline records what it found here instead of silently
fixing data. Nothing in the engine fills gaps or invents values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import pandas as pd

Severity = Literal["error", "warning", "info"]
MAX_EXAMPLES = 5


@dataclass
class Issue:
    """A single finding."""

    severity: Severity
    code: str
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = f"[{self.severity.upper()}] {self.code}: {self.message}"
        if self.count:
            text += f" (count={self.count})"
        if self.examples:
            text += f" e.g. {', '.join(self.examples)}"
        return text


@dataclass
class DataQualityReport:
    """Collects issues from loading, normalization and validation."""

    source: str = ""
    issues: list[Issue] = field(default_factory=list)
    rows_read: int = 0
    rows_dropped: int = 0
    gaps: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=["session_date", "gap_start", "gap_end", "missing_bars"])
    )

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        count: int = 0,
        examples: Iterable[object] = (),
    ) -> Issue:
        """Record an issue and return it."""
        ex = [str(e) for e in list(examples)[:MAX_EXAMPLES]]
        issue = Issue(severity=severity, code=code, message=message, count=count, examples=ex)
        self.issues.append(issue)
        return issue

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def codes(self) -> set[str]:
        """Set of issue codes, convenient for tests and filtering."""
        return {i.code for i in self.issues}

    def get(self, code: str) -> Issue | None:
        """Return the first issue with ``code`` or None."""
        return next((i for i in self.issues if i.code == code), None)

    def summary(self) -> str:
        """Human-readable multi-line summary."""
        lines = [
            f"Data quality report for: {self.source or '<unknown>'}",
            f"  rows read: {self.rows_read}, rows dropped: {self.rows_dropped}",
            f"  errors: {len(self.errors)}, warnings: {len(self.warnings)}",
        ]
        order = {"error": 0, "warning": 1, "info": 2}
        for issue in sorted(self.issues, key=lambda i: order[i.severity]):
            lines.append(f"  {issue}")
        return "\n".join(lines)
