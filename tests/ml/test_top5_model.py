"""Verification tests for the new five-feature Random Forest artifact."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

import ml.credit_risk.predict as old_predict
from finagent.risk.credit_risk.model_feature_collection import (
    REQUIRED_FEATURE_ORDER,
    REQUIRED_MODEL_FEATURES,
)
from ml.credit_risk.top5_model import (
    ARTIFACT_PATH,
    DECISION_THRESHOLD,
    FEATURE_INPUT_ORDER,
    FEATURE_IMPORTANCE_ORDER,
    MODEL_NAME,
    MODEL_VERSION,
    Top5FeatureValidationError,
    artifact_available,
    load_top5_model_artifact,
    predict_top5,
)


def valid_features() -> dict[str, float]:
    return {
        "X1": 50_000,
        "X3": 2,
        "X6": -1, "X7": -1, "X8": -1, "X9": -1, "X10": -1, "X11": -1,
        "X18": 100, "X19": 200, "X20": 300, "X21": 400, "X22": 500, "X23": 600,
        "X12": 1000, "X13": 2000,
    }


def test_top5_artifact_contract_is_available_and_verified() -> None:
    assert artifact_available() is True
    pipeline, metadata = load_top5_model_artifact()

    assert hasattr(pipeline, "predict_proba")
    assert metadata["model_name"] == MODEL_NAME
    assert metadata["model_version"] == MODEL_VERSION
    assert tuple(metadata["features"]["columns"]) == FEATURE_INPUT_ORDER
    assert tuple(metadata["features"]["importance_order"]) == FEATURE_IMPORTANCE_ORDER
    assert tuple(REQUIRED_MODEL_FEATURES) == FEATURE_IMPORTANCE_ORDER
    assert set(REQUIRED_FEATURE_ORDER) == set(FEATURE_INPUT_ORDER)
    assert metadata["decision_threshold"] == DECISION_THRESHOLD
    assert metadata["artifact_origin"] == "reproduced_grouped_top5_variant"
    assert "X3" in metadata["features"]["groups"]
    assert metadata["fairness"]["certification"] is False
    assert (Path(".") / ARTIFACT_PATH).is_dir()


def test_top5_prediction_returns_real_probability_and_threshold() -> None:
    result = predict_top5(valid_features())

    assert result["prediction_status"] == "success"
    assert result["model_name"] == MODEL_NAME
    assert result["model_version"] == MODEL_VERSION
    assert 0.0 <= result["predicted_probability"] <= 1.0
    assert math.isfinite(result["predicted_probability"])
    assert result["threshold"] == DECISION_THRESHOLD
    assert result["feature_order"] == list(FEATURE_INPUT_ORDER)
    assert result["feature_completeness"] == 1.0


def test_top5_prediction_rejects_missing_features() -> None:
    features = valid_features()
    features.pop("X7")

    with pytest.raises(Top5FeatureValidationError, match="X7"):
        predict_top5(features)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -float("inf")])
def test_top5_prediction_rejects_non_finite_values(bad_value: float) -> None:
    features = valid_features()
    features["X1"] = bad_value

    with pytest.raises(Top5FeatureValidationError, match="finite"):
        predict_top5(features)


def test_top5_prediction_rejects_fractional_repayment_code() -> None:
    features = valid_features()
    features["X6"] = 1.5

    with pytest.raises(Top5FeatureValidationError, match="integer"):
        predict_top5(features)


def test_top5_model_does_not_use_old_predict_one(monkeypatch) -> None:
    def fail_old_model(*args, **kwargs):
        raise AssertionError("old predict_one path must not be used")

    monkeypatch.setattr(old_predict, "load_model_artifact", fail_old_model)
    monkeypatch.setattr(old_predict, "predict_one", fail_old_model)

    result = predict_top5(valid_features())
    assert result["prediction_status"] == "success"

