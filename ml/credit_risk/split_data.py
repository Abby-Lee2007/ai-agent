import pandas as pd
from sklearn.model_selection import train_test_split

# 1. 读取原始数据
X = pd.read_csv("datasets/raw/credit_features.csv")
y = pd.read_csv("datasets/raw/credit_target.csv")["Y"]

print("原始特征数据：", X.shape)
print("原始标签数据：", y.shape)

# 2. 划分训练集和测试集
X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=42,
    stratify=y
)

# 3. 查看划分结果
print("\n========== 数据集划分结果 ==========")

print("训练集特征：", X_train.shape)
print("测试集特征：", X_test.shape)

print("\n训练集标签分布：")
print(y_train.value_counts(normalize=True).sort_index())

print("\n测试集标签分布：")
print(y_test.value_counts(normalize=True).sort_index())

# 4. 保存划分后的数据
output_dir = "datasets/processed"

X_train.to_csv(f"{output_dir}/X_train.csv", index=False)
X_test.to_csv(f"{output_dir}/X_test.csv", index=False)

y_train.to_csv(f"{output_dir}/y_train.csv", index=False)
y_test.to_csv(f"{output_dir}/y_test.csv", index=False)

print("\n训练集和测试集保存成功！")