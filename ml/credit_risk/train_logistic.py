
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    classification_report,
    confusion_matrix
)

# =====================================
# 1. 读取训练集和测试集
# =====================================

X_train = pd.read_csv("datasets/processed/X_train.csv")
X_test = pd.read_csv("datasets/processed/X_test.csv")

y_train = pd.read_csv("datasets/processed/y_train.csv")["Y"]
y_test = pd.read_csv("datasets/processed/y_test.csv")["Y"]

print("训练集规模：", X_train.shape)
print("测试集规模：", X_test.shape)


# =====================================
# 2. 定义数值字段和类别字段
# =====================================

numeric_features = [
    "X1", "X5",
    "X12", "X13", "X14", "X15", "X16", "X17",
    "X18", "X19", "X20", "X21", "X22", "X23"
]

categorical_features = [
    "X2", "X3", "X4",
    "X6", "X7", "X8", "X9", "X10", "X11"
]


# =====================================
# 3. 创建数据预处理器
# =====================================

numeric_transformer = StandardScaler()

categorical_transformer = OneHotEncoder(
    handle_unknown="ignore"
)

preprocessor = ColumnTransformer(
    transformers=[
        ("num", numeric_transformer, numeric_features),
        ("cat", categorical_transformer, categorical_features)
    ]
)


# =====================================
# 4. 创建逻辑回归模型
# =====================================

model = Pipeline(
    steps=[
        ("preprocessor", preprocessor),
        ("classifier", LogisticRegression(
            max_iter=1000,
            random_state=42
        ))
    ]
)


# =====================================
# 5. 训练模型
# =====================================

print("\n开始训练逻辑回归模型...")

model.fit(X_train, y_train)

print("模型训练完成！")


# =====================================
# 6. 进行预测
# =====================================

y_pred = model.predict(X_test)

y_proba = model.predict_proba(X_test)[:, 1]


# =====================================
# 7. 计算评价指标
# =====================================

print("\n========== 模型评价结果 ==========")

print("Accuracy:", accuracy_score(y_test, y_pred))
print("Precision:", precision_score(y_test, y_pred))
print("Recall:", recall_score(y_test, y_pred))
print("F1 Score:", f1_score(y_test, y_pred))
print("ROC-AUC:", roc_auc_score(y_test, y_proba))

print("\n========== 分类报告 ==========")

print(classification_report(y_test, y_pred))

print("\n========== 混淆矩阵 ==========")

print(confusion_matrix(y_test, y_pred))
