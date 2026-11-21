"""Sprint 4.1 hardening: model artifacts cannot execute code even if an attacker replaces
BOTH the pickle and its identity file (HMAC with a secret key + allow-list unpickler)."""

import hashlib
import json
import os
import pickle
from dataclasses import replace

import numpy as np
import pytest

from src.ai.market_regime import ml
from src.ai.market_regime.features import ML_FEATURES
from src.config import ConfigError
from tests.regime_helpers import RC

KEY = "a-long-secret-signing-key-for-tests-0123456789"


class Evil:
    """Pickles into a call of os.system -- the classic pickle code-execution gadget."""

    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (os.system, (f"touch {self.marker}",))


@pytest.fixture
def trained(tmp_path, monkeypatch):
    monkeypatch.setenv("FTS_MODEL_SIGNING_KEY", KEY)
    rng = np.random.default_rng(1)
    x = rng.normal(size=(300, len(ML_FEATURES)))
    y = rng.integers(-1, 2, 300).astype(float)
    m = ml.fit(replace(RC.ml, n_estimators=10, min_train_samples=100), x, y)
    path = tmp_path / "model.pkl"
    ident = ml.save_model(m, path, {"fingerprint": "f"})
    return path, ident, x


def test_signed_model_round_trip(trained):
    path, ident, x = trained
    model, loaded = ml.load_model(path)
    assert loaded["hmac_sha256"] == ident["hmac_sha256"] and model.predict_proba(x[:3]).shape == (3, 3)


def test_attacker_replacing_both_files_cannot_execute_code(trained, tmp_path):
    path, ident, _ = trained
    marker = tmp_path / "pwned"
    blob = pickle.dumps(Evil(marker))
    forged = dict(ident, artifact_sha256=hashlib.sha256(blob).hexdigest())  # attacker recomputes the hash...
    path.write_bytes(blob)
    path.with_suffix(".json").write_text(json.dumps(forged))
    with pytest.raises(ConfigError, match="signature"):  # ...but cannot recompute the HMAC without the key
        ml.load_model(path)
    assert not marker.exists()
    forged.pop("hmac_sha256")
    path.with_suffix(".json").write_text(json.dumps(forged))
    with pytest.raises(ConfigError, match="not signed"):
        ml.load_model(path)
    assert not marker.exists()


def test_allow_list_blocks_gadgets_even_in_a_correctly_signed_file(trained, tmp_path):
    path, ident, _ = trained
    marker = tmp_path / "pwned"
    blob = pickle.dumps(Evil(marker))
    forged = {k: v for k, v in ident.items() if k != "hmac_sha256"}
    forged["artifact_sha256"] = hashlib.sha256(blob).hexdigest()
    forged["hmac_sha256"] = ml._signature(forged, KEY.encode())  # insider with the key
    path.write_bytes(blob)
    path.with_suffix(".json").write_text(json.dumps(forged))
    with pytest.raises(ConfigError, match="not allowed"):
        ml.load_model(path)
    assert not marker.exists()
    with pytest.raises(pickle.UnpicklingError):
        ml._RestrictedUnpickler(__import__("io").BytesIO(pickle.dumps(eval))).load()


def test_wrong_or_missing_key(trained, monkeypatch):
    path, _, _ = trained
    monkeypatch.setenv("FTS_MODEL_SIGNING_KEY", "another-key-that-is-long-enough-0123456789")
    with pytest.raises(ConfigError, match="signature"):
        ml.load_model(path)
    monkeypatch.delenv("FTS_MODEL_SIGNING_KEY")
    with pytest.raises(ConfigError, match="FTS_MODEL_SIGNING_KEY"):
        ml.load_model(path)
    monkeypatch.setenv("FTS_MODEL_SIGNING_KEY", "short")
    with pytest.raises(ConfigError, match="32"):
        ml.save_model(object(), path, {})


def test_random_forest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FTS_MODEL_SIGNING_KEY", KEY)
    rng = np.random.default_rng(2)
    m = ml.fit(replace(RC.ml, model="random_forest", n_estimators=5, min_train_samples=100),
               rng.normal(size=(200, len(ML_FEATURES))), rng.integers(-1, 2, 200).astype(float))
    ml.save_model(m, tmp_path / "rf.pkl", {})
    assert ml.load_model(tmp_path / "rf.pkl")[0].n_estimators == 5


def test_save_model_requires_key_via_cli(tmp_path, monkeypatch):
    import run_regime_validation as rrv

    monkeypatch.delenv("FTS_MODEL_SIGNING_KEY", raising=False)
    assert rrv.main(["--synthetic-sessions", "26", "--oos-sessions", "3", "--output-dir", str(tmp_path / "o"),
                     "--ledger", str(tmp_path / "l.jsonl"), "--save-model", str(tmp_path / "m")]) == 1
