"""Model identity: any change to code, data, features, labels, thresholds or
hyperparameters changes the fingerprint (used by manifests and the validation ledger)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.ai.market_regime.config import RegimeConfig
from src.ai.market_regime.features import ML_FEATURES


def model_identity(rc: RegimeConfig, data_sha256: dict[str, str], source_sha256: str) -> dict[str, Any]:
    import sklearn

    body = {
        "model_version": rc.model_version, "classifier": rc.classifier, "ml_features": list(ML_FEATURES),
        "features": rc.to_dict()["features"], "classification": rc.to_dict()["classification"],
        "confidence": rc.to_dict()["confidence"], "labels": rc.to_dict()["labels"], "ml": rc.to_dict()["ml"],
        "validation": rc.to_dict()["validation"], "sklearn": sklearn.__version__, "source_sha256": source_sha256,
        "data_sha256": dict(sorted(data_sha256.items())),
    }
    fp = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return body | {"fingerprint": fp}


def instrument_key(root: str, synthetic: bool) -> str:
    """Ledger key of an instrument; synthetic data never shares history with real data."""
    return f"{root}:synthetic" if synthetic else root
