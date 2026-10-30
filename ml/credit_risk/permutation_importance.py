
import pandas as pd
import matplotlib.pyplot as plt

from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score

# 1. 读取训练数据
X_train = pd.read_csv("datasets/processed/X_train.csv")
y_train = pd.read_csv("datasets/processed/y_train.csv")["Y"]

# 2. 划分子训练集和验证集
X_subtrain, X_valid, y_subtrain, y_valid = train_test_split(
    X_train,
    y_train,
    test_size=0.2,
    random_state=42,
    stratify=y_train
)

print("子训练集规模：", X_subtrain.shape)
print("验证集规模：", X_valid.shape)

# 3. 定义字段
numeric_features = [
    "X1", "X5",
    "X12", "X13", "X14", "X15", "X16", "X17",
    "X18", "X19", "X20", "X21", "X22", "X23"
]

categorical_features = [
    "X2", "X3", "X4",
    "X6", "X7", "X8", "X9", "X10", "X11"
]

# 4. 数据预处理
preprocessor = ColumnTransformer(
    transformers=[
        ("num", StandardScaler(), numeric_features),
        (
            "cat",
            OneHotEncoder(handle_unknown="ignore"),
            categorical_features
        )
    ]
)

# 5. 建立随机森林模型
model = Pipeline(
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
                n_jobs=-1
            )
        )
    ]
)

# 6. 训练模型
print("\n开始训练模型...")
model.fit(X_subtrain, y_subtrain)
print("训练完成！")

# 7. 计算验证集 ROC-AUC
valid_proba = model.predict_proba(X_valid)[:, 1]
baseline_auc = roc_auc_score(y_valid, valid_proba)

print("\n验证集原始 ROC-AUC：", baseline_auc)

# 8. 计算置换重要性
print("\n开始计算 Permutation Importance...")

result = permutation_importance(
    model,
    X_valid,
    y_valid,
    scoring="roc_auc",
    n_repeats=10,
    random_state=42,
    n_jobs=-1
)

# 9. 整理结果
importance_df = pd.DataFrame({
    "feature": X_valid.columns,
    "importance_mean": result.importances_mean,
    "importance_std": result.importances_std
})

importance_df = importance_df.sort_values(
    by="importance_mean",
    ascending=False
)

print("\n========== Permutation Importance Top 15 ==========")
print(importance_df.head(15).to_string(index=False))

# 10. 绘图
top_features = (
    importance_df
    .head(15)
    .sort_values("importance_mean")
)

plt.figure(figsize=(10, 7))

plt.barh(
    top_features["feature"],
    top_features["importance_mean"],
    xerr=top_features["importance_std"]
)

plt.title("Permutation Importance (Validation Set)")
plt.xlabel("Decrease in ROC-AUC after permutation")
plt.ylabel("Feature")
plt.tight_layout()

plt.savefig(
    "ml/credit_risk/permutation_importance.png",
    dpi=300,
    bbox_inches="tight"
)

plt.show()

print("\n置换重要性图已保存！")
