"""Reusable components for credit-risk model experiments.

This module defines feature groups, sklearn pipelines, dataset loading, and
evaluation helpers. Importing it does not read datasets or train a model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


FEATURE_ORDER: Tuple[str, ...] = tuple(
    f"X{index}" for index in range(1, 24)
)

NUMERIC_FEATURES: Tuple[str, ...] = (
    "X1",
    "X5",
    "X12",
    "X13",
    "X14",
    "X15",
    "X16",
    "X17",
    "X18",
    "X19",
    "X20",
    "X21",
    "X22",
    "X23",
)

CATEGORICAL_FEATURES: Tuple[str, ...] = (
    "X2",
    "X3",
    "X4",
    "X6",
    "X7",
    "X8",
    "X9",
    "X10",
    "X11",
)

TARGET_COLUMN = "Y"


def load_processed_split(
    project_root: Path,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Load the existing processed train/test split without resplitting it.

    Args:
        project_root: Root directory of the ``ai_banking_agent`` project.

    Returns:
        ``X_train``, ``X_test``, ``y_train``, and ``y_test`` in that order.

    Raises:
        FileNotFoundError: If one of the processed CSV files is missing.
        ValueError: If feature columns, target column, or row counts are invalid.
    """
    root = Path(project_root)
    processed_dir = root / "datasets" / "processed"
    paths = {
        "X_train": processed_dir / "X_train.csv",
        "X_test": processed_dir / "X_test.csv",
        "y_train": processed_dir / "y_train.csv",
        "y_test": processed_dir / "y_test.csv",
    }

    missing_files = [
        str(path) for path in paths.values() if not path.is_file()
    ]
    if missing_files:
        raise FileNotFoundError(
            "Missing processed dataset files: " + ", ".join(missing_files)
        )

    X_train = pd.read_csv(paths["X_train"])
    X_test = pd.read_csv(paths["X_test"])
    y_train_frame = pd.read_csv(paths["y_train"])
    y_test_frame = pd.read_csv(paths["y_test"])

    expected_features = list(FEATURE_ORDER)
    for name, frame in (("X_train", X_train), ("X_test", X_test)):
        missing_columns = [
            column for column in expected_features if column not in frame.columns
        ]
        if missing_columns:
            raise ValueError(
                f"{name} is missing required columns: {', '.join(missing_columns)}"
            )

    for name, frame in (
        ("y_train", y_train_frame),
        ("y_test", y_test_frame),
    ):
        if TARGET_COLUMN not in frame.columns:
            raise ValueError(
                f"{name} must contain the target column {TARGET_COLUMN!r}"
            )

    if len(X_train) != len(y_train_frame) or len(X_test) != len(y_test_frame):
        raise ValueError("Feature and target row counts must match")

    y_train = y_train_frame[TARGET_COLUMN]
    y_test = y_test_frame[TARGET_COLUMN]
    return X_train, X_test, y_train, y_test


def build_preprocessor() -> ColumnTransformer:
    """Build the shared preprocessing pipeline used by all experiments."""
    return ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), list(NUMERIC_FEATURES)),
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore"),
                list(CATEGORICAL_FEATURES),
            ),
        ]
    )


def build_balanced_logistic_pipeline(
    random_state: int = 42,
) -> Pipeline:
    """Build a complete balanced logistic-regression pipeline."""
    return Pipeline(
        steps=[
            ("preprocessor", build_preprocessor()),
            (
                "classifier",
                LogisticRegression(
                    max_iter=1000,
                    random_state=random_state,
                    class_weight="balanced",
                ),
            ),
        ]
    )


def build_random_forest_pipeline(
    random_state: int = 42,
) -> Pipeline:
    """Build a complete random-forest pipeline with the original settings."""
    return Pipeline(
        steps=[
            ("preprocessor", build_preprocessor()),
            (
                "classifier",
                RandomForestClassifier(
                    n_estimators=300,
                    max_depth=12,
                    min_samples_split=10,
                    min_samples_leaf=5,
                    class_weight="balanced",
                    random_state=random_state,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def evaluate_binary_model(
    pipeline: Pipeline,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    threshold: float = 0.5,
) -> Dict[str, Any]:
    """Evaluate a fitted binary pipeline at an explicit probability threshold.

    Args:
        pipeline: Fitted sklearn pipeline exposing ``predict_proba``.
        X_test: Test features.
        y_test: Binary target values.
        threshold: Positive-class probability cutoff.

    Returns:
        A JSON-serializable mapping containing standard metrics and the
        confusion matrix.

    Raises:
        ValueError: If the threshold, class labels, or row counts are invalid.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")
    if len(X_test) != len(y_test):
        raise ValueError("X_test and y_test must have the same number of rows")
    if len(X_test) == 0:
        raise ValueError("X_test and y_test must not be empty")

    classes = list(pipeline.classes_)
    if 0 not in classes or 1 not in classes:
        raise ValueError("pipeline must be fitted on binary classes 0 and 1")

    positive_index = classes.index(1)
    probabilities = pipeline.predict_proba(X_test)[:, positive_index]
    predictions = (probabilities >= threshold).astype(int)
    target = y_test.to_numpy()
    matrix = confusion_matrix(target, predictions, labels=[0, 1])

    return {
        "threshold": float(threshold),
        "n_samples": int(len(target)),
        "accuracy": float(accuracy_score(target, predictions)),
        "precision": float(
            precision_score(target, predictions, zero_division=0)
        ),
        "recall": float(recall_score(target, predictions, zero_division=0)),
        "f1": float(f1_score(target, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(target, probabilities)),
        "confusion_matrix": matrix.tolist(),
    }
