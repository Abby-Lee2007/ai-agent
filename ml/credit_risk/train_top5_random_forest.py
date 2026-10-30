"""Train and save the five-feature Random Forest application model.

The feature subset is derived reproducibly from grouped MDI importance of the
historical 23-feature Random Forest.  This artifact is a new reduced variant,
not the missing original Random Forest artifact.
"""

from __future__ import annotations

import argparse
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ml.credit_risk.artifact import sha256_file
from ml.credit_risk.experiment import (
    CATEGORICAL_FEATURES,
    FEATURE_ORDER,
    NUMERIC_FEATURES,
    build_random_forest_pipeline,
    load_processed_split,
)


MODEL_NAME = "credit-risk-top5-random-forest"
MODEL_VERSION = "1.0.0"
MODEL_TYPE = "Random Forest (top-five reduced variant)"
DECISION_THRESHOLD = 0.5
TOP5_INPUT_ORDER = ("X1", "X6", "X7", "X8", "X9")
TOP5_IMPORTANCE_ORDER = ("X6", "X7", "X8", "X9", "X1")
SENSITIVE_EXCLUDED = ("X2", "X3", "X4", "X5")
DEFAULT_OUTPUT = (
    Path("artifacts")
    / "credit_risk"
    / "top5_random_forest"
    / MODEL_VERSION
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _grouped_importance(pipeline: Pipeline) -> Dict[str, float]:
    preprocessor = pipeline.named_steps["preprocessor"]
    names = list(preprocessor.get_feature_names_out())
    values = pipeline.named_steps["classifier"].feature_importances_
    grouped: Dict[str, float] = {}
    for name, value in zip(names, values):
        if name.startswith("num__"):
            original = name[5:]
        else:
            original = name[5:].split("_")[0]
        grouped[original] = grouped.get(original, 0.0) + float(value)
    return grouped


def _metrics(
    y_true: pd.Series,
    probabilities: np.ndarray,
    threshold: float,
) -> Dict[str, Any]:
    predicted = (probabilities >= threshold).astype(int)
    return {
        "threshold": threshold,
        "n_samples": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, predicted)), 6),
        "precision": round(float(precision_score(y_true, predicted)), 6),
        "recall": round(float(recall_score(y_true, predicted)), 6),
        "f1": round(float(f1_score(y_true, predicted)), 6),
        "roc_auc": round(float(roc_auc_score(y_true, probabilities)), 6),
    }


def build_top5_pipeline() -> Pipeline:
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), ["X1"]),
            ("cat", OneHotEncoder(handle_unknown="ignore"), ["X6", "X7", "X8", "X9"]),
        ]
    )
    return Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            (
                "classifier",
                RandomForestClassifier(
                    n_estimators=300,
                    max_depth=12,
                    min_samples_split=10,
                    min_samples_leaf=5,
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def train_and_save(
    project_root: Path,
    output_dir: Path,
) -> Path:
    root = Path(project_root).resolve()
    output = Path(output_dir)
    if not output.is_absolute():
        output = root / output
    if output.exists():
        raise FileExistsError(f"Artifact directory already exists: {output}")

    X_train, X_test, y_train, y_test = load_processed_split(root)

    full_pipeline = build_random_forest_pipeline(42)
    full_pipeline.fit(X_train, y_train)
    importance = _grouped_importance(full_pipeline)
    ranked = sorted(importance.items(), key=lambda item: item[1], reverse=True)
    selected = tuple(feature for feature, _ in ranked[:5])
    if selected != TOP5_IMPORTANCE_ORDER:
        raise RuntimeError(
            "Recomputed grouped MDI no longer matches the recorded Top 5: "
            f"{selected!r}"
        )

    pipeline = build_top5_pipeline()
    pipeline.fit(X_train[list(TOP5_INPUT_ORDER)], y_train)
    probabilities = pipeline.predict_proba(X_test[list(TOP5_INPUT_ORDER)])[:, 1]
    full_probabilities = full_pipeline.predict_proba(X_test)[:, 1]

    output.mkdir(parents=True)
    pipeline_path = output / "pipeline.joblib"
    joblib.dump(pipeline, pipeline_path, compress=3)

    metadata = {
        "schema_version": "1.0",
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "model_type": MODEL_TYPE,
        "created_at_utc": _utc_now(),
        "decision_threshold": DECISION_THRESHOLD,
        "artifact_origin": "reproduced_top5_variant",
        "description": (
            "New five-feature Random Forest trained from the existing processed "
            "UCI split. It is not the missing original 23-feature artifact."
        ),
        "features": {
            "all": list(TOP5_INPUT_ORDER),
            "numeric": ["X1"],
            "categorical": ["X6", "X7", "X8", "X9"],
            "importance_order": list(TOP5_IMPORTANCE_ORDER),
            "optional_supplementary": [
                feature for feature in FEATURE_ORDER
                if feature not in TOP5_INPUT_ORDER
            ],
            "sensitive_excluded": list(SENSITIVE_EXCLUDED),
        },
        "feature_selection": {
            "method": "grouped MDI importance from full 23-feature Random Forest",
            "source_script": "ml/credit_risk/feature_importance.py",
            "recomputed_on": "datasets/processed/X_train.csv",
            "grouped_importance": {
                feature: round(value, 10)
                for feature, value in ranked
            },
        },
        "preprocessing": {
            "numeric": ["X1"],
            "numeric_scaler": "StandardScaler",
            "categorical": ["X6", "X7", "X8", "X9"],
            "categorical_encoder": "OneHotEncoder(handle_unknown='ignore')",
        },
        "classifier_parameters": {
            "n_estimators": 300,
            "max_depth": 12,
            "min_samples_split": 10,
            "min_samples_leaf": 5,
            "class_weight": "balanced",
            "random_state": 42,
            "n_jobs": -1,
        },
        "training_data": {
            "dataset": "UCI Default of Credit Card Clients",
            "rows": int(len(X_train)),
            "features": len(TOP5_INPUT_ORDER),
            "target": "Y",
            "target_distribution": {
                str(label): int(count)
                for label, count in y_train.value_counts().sort_index().items()
            },
        },
        "evaluation_data": {
            "rows": int(len(X_test)),
            "usage": "evaluation_only; never passed to pipeline.fit",
            "target_distribution": {
                str(label): int(count)
                for label, count in y_test.value_counts().sort_index().items()
            },
        },
        "evaluation": _metrics(y_test, probabilities, DECISION_THRESHOLD),
        "historical_full_rf_reference": {
            "features": list(FEATURE_ORDER),
            "numeric_features": list(NUMERIC_FEATURES),
            "categorical_features": list(CATEGORICAL_FEATURES),
            "evaluation": _metrics(y_test, full_probabilities, 0.5),
            "note": "Historical metrics retained for comparison; not deployable as an artifact by this script.",
        },
        "fairness": {
            "sensitive_features_excluded": list(SENSITIVE_EXCLUDED),
            "certification": False,
            "warning": (
                "Excluding sensitive fields does not prove fairness. "
                "This is an offline UCI research result."
            ),
        },
        "limitations": [
            "Trained on UCI research data, not this bank's real customer population.",
            "Probability is not guaranteed calibrated.",
            "Model output is not a formal credit approval decision.",
            "Top-five reduction uses training-set grouped MDI and requires external validation.",
        ],
        "dependencies": {
            "python": platform.python_version(),
            "scikit-learn": sklearn.__version__,
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "joblib": joblib.__version__,
        },
        "artifact": {
            "pipeline_file": pipeline_path.name,
            "pipeline_sha256": sha256_file(pipeline_path),
        },
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "feature_importance.json").write_text(
        json.dumps(
            {
                "method": "grouped_mdi",
                "ranked_features": [
                    {"rank": index, "feature": feature, "importance": round(value, 10)}
                    for index, (feature, value) in enumerate(ranked, start=1)
                ],
                "selected_top5": list(TOP5_IMPORTANCE_ORDER),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Train the top-five credit-risk model")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    output = train_and_save(Path(args.project_root), Path(args.output_dir))
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    print(f"Artifact: {output}")
    print(f"Model: {metadata['model_name']} {metadata['model_version']}")
    print(f"Features: {metadata['features']['importance_order']}")
    print(f"SHA-256: {metadata['artifact']['pipeline_sha256']}")
    print(f"Evaluation: {metadata['evaluation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
