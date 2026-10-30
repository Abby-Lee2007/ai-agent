"""Verified inference adapter for the grouped Top-5 credit-risk model."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import joblib
import pandas as pd

from ml.credit_risk.artifact import sha256_file


MODEL_NAME = "credit-risk-grouped-top5-random-forest"
MODEL_VERSION = "5.0.0"
STATUS_FEATURES: Tuple[str, ...] = tuple(f"X{i}" for i in range(6, 12))
PAYMENT_FEATURES: Tuple[str, ...] = tuple(f"X{i}" for i in range(18, 24))
STATEMENT_FEATURES: Tuple[str, ...] = ("X12", "X13")
EXTRA_FEATURE = "X3"
FEATURE_INPUT_ORDER: Tuple[str, ...] = (
    *STATUS_FEATURES,
    *PAYMENT_FEATURES,
    *STATEMENT_FEATURES,
    "X1",
    EXTRA_FEATURE,
)
FEATURE_GROUPS: Dict[str, Tuple[str, ...]] = {
    "REPAYMENT_STATUS_6": STATUS_FEATURES,
    "REPAYMENT_AMOUNT_6": PAYMENT_FEATURES,
    "STATEMENT_AMOUNT_2": STATEMENT_FEATURES,
    "X1": ("X1",),
    EXTRA_FEATURE: (EXTRA_FEATURE,),
}
FEATURE_IMPORTANCE_ORDER: Tuple[str, ...] = tuple(FEATURE_GROUPS)
DECISION_THRESHOLD = 0.5
ARTIFACT_PATH = (
    Path("artifacts")
    / "credit_risk"
    / "top5_grouped_random_forest"
    / MODEL_VERSION
)


class Top5ModelArtifactError(RuntimeError):
    """Raised when the grouped Top-5 artifact cannot be trusted."""


class Top5FeatureValidationError(ValueError):
    """Raised when grouped Top-5 inputs are invalid."""


def _project_root(project_root: Optional[Path]) -> Path:
    if project_root is None:
        return Path(__file__).resolve().parents[2]
    return Path(project_root).resolve()


def artifact_available(project_root: Optional[Path] = None) -> bool:
    root = _project_root(project_root)
    artifact_dir = root / ARTIFACT_PATH
    return (artifact_dir / "metadata.json").is_file() and (
        artifact_dir / "pipeline.joblib"
    ).is_file()


def load_top5_model_artifact(
    project_root: Optional[Path] = None,
) -> tuple[Any, Dict[str, Any]]:
    root = _project_root(project_root)
    artifact_dir = root / ARTIFACT_PATH
    metadata_path = artifact_dir / "metadata.json"
    if not metadata_path.is_file():
        raise Top5ModelArtifactError(
            f"Grouped Top-5 metadata is missing: {metadata_path}"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Top5ModelArtifactError(
            f"Unable to read grouped Top-5 metadata: {exc}"
        ) from exc
    if not isinstance(metadata, dict):
        raise Top5ModelArtifactError("Grouped Top-5 metadata must be an object")
    if metadata.get("model_name") != MODEL_NAME:
        raise Top5ModelArtifactError("Grouped Top-5 model name mismatch")
    if metadata.get("model_version") != MODEL_VERSION:
        raise Top5ModelArtifactError("Grouped Top-5 model version mismatch")
    if float(metadata.get("decision_threshold", -1)) != DECISION_THRESHOLD:
        raise Top5ModelArtifactError("Grouped Top-5 threshold mismatch")

    features = metadata.get("features")
    if not isinstance(features, dict):
        raise Top5ModelArtifactError("Grouped Top-5 metadata is missing features")
    if tuple(features.get("columns") or ()) != FEATURE_INPUT_ORDER:
        raise Top5ModelArtifactError(
            "Grouped Top-5 column order does not match the inference contract"
        )
    if tuple(features.get("importance_order") or ()) != FEATURE_IMPORTANCE_ORDER:
        raise Top5ModelArtifactError("Grouped Top-5 slot order mismatch")
    if features.get("groups") != {
        key: list(value) for key, value in FEATURE_GROUPS.items()
    }:
        raise Top5ModelArtifactError("Grouped Top-5 feature groups mismatch")

    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict):
        raise Top5ModelArtifactError("Grouped Top-5 metadata is missing artifact")
    pipeline_name = artifact.get("pipeline_file")
    expected_sha256 = artifact.get("pipeline_sha256")
    if not isinstance(pipeline_name, str) or Path(pipeline_name).name != pipeline_name:
        raise Top5ModelArtifactError("Invalid grouped Top-5 pipeline filename")
    if not isinstance(expected_sha256, str) or not expected_sha256:
        raise Top5ModelArtifactError("Grouped Top-5 SHA-256 is missing")

    pipeline_path = artifact_dir / pipeline_name
    if not pipeline_path.is_file():
        raise Top5ModelArtifactError(
            f"Grouped Top-5 pipeline file is missing: {pipeline_path}"
        )
    if sha256_file(pipeline_path).lower() != expected_sha256.lower():
        raise Top5ModelArtifactError("Grouped Top-5 SHA-256 mismatch")
    try:
        pipeline = joblib.load(pipeline_path)
    except Exception as exc:
        raise Top5ModelArtifactError(
            f"Unable to load grouped Top-5 pipeline: {exc}"
        ) from exc
    if not hasattr(pipeline, "predict_proba"):
        raise Top5ModelArtifactError(
            "Grouped Top-5 pipeline does not provide predict_proba"
        )
    pipeline_features = getattr(pipeline, "feature_names_in_", None)
    if pipeline_features is not None and tuple(pipeline_features) != FEATURE_INPUT_ORDER:
        raise Top5ModelArtifactError(
            "Loaded pipeline columns do not match the grouped Top-5 contract"
        )
    return pipeline, metadata


def _normalize_features(features: Mapping[str, Any]) -> pd.DataFrame:
    if not isinstance(features, Mapping):
        raise Top5FeatureValidationError("features must be a mapping")
    missing = [key for key in FEATURE_INPUT_ORDER if key not in features]
    if missing:
        raise Top5FeatureValidationError(
            "Missing required grouped Top-5 features: " + ", ".join(missing)
        )
    normalized: Dict[str, float] = {}
    for key in FEATURE_INPUT_ORDER:
        value = features[key]
        if isinstance(value, bool):
            raise Top5FeatureValidationError(
                f"Feature {key} must be numeric, not boolean"
            )
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise Top5FeatureValidationError(
                f"Feature {key} is not numeric"
            ) from exc
        if not math.isfinite(number):
            raise Top5FeatureValidationError(f"Feature {key} must be finite")
        if key in STATUS_FEATURES and not number.is_integer():
            raise Top5FeatureValidationError(
                f"Feature {key} must be an integer category code"
            )
        normalized[key] = number
    return pd.DataFrame(
        [[normalized[key] for key in FEATURE_INPUT_ORDER]],
        columns=list(FEATURE_INPUT_ORDER),
    )


def predict_top5(
    features: Mapping[str, Any],
    *,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate and execute one grouped Top-5 prediction."""
    frame = _normalize_features(features)
    pipeline, metadata = load_top5_model_artifact(project_root)
    probabilities = pipeline.predict_proba(frame)
    classes = list(pipeline.classes_)
    if 0 not in classes or 1 not in classes:
        raise Top5ModelArtifactError("Grouped Top-5 model must contain classes 0 and 1")
    probability = float(probabilities[0, classes.index(1)])
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise Top5ModelArtifactError("Grouped Top-5 returned an invalid probability")
    predicted_label = int(probability >= DECISION_THRESHOLD)
    return {
        "prediction_status": "success",
        "status": "success",
        "model_name": metadata["model_name"],
        "model_version": metadata["model_version"],
        "model_type": metadata.get("model_type", ""),
        "model_sha256": metadata["artifact"]["pipeline_sha256"],
        "predicted_probability": probability,
        "probability": probability,
        "threshold": DECISION_THRESHOLD,
        "predicted_label": predicted_label,
        "risk_label": (
            "较高模型风险" if predicted_label == 1 else "较低模型风险"
        ),
        "risk_label_text": (
            "模型识别到较高的违约风险"
            if predicted_label == 1
            else "模型未识别到较高的违约风险"
        ),
        "feature_order": list(FEATURE_INPUT_ORDER),
        "feature_group_order": list(FEATURE_IMPORTANCE_ORDER),
        "feature_groups": {
            key: list(value) for key, value in FEATURE_GROUPS.items()
        },
        "feature_completeness": 1.0,
        "feature_quality": "五组核心资料均通过数值和范围校验",
        "missing_features": [],
        "feature_source_summary": {
            key: str(features.get(key, ""))
            for key in FEATURE_INPUT_ORDER
        },
        "model_limitations": list(metadata.get("limitations") or ()),
        "fairness_warning": (
            "这是基于 UCI 研究数据的 Top-5 分组模型；"
            "教育程度仅用于研究模型，不进入正式授信决策；"
            "性别、年龄和婚姻不在模型输入中，"
            "但不等同于完成公平性认证。"
        ),
        "explanation": (
            "模型使用五组信息：六个月还款状态、六个月还款金额、"
            "最近两个月账单金额、当前信用额度和教育程度。"
            "组内仍保留逐月明细；概率不直接决定审批或额度。"
        ),
    }


__all__ = [
    "ARTIFACT_PATH",
    "DECISION_THRESHOLD",
    "EXTRA_FEATURE",
    "FEATURE_GROUPS",
    "FEATURE_IMPORTANCE_ORDER",
    "FEATURE_INPUT_ORDER",
    "MODEL_NAME",
    "MODEL_VERSION",
    "PAYMENT_FEATURES",
    "STATEMENT_FEATURES",
    "STATUS_FEATURES",
    "Top5FeatureValidationError",
    "Top5ModelArtifactError",
    "artifact_available",
    "load_top5_model_artifact",
    "predict_top5",
]
