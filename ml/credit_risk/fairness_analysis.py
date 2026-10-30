
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
    confusion_matrix
)
def calculate_fpr(y_true, y_pred):
    tn, fp, fn, tp = confusion_matrix(
        y_true,
        y_pred,
        labels=[0, 1]
    ).ravel()

    if tn + fp == 0:
        return 0

    return fp / (tn + fp)

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
print("开始训练模型...")
model.fit(X_subtrain, y_subtrain)
print("训练完成！")

# 7. 在验证集上预测
y_proba = model.predict_proba(X_valid)[:, 1]
y_pred = (y_proba >= 0.5).astype(int)

# 8. 整理验证集结果
results = X_valid[["X2", "X5"]].copy()
results["actual"] = y_valid.values
results["predicted"] = y_pred

# 9. 按性别分组分析
gender_names = {
    1: "男性",
    2: "女性"
}

print("\n========== 性别分组公平性分析 ==========")

for gender, group in results.groupby("X2"):

    actual = group["actual"]
    predicted = group["predicted"]

    print(f"\n群体：{gender_names.get(gender, str(gender))}")
    print("样本数量：", len(group))
    print("实际违约率：", round(actual.mean(), 4))
    print("预测违约率：", round(predicted.mean(), 4))
    print("Accuracy：", round(accuracy_score(actual, predicted), 4))
    print(
        "Precision：",
        round(precision_score(actual, predicted, zero_division=0), 4)
    )
    print("Recall：", round(recall_score(actual, predicted, zero_division=0), 4))
    print("FPR：", round(calculate_fpr(actual, predicted), 4))

    print("混淆矩阵：")
    print(confusion_matrix(actual, predicted, labels=[0, 1]))

print("\n性别分组分析完成！")
# 10. 按年龄分组分析
results["age_group"] = pd.cut(
    results["X5"],
    bins=[0, 29, 39, 49, float("inf")],
    labels=["30岁以下", "30-39岁", "40-49岁", "50岁及以上"]
)

print("\n========== 年龄分组公平性分析 ==========")

for age_group, group in results.groupby("age_group", observed=True):

    actual = group["actual"]
    predicted = group["predicted"]

    print(f"\n年龄群体：{age_group}")
    print("样本数量：", len(group))
    print("实际违约率：", round(actual.mean(), 4))
    print("预测违约率：", round(predicted.mean(), 4))
    print("Accuracy：", round(accuracy_score(actual, predicted), 4))
    print(
        "Precision：",
        round(precision_score(actual, predicted, zero_division=0), 4)
    )
    print("Recall：", round(recall_score(actual, predicted, zero_division=0), 4))
    print("FPR：", round(calculate_fpr(actual, predicted), 4))

    print("混淆矩阵：")
    print(confusion_matrix(actual, predicted, labels=[0, 1]))

print("\n年龄分组分析完成！")
