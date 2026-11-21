"""Layer 2: statistical classification of the upcoming regime (scikit-learn).

Models: HistGradientBoostingClassifier (default; fast, handles NaN natively) or
RandomForestClassifier (robust baseline). Both give class probabilities over the forward
labels {+1, -1, 0}; the probability of the predicted class is then calibrated on a
chronologically LATER validation fold (see validation.py). XGBoost is not used: it would
add a heavy dependency without a demonstrated benefit on this feature set.

Artifacts: the fitted model is stored with pickle ONLY next to an identity JSON holding
the pickle's SHA-256, the feature list, label settings, hyperparameters, training-data
hash and code hash. :func:`load_model` refuses a file whose hash or feature list differs.
Never load model files from untrusted sources (pickle can execute code).
"""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.ai.market_regime.config import MLConfig
from src.ai.market_regime.features import ML_FEATURES
from src.config import ConfigError

CLASS_ORDER = (-1.0, 0.0, 1.0)


def make_model(cfg: MLConfig):
    if cfg.model == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(n_estimators=cfg.n_estimators, max_depth=cfg.max_depth, min_samples_leaf=20,
                                      class_weight="balanced", random_state=cfg.random_state, n_jobs=1)
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(max_depth=cfg.max_depth, max_iter=cfg.n_estimators,
                                          learning_rate=cfg.learning_rate, class_weight="balanced",
                                          random_state=cfg.random_state)


def feature_matrix(frame: pd.DataFrame) -> np.ndarray:
    x = frame[list(ML_FEATURES)].astype(float).to_numpy()
    return np.where(np.isfinite(x), x, np.nan)


def fit(cfg: MLConfig, x: np.ndarray, y: np.ndarray):
    if len(y) < cfg.min_train_samples:
        raise ConfigError(f"only {len(y)} training samples (< ml.min_train_samples {cfg.min_train_samples})")
    if len(set(y.tolist())) < 2:
        raise ConfigError("training labels contain a single class")
    model = make_model(cfg)
    if cfg.model == "random_forest":  # RF cannot take NaN
        x = np.nan_to_num(x, nan=0.0)
    model.fit(x, y)
    return model


def predict(model, cfg: MLConfig, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (predicted class, probability of that class) with validated outputs."""
    if cfg.model == "random_forest":
        x = np.nan_to_num(x, nan=0.0)
    proba = model.predict_proba(x)
    classes = [float(c) for c in model.classes_]
    if proba.ndim != 2 or proba.shape[1] != len(classes) or not np.all(np.isfinite(proba)):
        raise ValueError("invalid model output")
    idx = proba.argmax(axis=1)
    return np.array([classes[i] for i in idx]), proba[np.arange(len(idx)), idx]


def save_model(model, path: str | Path, identity: dict[str, Any]) -> dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = pickle.dumps(model)
    p.write_bytes(blob)
    ident = dict(identity, artifact_sha256=hashlib.sha256(blob).hexdigest(), features=list(ML_FEATURES))
    p.with_suffix(".json").write_text(json.dumps(ident, indent=2, default=str), encoding="utf-8")
    return ident


def load_model(path: str | Path):
    p = Path(path)
    try:
        ident = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        blob = p.read_bytes()
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read model artifact {p}: {exc}") from exc
    if hashlib.sha256(blob).hexdigest() != ident.get("artifact_sha256"):
        raise ConfigError(f"Model artifact {p} does not match its identity hash; refusing to load it")
    if ident.get("features") != list(ML_FEATURES):
        raise ConfigError(f"Model artifact {p} was trained on a different feature list")
    return pickle.loads(blob), ident  # noqa: S301 - integrity-checked local artifact
