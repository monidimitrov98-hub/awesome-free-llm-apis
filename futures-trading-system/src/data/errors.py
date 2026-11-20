"""Exceptions raised by the market data engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.report import DataQualityReport


class DataError(ValueError):
    """Base class for market data problems."""


class TimestampError(DataError):
    """Timestamps could not be normalized (e.g. ambiguous DST times)."""


class ContractError(DataError):
    """A contract symbol could not be identified or is inconsistent."""


class DataValidationError(DataError):
    """Validation found errors and the configuration says to stop.

    The full report is available as ``exc.report``.
    """

    def __init__(self, report: "DataQualityReport") -> None:
        self.report = report
        super().__init__("Data validation failed:\n" + report.summary())
