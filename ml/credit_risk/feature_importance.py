
import pandas as pd
import matplotlib.pyplot as plt

from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestClassifier

# 1. 读取训练数据
X_train = pd.read_csv("datasets/processed/X_train.csv")
y_train = pd.read_csv("datasets/processed/y_train.csv")["Y"]

# 2. 定义字段
numeric_features = [
    "X1", "X5",
    "X12", "X13", "X14", "X15", "X16", "X17",
    "X18", "X19", "X20", "X21", "X22", "X23"
]

categorical_features = [
    "X2", "X3", "X4",
    "X6", "X7", "X8", "X9", "X10", "X11"
]

# 3. 数据预处理
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

# 4. 建立随机森林模型
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

# 5. 训练模型
print("开始训练随机森林...")
model.fit(X_train, y_train)
print("训练完成！")

# 6. 获取预处理后的特征名称
feature_names = model.named_steps[
    "preprocessor"
].get_feature_names_out()

# 7. 获取随机森林特征重要性
importances = model.named_steps[
    "classifier"
].feature_importances_

importance_df = pd.DataFrame({
    "feature": feature_names,
    "importance": importances
})

# 8. 按重要性从高到低排序
importance_df = importance_df.sort_values(
    by="importance",
    ascending=False
)

print("\n========== Top 20 特征重要性 ==========")
print(importance_df.head(20).to_string(index=False))

# 9. 汇总原始字段的重要性
# One-Hot 编码会把一个类别字段拆成多个特征，
# 因此把同一原始字段对应的重要性加总。
importance_df["original_feature"] = (
    importance_df["feature"]
    .str.replace("num__", "", regex=False)
    .str.replace("cat__", "", regex=False)
    .str.split("_")
    .str[0]
)

grouped_importance = (
    importance_df
    .groupby("original_feature")["importance"]
    .sum()
    .sort_values(ascending=False)
)

print("\n========== 原始字段重要性 ==========")
print(grouped_importance.to_string())

# 10. 绘制前 15 个原始字段
top_features = grouped_importance.head(15).sort_values()

plt.figure(figsize=(10, 7))
top_features.plot(kind="barh")

plt.title("Random Forest Feature Importance")
plt.xlabel("Importance")
plt.ylabel("Original Feature")
plt.tight_layout()

# 保存图片
plt.savefig(
    "ml/credit_risk/feature_importance.png",
    dpi=300,
    bbox_inches="tight"
)

plt.show()

print("\n特征重要性图已保存！")
