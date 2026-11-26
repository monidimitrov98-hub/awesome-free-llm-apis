"""Historical market-data infrastructure (Sprint 12) -- ingestion, validation, versioning, readiness.

DATA QUALITY ONLY. Nothing here downloads data, uses credentials, places orders or touches any safety layer.
Provider files are obtained with the provider's own tools, then ingested here into immutable datasets.

    python run_data.py adapters
    python run_data.py ingest --dataset-id databento.mes.1m --adapter databento_ohlcv_csv --file a.csv [--file b.csv]
    python run_data.py ingest --dataset-id nt.mes.1m --adapter ninjatrader_export --param timezone=America/Chicago \\
        --file "MES 03-25.Last.txt" [--provenance source_facts.json]
    python run_data.py ingest --dataset-id databento.mes.1m --adapter databento_ohlcv_csv --file new.csv \\
        --base databento.mes.1m@v1                 (incremental update -> new version; history never changes)
    python run_data.py list
    python run_data.py show databento.mes.1m@v1
    python run_data.py verify databento.mes.1m@v1
    python run_data.py continuous --dataset-id databento.mes.1m.cont --source databento.mes.1m@v1 --rule volume
    python run_data.py cross-validate databento.mes.1m@v1 other.mes.1m@v1 [--start 2025-01-02 --end 2025-03-31]
    python run_data.py readiness databento.mes.1m.cont@v1
    python run_data.py conflicts nt.mes.1m@v1 nt.mes.z26.1m@v1         (read-only minute-by-minute comparison)
    python run_data.py conflicts A@v1 B@v1 --record --by NAME [--note TEXT]   (HUMAN: record a data conflict)
    python run_data.py supersede A@v1 --with B@v1 --by NAME --reason TEXT    (HUMAN: resolve it; nothing deleted)
    python run_data.py recover

Exit codes: 0 ok, 1 refused / error, 3 cross-validation FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def _kv(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        if not k or not _:
            raise ConfigError(f"--param needs key=value (got {it!r})")
        out[k.strip()] = v.strip()
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Historical market-data infrastructure (data quality only).")
    p.add_argument("--settings", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--root", type=Path, default=None, help="dataset store (default data/datasets or FTS_DATASETS_ROOT)")
    p.add_argument("--calendar", type=Path, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("adapters")
    i = sub.add_parser("ingest")
    i.add_argument("--dataset-id", required=True)
    i.add_argument("--adapter", required=True)
    i.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    i.add_argument("--file", action="append", type=Path, required=True)
    i.add_argument("--resolution", default="1min")
    i.add_argument("--description", default="")
    i.add_argument("--base", default=None, help="extend this version (incremental update)")
    i.add_argument("--allow-corrections", action="store_true")
    i.add_argument("--provenance", type=Path, default=None,
                   help="JSON file of recorded source facts (e.g. timezone evidence); stored in the manifest")
    sub.add_parser("list")
    for name in ("show", "verify", "readiness"):
        s = sub.add_parser(name)
        s.add_argument("ref")
    c = sub.add_parser("continuous")
    c.add_argument("--dataset-id", required=True)
    c.add_argument("--source", action="append", required=True)
    c.add_argument("--rule", choices=("calendar", "volume"), default="calendar")
    c.add_argument("--days-before-expiry", type=int, default=5)
    c.add_argument("--confirm-days", type=int, default=1)
    c.add_argument("--adjustment", choices=("none", "difference", "ratio"), default="none")
    c.add_argument("--description", default="")
    x = sub.add_parser("cross-validate")
    x.add_argument("a")
    x.add_argument("b")
    x.add_argument("--start", default=None)
    x.add_argument("--end", default=None)
    k = sub.add_parser("conflicts", help="compare two per-contract datasets minute by minute (read-only unless "
                                         "--record)")
    k.add_argument("a")
    k.add_argument("b")
    k.add_argument("--record", action="store_true", help="HUMAN: record the differences as a data conflict")
    k.add_argument("--by", default="")
    k.add_argument("--note", default="")
    k.add_argument("--show", type=int, default=20, help="conflicting minutes to print (all are in the report)")
    s = sub.add_parser("supersede", help="HUMAN: declare OLD superseded by NEW (needs a recorded conflict)")
    s.add_argument("old")
    s.add_argument("--with", dest="new", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--reason", required=True)
    d = sub.add_parser("downgrade", help="HUMAN: mark a dataset VALIDATION_FAILED after a discovered problem")
    d.add_argument("ref")
    d.add_argument("--by", required=True)
    d.add_argument("--reason", required=True)
    sub.add_parser("recover")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    from src.histdata.calendar import load_calendar
    from src.histdata.providers import ADAPTERS, make_adapter
    from src.histdata.store import DatasetStore

    try:
        app = load_config(a.settings)
        cal = load_calendar(a.calendar)
        store = DatasetStore(a.root)
        if a.cmd == "adapters":
            for n, cls in ADAPTERS.items():
                print(f"{n}: {cls.description} (params: {', '.join(cls.params_allowed) or '-'})")
            return 0
        if a.cmd == "ingest":
            params = _kv(a.param)
            if "columns" in params:
                params["columns"] = json.loads(params["columns"])
            prov = None
            if a.provenance is not None:
                try:
                    prov = json.loads(a.provenance.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ConfigError(f"{a.provenance}: invalid JSON: {exc}") from exc
            r = store.ingest(a.dataset_id, make_adapter(a.adapter, **params), a.file, specs=app.contracts,
                             calendar=cal, resolution=a.resolution, description=a.description, base=a.base,
                             allow_corrections=a.allow_corrections, provenance=prov)
            print(json.dumps(r, indent=2, default=str))
            if r.get("created") and r["status"] == "VALIDATION_FAILED":
                print("VALIDATION FAILED: see `run_data.py show` (the dataset is kept, unusable for research)")
            return 0
        if a.cmd == "list":
            from src.histdata.conflicts import usage_refusal

            for v in store.versions():
                ref = f"{v['dataset_id']}@{v['version']}"
                v["data_conflict"] = usage_refusal(store.conflicts.state(ref))
                print(json.dumps(v, default=str))
            return 0
        if a.cmd == "show":
            ds = store.load(a.ref)
            v = ds.validation()
            print(json.dumps({"identity": ds.identity(), "status_history": ds.status_history,
                              "conflict_state": ds.conflict_state,
                              "manifest": {k: ds.manifest[k] for k in ("provider", "source_files", "contracts",
                                                                     "first_bar_utc", "last_bar_utc", "calendar",
                                                                     "lineage", "description")},
                              "gate": v["gate"], "findings": v["findings"]}, indent=2, default=str))
            return 0
        if a.cmd == "verify":
            ds = store.load(a.ref)
            print(f"{ds.ref}: manifest and every file verified ({ds.status})")
            return 0
        if a.cmd == "readiness":
            from src.histdata.readiness import assess

            print(json.dumps(assess(store.load(a.ref)), indent=2, default=str))
            return 0
        if a.cmd == "continuous":
            r = store.create_continuous(a.dataset_id, a.source, specs=app.contracts, calendar=cal, rule=a.rule,
                                        days_before_expiry=a.days_before_expiry, confirm_days=a.confirm_days,
                                        adjustment=a.adjustment, description=a.description)
            print(json.dumps(r, indent=2, default=str))
            return 0
        if a.cmd == "cross-validate":
            from src.histdata.crossval import compare

            ra, rb = store.load(a.a), store.load(a.b)
            for x in (ra, rb):
                if x.usage_refusal():
                    raise ConfigError(x.usage_refusal())
            rep = compare(ra, rb, cal, app.contracts, start=a.start, end=a.end)
            out = store.root / "_reports"
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"crossval_{rep['report_sha256'][:16]}.json"
            path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
            print(json.dumps({k: rep[k] for k in ("result", "problems", "rth", "ohlc", "volume",
                                                  "best_timestamp_shift_minutes", "common_trade_dates")},
                             indent=2, default=str))
            print(f"report: {path}")
            if rep["result"] != "PASS":
                return 3
            ev = {"report_sha256": rep["report_sha256"], "report": str(path), "against": None}
            for x, y in ((ra, rb), (rb, ra)):
                store.set_status(x.ref, "CROSS_SOURCE_VALIDATED", by="cross-validate",
                                 evidence={**ev, "against": y.ref})
            print(f"{ra.ref} and {rb.ref}: CROSS_SOURCE_VALIDATED")
            return 0
        if a.cmd == "conflicts":
            rep = store.compare(a.a, a.b)
            summary = {"a": rep["a"]["ref"], "b": rep["b"]["ref"], "report_sha256": rep["report_sha256"],
                       "totals": rep["totals"], "contracts": rep["contracts"],
                       "conflicts": rep["conflicts"][:max(a.show, 0)]}
            print(json.dumps(summary, indent=2, default=str))
            if a.record:
                rec = store.record_conflict(rep, by=a.by, note=a.note)
                print(f"conflict {rec['conflict_id']} recorded: {rec['a']} and {rec['b']} are refused for research "
                      "until one supersedes the other (`run_data.py supersede`)")
            else:
                from src.histdata.conflicts import has_conflict

                print("read-only comparison: nothing recorded" + (" (use --record --by NAME to record the "
                                                                  "conflict)" if has_conflict(rep) else ""))
            return 0
        if a.cmd == "supersede":
            rec = store.supersede(a.old, a.new, by=a.by, reason=a.reason)
            print(f"{rec['dataset']} superseded by {rec['superseded_by']} (conflict(s) {', '.join(rec['conflict_ids'])})")
            return 0
        if a.cmd == "downgrade":
            store.set_status(a.ref, "VALIDATION_FAILED", by=a.by, evidence={"reason": a.reason[:500]})
            print(f"{a.ref}: VALIDATION_FAILED recorded")
            return 0
        if a.cmd == "recover":
            print(json.dumps(store.recover(), indent=2))
            return 0
    except (ConfigError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
