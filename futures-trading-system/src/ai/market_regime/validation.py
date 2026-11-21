"""Historical validation of the regime classifiers (chronological, purged, embargoed).

* Folds are CHRONOLOGICAL walk-forward folds over session dates (never random splits):
  train on sessions before the test window, test on the next ``test_sessions`` sessions.
* Labels look ``horizon`` bars ahead, so every training row whose label window ends at or
  after ``test_start - embargo`` is PURGED; the embargo adds a further gap.
* Rules (Layer 1) need no fitting, but their confidence calibration is fitted on the
  training rows only. The ML model (Layer 2) is trained on the earlier part of the
  training rows and calibrated on the later part (also purged), then tested.
* The last ``oos_sessions`` sessions are a FINAL out-of-sample period: excluded from every
  fold and only evaluated when explicitly revealed (recorded in the validation ledger).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from src.ai.market_regime import ml
from src.ai.market_regime.classifier import classify_frame, predicted_class
from src.ai.market_regime.confidence import BinnedCalibrator, calibration_report
from src.ai.market_regime.config import RegimeConfig
from src.ai.market_regime.features import add_regime_features
from src.ai.market_regime.labels import forward_labels

LABELS = (-1.0, 0.0, 1.0)
LABEL_NAMES = {-1.0: "trend_down", 0.0: "no_trend", 1.0: "trend_up"}


def build_dataset(bars: pd.DataFrame, rc: RegimeConfig, session, **kw) -> pd.DataFrame:
    feats = add_regime_features(bars, rc.features, session, **kw)
    lab = forward_labels(bars, rc.labels, kw.get("atr_col", "atr_14"))
    rules = classify_frame(feats, rc.classification)
    out = feats.join(lab).join(rules.add_prefix("rule_"))
    out["rule_pred"] = [predicted_class(d, t) for d, t in zip(out["rule_direction"], out["rule_trend"])]
    out["session"] = pd.to_datetime(out["session_date"])
    return out


def folds(sessions: list[pd.Timestamp], train: int, test: int) -> list[tuple[list, list]]:
    out, i = [], train
    while i + test <= len(sessions):
        out.append((sessions[i - train:i], sessions[i:i + test]))
        i += test
    return out


def purged_train_mask(ds: pd.DataFrame, train_sessions: list, test_start: pd.Timestamp, step: pd.Timedelta,
                      embargo_bars: int) -> pd.Series:
    """Training rows: in the training sessions, labelled, and whose label window ends
    before ``test_start - embargo`` (no overlap of label horizons with the test period)."""
    limit = test_start - embargo_bars * step
    return ds["session"].isin(train_sessions) & ds["label"].notna() & ds["rf_valid"] \
        & (pd.to_datetime(ds["label_end"]) < limit)


def classification_metrics(y: np.ndarray, pred: np.ndarray) -> dict[str, Any]:
    from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

    mask = np.isfinite(y) & np.isfinite(pred)
    y, pred = y[mask], pred[mask]
    if y.size == 0:
        return {"n": 0}
    p, r, f, s = precision_recall_fscore_support(y, pred, labels=list(LABELS), zero_division=0)
    majority = max(LABELS, key=lambda c: (y == c).sum())
    return {
        "n": int(y.size), "accuracy": float((y == pred).mean()),
        "majority_baseline_accuracy": float((y == majority).mean()), "majority_class": LABEL_NAMES[majority],
        "per_class": {LABEL_NAMES[c]: {"precision": float(p[i]), "recall": float(r[i]), "f1": float(f[i]),
                                       "support": int(s[i])} for i, c in enumerate(LABELS)},
        "confusion_matrix": {"labels": [LABEL_NAMES[c] for c in LABELS],
                             "rows_true_cols_pred": confusion_matrix(y, pred, labels=list(LABELS)).tolist()},
    }


def transition_detection(ds: pd.DataFrame, pred_col: str, max_lag: int) -> dict[str, Any]:
    """For each change of the (forward) label inside a session, bars until the prediction matches."""
    lags, missed = [], 0
    for _, g in ds.groupby("session"):
        y, p = g["label"].to_numpy(), g[pred_col].to_numpy()
        for i in range(1, len(y)):
            if not (np.isfinite(y[i]) and np.isfinite(y[i - 1])) or y[i] == y[i - 1]:
                continue
            hit = next((k for k in range(0, max_lag + 1) if i + k < len(p) and p[i + k] == y[i]), None)
            if hit is None:
                missed += 1
            else:
                lags.append(hit)
    n = len(lags) + missed
    return {"transitions": n, "detected_within_horizon": len(lags), "detection_rate": len(lags) / n if n else None,
            "mean_lag_bars": float(np.mean(lags)) if lags else None}


def _evaluate_predictions(test: pd.DataFrame, pred_col: str, prob_col: str, horizon: int) -> dict[str, Any]:
    y, pred = test["label"].to_numpy(dtype=float), test[pred_col].to_numpy(dtype=float)
    res = classification_metrics(y, pred)
    correct = (y == pred).astype(float)
    res["calibration"] = calibration_report(test[prob_col].to_numpy(dtype=float), np.where(np.isfinite(y), correct, np.nan))
    per_session = test.groupby("session").apply(
        lambda g: float((g["label"] == g[pred_col]).mean()) if g["label"].notna().any() else np.nan,
        include_groups=False).dropna()
    res["stability_by_session"] = {"sessions": int(per_session.size),
                                   "accuracy_mean": float(per_session.mean()) if per_session.size else None,
                                   "accuracy_std": float(per_session.std()) if per_session.size > 1 else None,
                                   "accuracy_min": float(per_session.min()) if per_session.size else None}
    res["by_volatility"] = {str(k): classification_metrics(g["label"].to_numpy(float), g[pred_col].to_numpy(float))
                            for k, g in test.groupby(test["rule_volatility"].fillna("unknown"))}
    res["transitions"] = transition_detection(test, pred_col, horizon)
    return res


def _rules_with_calibration(ds: pd.DataFrame, train_mask: pd.Series, rc: RegimeConfig) -> pd.Series:
    tr = ds[train_mask]
    cal = BinnedCalibrator().fit(tr["rule_pred"].to_numpy(float), tr["rule_trend_score"].to_numpy(float),
                                 tr["label"].to_numpy(float))
    probs = [cal.predict(p, s)[0] if math.isfinite(p) else None
             for p, s in zip(ds["rule_pred"], ds["rule_trend_score"])]
    return pd.Series([np.nan if v is None else v for v in probs], index=ds.index), cal


def _ml_fit_predict(ds: pd.DataFrame, train_mask: pd.Series, test_mask: pd.Series, rc: RegimeConfig,
                    step: pd.Timedelta) -> tuple[pd.Series, pd.Series, dict[str, Any]]:
    tr = ds[train_mask]
    sessions = sorted(tr["session"].unique())
    cut = sessions[max(1, int(len(sessions) * 0.8)) - 1] if len(sessions) > 1 else sessions[-1]
    fit_mask = train_mask & purged_train_mask(ds, [s for s in sessions if s <= cut],
                                              pd.Timestamp(ds.index[ds["session"] > cut][0]) if (ds["session"] > cut).any()
                                              else ds.index[-1], step, rc.validation.embargo_bars)
    cal_mask = train_mask & (ds["session"] > cut)
    model = ml.fit(rc.ml, ml.feature_matrix(ds[fit_mask]), ds.loc[fit_mask, "label"].to_numpy(float))
    cal_pred, cal_prob = ml.predict(model, rc.ml, ml.feature_matrix(ds[cal_mask])) if cal_mask.any() else (np.array([]), np.array([]))
    cal = BinnedCalibrator().fit(cal_pred, cal_prob, ds.loc[cal_mask, "label"].to_numpy(float))
    pred, raw = ml.predict(model, rc.ml, ml.feature_matrix(ds[test_mask]))
    prob = [cal.predict(p, s)[0] for p, s in zip(pred, raw)]
    info = {"fit_rows": int(fit_mask.sum()), "calibration_rows": int(cal_mask.sum())}
    return (pd.Series(pred, index=ds.index[test_mask]),
            pd.Series([np.nan if v is None else v for v in prob], index=ds.index[test_mask]), info)


def evaluate_instrument(ds: pd.DataFrame, rc: RegimeConfig, oos_sessions: int, reveal_oos: bool,
                        truth: pd.Series | None = None) -> dict[str, Any]:
    step = pd.Timedelta(ds["available_at"].iloc[0] - ds.index[0])
    sessions = sorted(ds["session"].dropna().unique())
    if len(sessions) <= oos_sessions + rc.validation.train_sessions:
        raise ValueError(f"{len(sessions)} sessions: not enough for {rc.validation.train_sessions} training + "
                         f"{oos_sessions} final OOS sessions")
    dev, final = sessions[:-oos_sessions], sessions[-oos_sessions:]
    labelled = ds["label"].notna() & ds["rf_valid"]
    report: dict[str, Any] = {
        "sessions": len(sessions), "development_sessions": [str(dev[0].date()), str(dev[-1].date()), len(dev)],
        "final_oos_sessions": [str(final[0].date()), str(final[-1].date()), len(final)],
        "labelled_valid_bars": int(labelled.sum()),
        "label_distribution": {LABEL_NAMES[c]: int((ds.loc[labelled, "label"] == c).sum()) for c in LABELS},
        "walk_forward": [],
    }
    pooled = {"rules": [], "ml": []}
    for k, (tr_s, te_s) in enumerate(folds(dev, rc.validation.train_sessions, rc.validation.test_sessions), 1):
        test_start = ds.index[ds["session"] == te_s[0]][0]
        train_mask = purged_train_mask(ds, tr_s, test_start, step, rc.validation.embargo_bars)
        test_mask = ds["session"].isin(te_s) & labelled
        rule_prob, _ = _rules_with_calibration(ds, train_mask, rc)
        test = ds[test_mask].copy()
        test["rule_prob"] = rule_prob[test_mask]
        fold = {"fold": k, "train": [str(tr_s[0].date()), str(tr_s[-1].date())],
                "test": [str(te_s[0].date()), str(te_s[-1].date())], "train_rows": int(train_mask.sum()),
                "max_train_label_end": str(pd.to_datetime(ds.loc[train_mask, "label_end"]).max()),
                "test_start": str(test_start), "rules": _evaluate_predictions(test, "rule_pred", "rule_prob",
                                                                            rc.labels.horizon_bars)}
        try:
            pred, prob, info = _ml_fit_predict(ds, train_mask, test_mask, rc, step)
            test["ml_pred"], test["ml_prob"] = pred, prob
            fold["ml"] = _evaluate_predictions(test, "ml_pred", "ml_prob", rc.labels.horizon_bars) | info
            pooled["ml"].append(test[["label", "ml_pred", "ml_prob", "session", "rule_volatility"]])
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            fold["ml"] = {"error": f"{type(exc).__name__}: {exc}"}
        if truth is not None:
            t = truth.reindex(test.index)
            fold["rules_vs_ground_truth"] = classification_metrics(t.to_numpy(float), test["rule_pred"].to_numpy(float))
        pooled["rules"].append(test[["label", "rule_pred", "rule_prob", "session", "rule_volatility"]])
        report["walk_forward"].append(fold)
    for name, parts in pooled.items():
        if parts:
            allp = pd.concat(parts)
            report[f"pooled_walk_forward_{name}"] = classification_metrics(
                allp["label"].to_numpy(float), allp[f"{'rule' if name == 'rules' else 'ml'}_pred"].to_numpy(float))
    if reveal_oos:
        test_start = ds.index[ds["session"] == final[0]][0]
        train_mask = purged_train_mask(ds, dev, test_start, step, rc.validation.embargo_bars)
        test_mask = ds["session"].isin(final) & labelled
        rule_prob, _ = _rules_with_calibration(ds, train_mask, rc)
        test = ds[test_mask].copy()
        test["rule_prob"] = rule_prob[test_mask]
        oos = {"rules": _evaluate_predictions(test, "rule_pred", "rule_prob", rc.labels.horizon_bars)}
        try:
            pred, prob, info = _ml_fit_predict(ds, train_mask, test_mask, rc, step)
            test["ml_pred"], test["ml_prob"] = pred, prob
            oos["ml"] = _evaluate_predictions(test, "ml_pred", "ml_prob", rc.labels.horizon_bars) | info
        except Exception as exc:  # noqa: BLE001
            oos["ml"] = {"error": f"{type(exc).__name__}: {exc}"}
        if truth is not None:
            oos["rules_vs_ground_truth"] = classification_metrics(truth.reindex(test.index).to_numpy(float),
                                                                  test["rule_pred"].to_numpy(float))
        report["final_oos"] = oos
    else:
        report["final_oos"] = "SEALED (pass --reveal-oos once the configuration and code are final and committed)"
    return report
