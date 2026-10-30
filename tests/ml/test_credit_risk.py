"""Automated tests for the versioned credit-risk model and prediction API."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.credit_risk.experiment import load_processed_split
from ml.credit_risk.predict import (
    FeatureValidationError,
    ModelArtifactError,
    load_model_artifact,
    predict_one,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_RELATIVE_PATH = Path(
    "artifacts/credit_risk/balanced_logistic/1.0.0"
)
ARTIFACT_DIR = PROJECT_ROOT / ARTIFACT_RELATIVE_PATH


@pytest.fixture(scope="module")
def first_test_record() -> pd.Series:
    """Return the first existing test record without regenerating the split."""
    _, X_test, _, _ = load_processed_split(PROJECT_ROOT)
    return X_test.iloc[0]


@pytest.fixture()
def record_dict(first_test_record: pd.Series) -> dict[str, object]:
    return first_test_record.to_dict()


def _copy_artifact(
    project_root: Path,
    *,
    include_metadata: bool = True,
    include_pipeline: bool = True,
) -> Path:
    target = project_root / ARTIFACT_RELATIVE_PATH
    target.mkdir(parents=True)
    if include_metadata:
        shutil.copy2(ARTIFACT_DIR / "metadata.json", target / "metadata.json")
    if include_pipeline:
        shutil.copy2(ARTIFACT_DIR / "pipeline.joblib", target / "pipeline.joblib")
    return target


def test_loads_versioned_model_artifact() -> None:
    pipeline, metadata = load_model_artifact(PROJECT_ROOT)

    assert list(pipeline.named_steps) == ["preprocessor", "classifier"]
    assert pipeline.named_steps["classifier"].__class__.__name__ == (
        "LogisticRegression"
    )
    assert metadata["model_name"] == "credit-risk-balanced-logistic"
    assert metadata["model_version"] == "1.0.0"


def test_rejects_missing_metadata(tmp_path: Path) -> None:
    _copy_artifact(tmp_path, include_metadata=False)

    with pytest.raises(ModelArtifactError, match="metadata file does not exist"):
        load_model_artifact(tmp_path)


def test_rejects_missing_pipeline(tmp_path: Path) -> None:
    _copy_artifact(tmp_path, include_pipeline=False)

    with pytest.raises(ModelArtifactError, match="pipeline file does not exist"):
        load_model_artifact(tmp_path)


def test_rejects_pipeline_sha256_mismatch(tmp_path: Path) -> None:
    artifact_dir = _copy_artifact(tmp_path)
    with (artifact_dir / "pipeline.joblib").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ModelArtifactError, match="SHA-256 mismatch"):
        load_model_artifact(tmp_path)


def test_predicts_from_dict_input(record_dict: dict[str, object]) -> None:
    result = predict_one(record_dict, project_root=PROJECT_ROOT)

    assert {
        "model_name",
        "model_version",
        "default_probability",
        "threshold",
        "predicted_label",
        "predicted_label_text",
    }.issubset(result)
    assert 0.0 <= result["default_probability"] <= 1.0
    assert result["predicted_label"] in {0, 1}


def test_predicts_from_series_input(first_test_record: pd.Series) -> None:
    result = predict_one(first_test_record, project_root=PROJECT_ROOT)

    assert 0.0 <= result["default_probability"] <= 1.0
    assert result["predicted_label"] in {0, 1}


def test_dict_and_series_probabilities_match(
    record_dict: dict[str, object],
    first_test_record: pd.Series,
) -> None:
    dict_result = predict_one(record_dict, project_root=PROJECT_ROOT)
    series_result = predict_one(first_test_record, project_root=PROJECT_ROOT)

    assert dict_result["default_probability"] == pytest.approx(
        series_result["default_probability"],
        abs=0.0,
    )


def test_default_threshold_comes_from_metadata(
    record_dict: dict[str, object],
) -> None:
    with (ARTIFACT_DIR / "metadata.json").open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)

    result = predict_one(record_dict, project_root=PROJECT_ROOT)

    assert result["threshold"] == metadata["decision_threshold"]


def test_explicit_threshold_overrides_metadata(
    record_dict: dict[str, object],
) -> None:
    result = predict_one(
        record_dict,
        project_root=PROJECT_ROOT,
        threshold=0.9,
    )

    assert result["threshold"] == pytest.approx(0.9)


def test_rejects_missing_feature(record_dict: dict[str, object]) -> None:
    record_dict.pop("X1")

    with pytest.raises(FeatureValidationError, match="Missing required features: X1"):
        predict_one(record_dict, project_root=PROJECT_ROOT)


def test_rejects_unknown_feature(record_dict: dict[str, object]) -> None:
    record_dict["UNKNOWN_FEATURE"] = 1

    with pytest.raises(FeatureValidationError, match="Unknown features"):
        predict_one(record_dict, project_root=PROJECT_ROOT)


def test_rejects_nan(record_dict: dict[str, object]) -> None:
    record_dict["X5"] = np.nan

    with pytest.raises(FeatureValidationError, match="NaN or missing"):
        predict_one(record_dict, project_root=PROJECT_ROOT)


@pytest.mark.parametrize("value", [np.inf, -np.inf])
def test_rejects_infinite_values(
    record_dict: dict[str, object],
    value: float,
) -> None:
    record_dict["X5"] = value

    with pytest.raises(FeatureValidationError, match="must be finite"):
        predict_one(record_dict, project_root=PROJECT_ROOT)


def test_rejects_non_numeric_string(record_dict: dict[str, object]) -> None:
    record_dict["X1"] = "not-a-number"

    with pytest.raises(FeatureValidationError, match="not a valid numeric value"):
        predict_one(record_dict, project_root=PROJECT_ROOT)


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_rejects_threshold_outside_unit_interval(
    record_dict: dict[str, object],
    value: float,
) -> None:
    with pytest.raises(
        FeatureValidationError,
        match="threshold must be between 0.0 and 1.0",
    ):
        predict_one(
            record_dict,
            project_root=PROJECT_ROOT,
            threshold=value,
        )
