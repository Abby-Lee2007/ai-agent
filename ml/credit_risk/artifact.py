"""Create the first versioned Balanced Logistic Regression artifact.

The module exposes functions and a CLI entry point. Importing it does not load
data, train a model, or write files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn

from ml.credit_risk.experiment import (
    CATEGORICAL_FEATURES,
    FEATURE_ORDER,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    build_balanced_logistic_pipeline,
    evaluate_binary_model,
    load_processed_split,
)


MODEL_NAME = "credit-risk-balanced-logistic"
MODEL_VERSION = "1.0.0"
MODEL_TYPE = "Balanced Logistic Regression"
DECISION_THRESHOLD = 0.5
DEFAULT_ARTIFACT_PATH = (
    Path("artifacts")
    / "credit_risk"
    / "balanced_logistic"
    / MODEL_VERSION
)


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target_distribution(target: pd.Series) -> Dict[str, int]:
    counts = target.value_counts().sort_index()
    return {
        str(label): int(count)
        for label, count in counts.items()
    }


def _processed_file_hashes(project_root: Path) -> Dict[str, str]:
    processed_dir = project_root / "datasets" / "processed"
    filenames = (
        "X_train.csv",
        "y_train.csv",
        "X_test.csv",
        "y_test.csv",
    )
    return {
        filename: sha256_file(processed_dir / filename)
        for filename in filenames
    }


def _classifier_parameters(pipeline: Any) -> Dict[str, Any]:
    return dict(pipeline.named_steps["classifier"].get_params())


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def save_balanced_logistic_artifact(
    project_root: Path,
    output_dir: Path,
) -> Path:
    """Train and save a versioned Balanced Logistic Regression artifact.

    The model is fitted only with ``X_train`` and ``y_train``. The test split is
    evaluated after fitting and is never passed to ``pipeline.fit``.

    Args:
        project_root: Root directory of the ``ai_banking_agent`` project.
        output_dir: New artifact directory. Existing directories are rejected.

    Returns:
        The created artifact directory.

    Raises:
        FileExistsError: If ``output_dir`` already exists.
        ValueError: If the fitted classes or target values are invalid.
    """
    root = Path(project_root)
    target_dir = Path(output_dir)
    if target_dir.exists():
        raise FileExistsError(
            f"Artifact directory already exists; refusing to overwrite: {target_dir}"
        )

    X_train, X_test, y_train, y_test = load_processed_split(root)
    if not set(y_train.unique()).issubset({0, 1}):
        raise ValueError("Training target must contain only binary values 0 and 1")
    if not set(y_test.unique()).issubset({0, 1}):
        raise ValueError("Test target must contain only binary values 0 and 1")

    pipeline = build_balanced_logistic_pipeline(random_state=42)
    started_at = _utc_now()
    start_counter = time.perf_counter()
    pipeline.fit(X_train, y_train)
    training_seconds = time.perf_counter() - start_counter
    completed_at = _utc_now()

    evaluation = evaluate_binary_model(
        pipeline,
        X_test,
        y_test,
        threshold=DECISION_THRESHOLD,
    )

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(exist_ok=False)
    model_path = target_dir / "pipeline.joblib"
    metadata_path = target_dir / "metadata.json"

    try:
        joblib.dump(pipeline, model_path)
        model_sha256 = sha256_file(model_path)
        metadata = {
            "schema_version": "1.0",
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "model_type": MODEL_TYPE,
            "created_at_utc": _utc_now(),
            "training_time": {
                "started_at_utc": started_at,
                "completed_at_utc": completed_at,
                "duration_seconds": round(training_seconds, 6),
            },
            "model_parameters": _classifier_parameters(pipeline),
            "decision_threshold": DECISION_THRESHOLD,
            "features": {
                "all": list(FEATURE_ORDER),
                "numeric": list(NUMERIC_FEATURES),
                "categorical": list(CATEGORICAL_FEATURES),
            },
            "training_data": {
                "dataset": "UCI Default of Credit Card Clients",
                "dataset_id": 350,
                "source_files": [
                    "datasets/processed/X_train.csv",
                    "datasets/processed/y_train.csv",
                ],
                "rows": int(len(X_train)),
                "features": int(X_train.shape[1]),
                "target": TARGET_COLUMN,
                "target_distribution": _target_distribution(y_train),
            },
            "evaluation_data": {
                "source_files": [
                    "datasets/processed/X_test.csv",
                    "datasets/processed/y_test.csv",
                ],
                "usage": "evaluation_only; never passed to pipeline.fit",
                "rows": int(len(X_test)),
                "target_distribution": _target_distribution(y_test),
            },
            "data_file_sha256": _processed_file_hashes(root),
            "evaluation": evaluation,
            "artifact": {
                "pipeline_file": "pipeline.joblib",
                "pipeline_sha256": model_sha256,
            },
            "dependencies": {
                "python": platform.python_version(),
                "scikit-learn": sklearn.__version__,
                "pandas": pd.__version__,
                "numpy": np.__version__,
                "joblib": joblib.__version__,
            },
        }
        _write_json(metadata_path, metadata)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    return target_dir


def verify_artifact_sha256(artifact_dir: Path) -> bool:
    """Verify that metadata records the actual pipeline file digest."""
    root = Path(artifact_dir)
    metadata_path = root / "metadata.json"
    with metadata_path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    expected = str(metadata["artifact"]["pipeline_sha256"])
    actual = sha256_file(root / metadata["artifact"]["pipeline_file"])
    return actual == expected


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line options for artifact creation."""
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Train and save the Balanced Logistic Regression artifact."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=project_root,
        help="Project root containing datasets/processed.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / DEFAULT_ARTIFACT_PATH,
        help="New artifact directory. Existing directories are rejected.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Create and verify the first Balanced Logistic Regression artifact."""
    args = parse_args(argv)
    artifact_dir = save_balanced_logistic_artifact(
        args.project_root,
        args.output_dir,
    )
    with (artifact_dir / "metadata.json").open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    hash_ok = verify_artifact_sha256(artifact_dir)

    print(f"Artifact: {artifact_dir}")
    print(f"Pipeline SHA-256: {metadata['artifact']['pipeline_sha256']}")
    print(f"SHA-256 verification: {'PASS' if hash_ok else 'FAIL'}")
    print("Evaluation metrics:")
    for key in ("accuracy", "precision", "recall", "f1", "roc_auc"):
        print(f"  {key}: {metadata['evaluation'][key]:.4f}")
    print(f"  threshold: {metadata['decision_threshold']}")
    return 0 if hash_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
