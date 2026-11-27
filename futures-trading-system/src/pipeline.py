"""End-to-end Sprint 1 pipeline: CSV -> validated UTC bars -> resampled frames with indicators."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.config import AppConfig
from src.data.contracts import ContractInfo, identify_contract
from src.data.errors import DataError, DataValidationError
from src.data.loader import load_csv
from src.data.report import DataQualityReport
from src.data.resample import check_bin_alignment, check_session_alignment, resample_ohlcv
from src.data.sessions import assign_session_dates
from src.data.validation import validate_ohlcv
from src.indicators.compute import add_indicators


@dataclass
class MarketData:
    """Output of :func:`build_market_data`."""

    contract: ContractInfo
    contract_source: str
    base: pd.DataFrame
    frames: dict[str, pd.DataFrame]
    report: DataQualityReport
    is_synthetic: bool = False
    notes: list[str] = field(default_factory=list)
    # Sprint 13 P1.1: identity of the verified dataset the bars came from (None for a CSV file)
    dataset_identity: dict | None = None


def _is_synthetic_file(path: Path) -> bool:
    """Detect the synthetic marker in the file name or the leading comment lines."""
    if "SYNTHETIC" in path.name.upper():
        return True
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(5):
            line = fh.readline()
            if not line.startswith("#"):
                break
            if "SYNTHETIC" in line.upper():
                return True
    return False


def build_market_data(csv_path: str | Path, config: AppConfig, *, holdout_guard: bool = False) -> MarketData:
    """Run the full market data pipeline on one CSV file.

    Steps: load -> UTC timestamps -> contract identification -> validation ->
    (raise or drop invalid rows) -> optional removal of out-of-session bars ->
    resampling to each configured timeframe -> indicators.

    Args:
        csv_path: Path to the CSV file.
        config: Loaded configuration.

    Returns:
        :class:`MarketData` with one indicator frame per timeframe.

    Raises:
        DataValidationError: If validation finds errors and ``data.on_error == "raise"``.
        DataError: For unreadable files, unknown contracts or unusable data.

    ``holdout_guard`` (Sprint 13 P1.1, research and backtest callers): after the file is read (a data-quality
    read) and BEFORE validation, resampling or any indicator, a file containing bars of a reserved holdout
    window is refused (src/research/holdout.py).
    """
    path = Path(csv_path)
    dcfg = config.data
    report = DataQualityReport(source=str(path))
    df, report = load_csv(path, dcfg, report)
    is_synthetic = _is_synthetic_file(path)
    if is_synthetic:
        report.add("info", "SYNTHETIC_DATA", "File is marked SYNTHETIC: values are not real market prices")

    # Contract identification (optionally filtering a multi-contract file).
    symbols = None
    if "symbol" in df.columns:
        if dcfg.symbol_filter:
            keep = (df["symbol"] == dcfg.symbol_filter.upper()).to_numpy()
            if not keep.any():
                raise DataError(f"symbol_filter '{dcfg.symbol_filter}' matches no rows")
            dropped = int((~keep).sum())
            if dropped:
                report.add("info", "SYMBOL_FILTER", f"Kept only {dcfg.symbol_filter}", count=dropped)
                report.rows_dropped += dropped
            df = df[keep]
        symbols = df["symbol"].dropna().unique().tolist()
    reference_year = int(df.index[0].year) if len(df) else None
    contract, source, notes = identify_contract(
        specs=config.contracts,
        symbols=symbols,
        filename=path.name,
        default_symbol=dcfg.default_symbol,
        reference_year=reference_year,
    )
    for note in notes:
        report.add("warning", "CONTRACT_NOTE", note)
    if contract.spec is None:
        raise DataError(f"No contract specification configured for root {contract.root}")
    if contract.is_continuous:
        report.add(
            "warning",
            "CONTINUOUS_OR_UNKNOWN_MONTH",
            f"Contract month unknown for {contract.root}; if this is a back-adjusted/continuous "
            "series, roll gaps may distort indicators",
        )

    return build_market_data_from_bars(df, config, contract=contract, contract_source=source, report=report,
                                       is_synthetic=is_synthetic, notes=notes, holdout_guard=holdout_guard)


def build_market_data_from_bars(df: pd.DataFrame, config: AppConfig, *, contract: ContractInfo, contract_source: str,
                                report: DataQualityReport, is_synthetic: bool, notes: list[str],
                                holdout_guard: bool = False, dataset_identity: dict | None = None) -> MarketData:
    """Validation -> optional out-of-session removal -> resampling -> indicators, for bars that are already
    loaded with a UTC index (a CSV via :func:`build_market_data`, or a Sprint 12 dataset).

    ``holdout_guard``: refuse bars of a reserved holdout window FIRST, before any validation, resampling or
    indicator computation (Sprint 13 P1.1)."""
    if holdout_guard:
        from src.research.holdout import refuse_reserved_bars

        refuse_reserved_bars(contract.root, df.index, "the input")
    dcfg = config.data
    # Validation.
    session = config.session
    report, invalid = validate_ohlcv(
        df,
        base_frequency=dcfg.base_frequency,
        session=session,
        tick_size=contract.spec.tick_size,
        report=report,
    )
    if report.has_errors:
        if dcfg.on_error == "raise":
            raise DataValidationError(report)
        n_bad = int(invalid.sum())
        report.add("info", "ROWS_DROPPED", "Rows with validation errors removed (on_error='drop')", count=n_bad)
        report.rows_dropped += n_bad
        df = df[~invalid]

    if dcfg.drop_outside_session:
        outside = assign_session_dates(df.index, session).isna().to_numpy()
        if outside.any():
            report.add("info", "OUTSIDE_SESSION_DROPPED", "Out-of-session bars removed", count=int(outside.sum()))
            report.rows_dropped += int(outside.sum())
            df = df[~outside]

    if df.empty:
        raise DataError("No usable rows left after validation")

    base = df[["open", "high", "low", "close", "volume"]].astype("float64")
    frames: dict[str, pd.DataFrame] = {}
    for tf in config.timeframes:
        check_session_alignment(session, tf)
        check_bin_alignment(base.index, session, tf)
        bars = resample_ohlcv(base, tf, dcfg.base_frequency)
        frames[tf] = add_indicators(bars, config.indicators, session)
        incomplete = int((~bars["is_complete"]).sum())
        if incomplete:
            report.add(
                "info",
                "INCOMPLETE_BARS",
                f"{tf}: bars whose source timestamps do not exactly match the expected grid "
                "(missing and/or off-grid source bars); is_complete=False. "
                "Never generate signals from these bars",
                count=incomplete,
            )

    return MarketData(
        contract=contract,
        contract_source=contract_source,
        base=base,
        frames=frames,
        report=report,
        is_synthetic=is_synthetic,
        notes=notes,
        dataset_identity=dataset_identity,
    )


__all__ = ["MarketData", "build_market_data", "build_market_data_from_bars"]
