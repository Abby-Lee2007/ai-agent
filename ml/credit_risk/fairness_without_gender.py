
import pandas as pd

from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    roc_auc_score,
    confusion_matrix
)


# 1. 读取数据
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

# 3. 保存性别信息，用于后续公平性分析
gender_valid = X_valid["X2"].copy()

# 4. 删除性别特征
X_subtrain = X_subtrain.drop(columns=["X2"])
X_valid = X_valid.drop(columns=["X2"])

print("原始特征数量：23")
print("删除性别特征后的数量：", X_subtrain.shape[1])

# 5. 定义字段
numeric_features = [
    "X1", "X5",
    "X12", "X13", "X14", "X15", "X16", "X17",
    "X18", "X19", "X20", "X21", "X22", "X23"
]

categorical_features = [
    "X3", "X4",
    "X6", "X7", "X8", "X9", "X10", "X11"
]

# 6. 建立预处理流程
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

# 7. 建立随机森林模型
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

# 8. 训练模型
print("\n开始训练删除性别特征后的模型...")
model.fit(X_subtrain, y_subtrain)
print("训练完成！")

# 9. 预测
y_pred = model.predict(X_valid)
y_proba = model.predict_proba(X_valid)[:, 1]

# 10. 整体性能评估
print("\n========== 删除性别特征后的模型表现 ==========")

print("Accuracy：", round(accuracy_score(y_valid, y_pred), 4))
print(
    "Precision：",
    round(precision_score(y_valid, y_pred, zero_division=0), 4)
)
print("Recall：", round(recall_score(y_valid, y_pred), 4))
print("ROC-AUC：", round(roc_auc_score(y_valid, y_proba), 4))

print("混淆矩阵：")
print(confusion_matrix(y_valid, y_pred, labels=[0, 1]))


# 11. 性别分组公平性分析
results = pd.DataFrame({
    "gender": gender_valid.values,
    "actual": y_valid.values,
    "predicted": y_pred
})

print("\n========== 删除性别特征后的性别分组分析 ==========")

for gender, group in results.groupby("gender"):

    actual = group["actual"]
    predicted = group["predicted"]

    tn, fp, fn, tp = confusion_matrix(
        actual,
        predicted,
        labels=[0, 1]
    ).ravel()

    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0

    gender_name = "男性" if gender == 1 else "女性"

    print(f"\n群体：{gender_name}")
    print("样本数量：", len(group))
    print("Recall：", round(recall_score(actual, predicted, zero_division=0), 4))
    print("FPR：", round(fpr, 4))
    print("Precision：", round(precision_score(actual, predicted, zero_division=0), 4))

print("\n实验完成！")
