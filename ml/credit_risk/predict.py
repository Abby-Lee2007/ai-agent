"""Single-record inference for the saved credit-risk model artifact.

Importing this module does not load a model or run a prediction.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional, Union

import joblib
import numpy as np
import pandas as pd

from ml.credit_risk.artifact import DEFAULT_ARTIFACT_PATH, sha256_file
from ml.credit_risk.experiment import FEATURE_ORDER


class ModelArtifactError(RuntimeError):
    """Raised when a model artifact is missing, invalid, or cannot be loaded."""


class FeatureValidationError(ValueError):
    """Raised when a prediction record has invalid fields or values."""


CustomerFeatures = Union[Dict[str, Any], pd.Series]


def _project_root(project_root: Optional[Path]) -> Path:
    if project_root is None:
        return Path(__file__).resolve().parents[2]
    return Path(project_root).resolve()


def _load_metadata(metadata_path: Path) -> Dict[str, Any]:
    if not metadata_path.is_file():
        raise ModelArtifactError(
            f"Model metadata file does not exist: {metadata_path}"
        )
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelArtifactError(
            f"Unable to read model metadata: {metadata_path}: {exc}"
        ) from exc
    if not isinstance(metadata, dict):
        raise ModelArtifactError("Model metadata must be a JSON object")
    return metadata


def load_model_artifact(
    project_root: Optional[Path] = None,
) -> tuple[Any, Dict[str, Any]]:
    """Load and verify the default model artifact.

    Metadata is read first, then the pipeline file hash is verified before
    ``joblib.load`` is called.

    Returns:
        A ``(pipeline, metadata)`` tuple.

    Raises:
        ModelArtifactError: If metadata or the model is missing, inconsistent,
            corrupt, or cannot be loaded.
    """
    root = _project_root(project_root)
    artifact_dir = root / DEFAULT_ARTIFACT_PATH
    metadata_path = artifact_dir / "metadata.json"
    metadata = _load_metadata(metadata_path)

    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict):
        raise ModelArtifactError(
            "Model metadata is missing the 'artifact' object"
        )
    pipeline_name = artifact.get("pipeline_file")
    expected_sha256 = artifact.get("pipeline_sha256")
    if not isinstance(pipeline_name, str) or not pipeline_name.strip():
        raise ModelArtifactError(
            "Model metadata is missing artifact.pipeline_file"
        )
    if Path(pipeline_name).name != pipeline_name:
        raise ModelArtifactError(
            "artifact.pipeline_file must be a filename without a path"
        )
    if not isinstance(expected_sha256, str) or not expected_sha256.strip():
        raise ModelArtifactError(
            "Model metadata is missing artifact.pipeline_sha256"
        )

    pipeline_path = artifact_dir / pipeline_name
    if not pipeline_path.is_file():
        raise ModelArtifactError(
            f"Model pipeline file does not exist: {pipeline_path}"
        )

    actual_sha256 = sha256_file(pipeline_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise ModelArtifactError(
            "Model pipeline SHA-256 mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    try:
        pipeline = joblib.load(pipeline_path)
    except Exception as exc:
        raise ModelArtifactError(
            f"Unable to load model pipeline: {pipeline_path}: {exc}"
        ) from exc

    if not hasattr(pipeline, "predict_proba"):
        raise ModelArtifactError(
            "Loaded model does not provide predict_proba"
        )
    return pipeline, metadata


def _normalize_features(customer_features: CustomerFeatures) -> pd.DataFrame:
    if isinstance(customer_features, pd.Series):
        if not customer_features.index.is_unique:
            raise FeatureValidationError(
                "customer_features contains duplicate feature names"
            )
        values = customer_features.to_dict()
    elif isinstance(customer_features, dict):
        values = dict(customer_features)
    else:
        raise FeatureValidationError(
            "customer_features must be a dict or pandas Series"
        )

    expected = set(FEATURE_ORDER)
    supplied = set(values)
    missing = [feature for feature in FEATURE_ORDER if feature not in supplied]
    unknown = sorted(supplied - expected)
    if missing:
        raise FeatureValidationError(
            "Missing required features: " + ", ".join(missing)
        )
    if unknown:
        raise FeatureValidationError(
            "Unknown features are not allowed: " + ", ".join(unknown)
        )

    normalized: Dict[str, float] = {}
    for feature in FEATURE_ORDER:
        value = values[feature]
        if isinstance(value, (bool, np.bool_)):
            raise FeatureValidationError(
                f"Feature {feature} must be numeric, not boolean"
            )
        try:
            converted = pd.to_numeric(
                pd.Series([value]),
                errors="raise",
            ).iloc[0]
        except (TypeError, ValueError) as exc:
            raise FeatureValidationError(
                f"Feature {feature} is not a valid numeric value: {value!r}"
            ) from exc

        if pd.isna(converted):
            raise FeatureValidationError(
                f"Feature {feature} must not be NaN or missing"
            )
        if isinstance(converted, (complex, np.complexfloating)):
            raise FeatureValidationError(
                f"Feature {feature} must be a real number"
            )
        try:
            numeric_value = float(converted)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FeatureValidationError(
                f"Feature {feature} is not a valid numeric value: {value!r}"
            ) from exc
        if not math.isfinite(numeric_value):
            raise FeatureValidationError(
                f"Feature {feature} must be finite; NaN and infinity are not allowed"
            )
        normalized[feature] = numeric_value

    return pd.DataFrame(
        [[normalized[feature] for feature in FEATURE_ORDER]],
        columns=list(FEATURE_ORDER),
    )


def _resolve_threshold(
    metadata: Dict[str, Any],
    threshold: Optional[float],
) -> float:
    value: Any
    if threshold is None:
        value = metadata.get("decision_threshold")
        if value is None:
            raise ModelArtifactError(
                "Model metadata does not define decision_threshold"
            )
    else:
        value = threshold

    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value,
        (int, float, np.integer, np.floating),
    ):
        raise FeatureValidationError("threshold must be a numeric value")
    resolved = float(value)
    if not math.isfinite(resolved) or not 0.0 <= resolved <= 1.0:
        raise FeatureValidationError("threshold must be between 0.0 and 1.0")
    return resolved


def predict_one(
    customer_features: CustomerFeatures,
    project_root: Optional[Path] = None,
    threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """Predict one customer's binary credit-risk class.

    This result is a model output for demonstration and evaluation only. It is
    not a loan decision and is not an estimate of a real-world default
    probability.

    Args:
        customer_features: Dict or pandas Series containing all 23 model
            features. Missing, unknown, non-finite, or non-numeric values are
            rejected.
        project_root: Optional project root containing the default artifact.
        threshold: Optional probability threshold. When omitted, the threshold
            stored in metadata is used.

    Returns:
        A JSON-serializable result containing model identity, probability,
        threshold, numeric prediction, and display text.

    Raises:
        ModelArtifactError: If the model artifact cannot be trusted or loaded.
        FeatureValidationError: If the record or threshold is invalid.
    """
    frame = _normalize_features(customer_features)
    pipeline, metadata = load_model_artifact(project_root)
    resolved_threshold = _resolve_threshold(metadata, threshold)

    model_name = metadata.get("model_name")
    model_version = metadata.get("model_version")
    if not isinstance(model_name, str) or not model_name:
        raise ModelArtifactError("Model metadata is missing model_name")
    if not isinstance(model_version, str) or not model_version:
        raise ModelArtifactError("Model metadata is missing model_version")

    classes = list(pipeline.classes_)
    if 0 not in classes or 1 not in classes:
        raise ModelArtifactError(
            "Loaded model must contain binary classes 0 and 1"
        )
    positive_index = classes.index(1)
    probabilities = pipeline.predict_proba(frame)
    if probabilities.shape[0] != 1 or probabilities.shape[1] <= positive_index:
        raise ModelArtifactError("Loaded model returned an invalid probability shape")

    default_probability = float(probabilities[0, positive_index])
    if not math.isfinite(default_probability) or not 0.0 <= default_probability <= 1.0:
        raise ModelArtifactError(
            "Loaded model returned a non-finite or out-of-range probability"
        )

    predicted_label = int(default_probability >= resolved_threshold)
    predicted_label_text = (
        "模型分类为违约（仅用于模型评估，不是贷款审批结论）"
        if predicted_label == 1
        else "模型分类为未违约（仅用于模型评估，不是贷款审批结论）"
    )
    return {
        "model_name": model_name,
        "model_version": model_version,
        "default_probability": default_probability,
        "threshold": resolved_threshold,
        "predicted_label": predicted_label,
        "predicted_label_text": predicted_label_text,
    }


__all__ = [
    "CustomerFeatures",
    "FeatureValidationError",
    "ModelArtifactError",
    "load_model_artifact",
    "predict_one",
]
