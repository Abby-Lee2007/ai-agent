
import pandas as pd

from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    confusion_matrix
)

# 1. 读取原始训练集和测试集
X_train = pd.read_csv("datasets/processed/X_train.csv")
X_test = pd.read_csv("datasets/processed/X_test.csv")

y_train = pd.read_csv("datasets/processed/y_train.csv")["Y"]
y_test = pd.read_csv("datasets/processed/y_test.csv")["Y"]

# 2. 从训练集中划分验证集
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

# 4. 建立预处理流程
preprocessor = ColumnTransformer(
    transformers=[
        ("num", StandardScaler(), numeric_features),
        ("cat", OneHotEncoder(handle_unknown="ignore"), categorical_features)
    ]
)

# 5. 建立类别权重平衡模型
model = Pipeline(
    steps=[
        ("preprocessor", preprocessor),
        (
            "classifier",
            LogisticRegression(
                max_iter=1000,
                random_state=42,
                class_weight="balanced"
            )
        )
    ]
)

# 6. 只用子训练集训练
print("\n开始训练模型...")
model.fit(X_subtrain, y_subtrain)
print("训练完成！")

# 7. 获取验证集违约概率
valid_proba = model.predict_proba(X_valid)[:, 1]

# 8. 比较不同阈值
thresholds = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]

print("\n========== 验证集阈值比较 ==========")
print("Threshold | Precision | Recall | F1")

for threshold in thresholds:
    valid_pred = (valid_proba >= threshold).astype(int)

    precision = precision_score(
        y_valid, valid_pred, zero_division=0
    )
    recall = recall_score(y_valid, valid_pred)
    f1 = f1_score(y_valid, valid_pred)

    print(
        f"{threshold:9.2f} | "
        f"{precision:9.4f} | "
        f"{recall:6.4f} | "
        f"{f1:6.4f}"
    )

# 9. 暂时以验证集 F1 最高的阈值作为选择标准
best_threshold = max(
    thresholds,
    key=lambda t: f1_score(
        y_valid,
        (valid_proba >= t).astype(int)
    )
)

print("\n验证集选择的阈值：", best_threshold)

# 10. 使用全部原始训练集重新训练模型
model.fit(X_train, y_train)

# 11. 在测试集上进行最终评估
test_proba = model.predict_proba(X_test)[:, 1]
test_pred = (test_proba >= best_threshold).astype(int)

print("\n========== 最终测试集结果 ==========")
print("使用阈值：", best_threshold)
print("Precision:", precision_score(y_test, test_pred))
print("Recall:", recall_score(y_test, test_pred))
print("F1 Score:", f1_score(y_test, test_pred))
print("ROC-AUC:", roc_auc_score(y_test, test_proba))

print("\n混淆矩阵：")
print(confusion_matrix(y_test, test_pred))
