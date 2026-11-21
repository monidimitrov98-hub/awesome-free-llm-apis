"""Layer 2: statistical classification of the upcoming regime (scikit-learn).

Models: HistGradientBoostingClassifier (default; fast, handles NaN natively) or
RandomForestClassifier (robust baseline). Both give class probabilities over the forward
labels {+1, -1, 0}; the probability of the predicted class is then calibrated on a
chronologically LATER validation fold (see validation.py). XGBoost is not used: it would
add a heavy dependency without a demonstrated benefit on this feature set.

Artifacts (Sprint 4.1 hardening): the fitted model is pickled next to an identity JSON
(pickle SHA-256, feature list, label settings, hyperparameters, data and code hashes).

A SHA-256 alone does NOT protect against an attacker who can replace BOTH files: they can
write a malicious pickle and its matching hash, and ``pickle.loads`` would execute their
code. Therefore:

1. The identity is AUTHENTICATED with HMAC-SHA256 using a secret key from the environment
   variable ``FTS_MODEL_SIGNING_KEY`` (>= 32 characters, never stored in the repository).
   Saving needs the key; loading verifies the HMAC (constant time) and the pickle hash
   BEFORE any byte is unpickled. Without the key, models are not loaded at all.
2. Defence in depth: unpickling uses an allow-list (NumPy array/dtype/random reconstruction
   helpers and scikit-learn CLASSES only); any other global (os.system, builtins.eval,
   subprocess, ...) is refused even in a correctly signed file.

Limits: whoever holds the signing key (or can run code as this user) can still forge a model;
keep the key secret and separate from the artifacts. The default classifier ("rules") loads
no pickle at all, and the calibration file is plain JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
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


SIGNING_KEY_ENV = "FTS_MODEL_SIGNING_KEY"
_NUMPY_GLOBALS = {
    ("numpy", "dtype"), ("numpy", "ndarray"),
    ("numpy._core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "scalar"),
    ("numpy.core.multiarray", "_reconstruct"), ("numpy.core.multiarray", "scalar"),
    ("numpy.random._pickle", "__bit_generator_ctor"), ("numpy.random._pickle", "__generator_ctor"),
    ("numpy.random._pickle", "__randomstate_ctor"), ("numpy.random._pcg64", "PCG64"),
    ("numpy.random._mt19937", "MT19937"), ("numpy.random.bit_generator", "SeedSequence"),
    ("numpy.random.bit_generator", "__pyx_unpickle_SeedSequence"),
}


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        if (module, name) in _NUMPY_GLOBALS:
            return super().find_class(module, name)
        if module == "sklearn" or module.startswith("sklearn."):
            obj = super().find_class(module, name)
            if isinstance(obj, type) or name.startswith("__pyx_unpickle_"):
                return obj
        raise pickle.UnpicklingError(f"global {module}.{name} is not allowed in a model artifact")


def _signing_key() -> bytes:
    key = os.environ.get(SIGNING_KEY_ENV, "")
    if len(key) < 32:
        raise ConfigError(f"model artifacts need a secret signing key in the environment variable {SIGNING_KEY_ENV} "
                          "(at least 32 characters, kept outside the repository)")
    return key.encode("utf-8")


def _signature(ident: dict[str, Any], key: bytes) -> str:
    body = json.dumps({k: v for k, v in ident.items() if k != "hmac_sha256"}, sort_keys=True, default=str)
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def save_model(model, path: str | Path, identity: dict[str, Any]) -> dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = _signing_key()
    blob = pickle.dumps(model)
    _RestrictedUnpickler(io.BytesIO(blob)).load()  # refuse to write anything we would refuse to read
    ident = dict(identity, artifact_sha256=hashlib.sha256(blob).hexdigest(), features=list(ML_FEATURES))
    ident = json.loads(json.dumps(ident, default=str))
    ident["hmac_sha256"] = _signature(ident, key)
    p.write_bytes(blob)
    p.with_suffix(".json").write_text(json.dumps(ident, indent=2), encoding="utf-8")
    return ident


def load_model(path: str | Path):
    p = Path(path)
    try:
        ident = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        blob = p.read_bytes()
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read model artifact {p}: {exc}") from exc
    if not isinstance(ident, dict) or not isinstance(ident.get("hmac_sha256"), str):
        raise ConfigError(f"Model artifact {p} is not signed; refusing to load it")
    if not hmac.compare_digest(_signature(ident, _signing_key()), ident["hmac_sha256"]):
        raise ConfigError(f"Model artifact {p}: identity signature invalid (tampered or signed with another key)")
    if hashlib.sha256(blob).hexdigest() != ident.get("artifact_sha256"):
        raise ConfigError(f"Model artifact {p} does not match its identity hash; refusing to load it")
    if ident.get("features") != list(ML_FEATURES):
        raise ConfigError(f"Model artifact {p} was trained on a different feature list")
    try:
        return _RestrictedUnpickler(io.BytesIO(blob)).load(), ident
    except (pickle.UnpicklingError, AttributeError, ImportError, ValueError, TypeError, EOFError) as exc:
        raise ConfigError(f"Model artifact {p} refused: {exc}") from exc
