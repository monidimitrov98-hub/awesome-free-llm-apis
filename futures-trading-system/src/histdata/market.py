"""Datasets -> the existing Sprint 1 pipeline (MarketData) for the backtester and the research engine.

Refusals (a research run can never silently use the wrong prices):

* adjusted continuous series (simulated fills, ticks and P&L need real contract prices);
* a per-contract dataset with several contracts unless ONE contract is selected (contracts are never merged;
  use a continuous dataset with its explicit roll table instead);
* a dataset whose stored bars do not match its manifest (verified by the store on load).
"""

from __future__ import annotations

from src.config import AppConfig, ConfigError
from src.data.contracts import ContractInfo, parse_contract_symbol
from src.data.report import DataQualityReport
from src.histdata.store import Dataset
from src.pipeline import MarketData, build_market_data_from_bars


def market_data_from_dataset(ds: Dataset, app: AppConfig, *, contract: str | None = None,
                             holdout_guard: bool = True) -> MarketData:
    """``holdout_guard`` (default on): bars of a reserved holdout window are refused before any computation. Only
    the final OOS evaluation, after its claim is durably CONSUMED, loads with the guard off (src/research/runs.py)."""
    m = ds.manifest
    if m["adjustment"] != "none":
        raise ConfigError(f"{ds.ref} is {m['adjustment']}-adjusted: refused for simulation (fills, ticks and P&L "
                          "must use real contract prices)")
    bars = ds.bars()
    if len(m["roots"]) != 1:
        raise ConfigError(f"{ds.ref} holds roots {m['roots']}: one root per run")
    root = m["roots"][0]
    if m["kind"] == "per_contract":
        names = sorted(m["contracts"])
        if contract is None and len(names) > 1:
            raise ConfigError(f"{ds.ref} holds contracts {names}: select one (contracts are never merged) or "
                              "build a continuous dataset")
        pick = contract or names[0]
        if pick not in names:
            raise ConfigError(f"{ds.ref} has no contract {pick}")
        bars = bars[bars["contract"] == pick]
        info = parse_contract_symbol(pick, app.contracts)
        source = "dataset_contract"
    else:
        info = ContractInfo(raw=f"{root} continuous ({ds.ref})", root=root, month=None, year=None,
                            spec=app.contracts.get(root))
        source = "dataset_continuous"
    if info.spec is None:
        raise ConfigError(f"no contract specification for {root}")
    df = bars.set_index("ts_utc")[["open", "high", "low", "close", "volume"]].astype("float64")
    df.index.name = "timestamp"
    report = DataQualityReport(source=f"dataset:{ds.ref}")
    report.rows_read = len(df)
    notes = [f"dataset {ds.ref} ({ds.status}, manifest {m['manifest_sha256'][:12]})"]
    if m["synthetic"]:
        report.add("info", "SYNTHETIC_DATA", "Dataset is marked SYNTHETIC: values are not real market prices")
    if m["kind"] == "continuous":
        report.add("warning", "CONTINUOUS_UNADJUSTED", f"continuous unadjusted series: prices jump at the "
                   f"{m['lineage']['rolls']} roll(s) listed in rolls.csv; bars are real contract prices")
    ident = {"ref": ds.ref, "manifest_sha256": m["manifest_sha256"], "bars_content_sha256": m["bars_content_sha256"],
             "files_verified": ds.files_verified}
    return build_market_data_from_bars(df, app, contract=info, contract_source=source, report=report,
                                       is_synthetic=bool(m["synthetic"]), notes=notes, holdout_guard=holdout_guard,
                                       dataset_identity=ident)


__all__ = ["market_data_from_dataset"]
