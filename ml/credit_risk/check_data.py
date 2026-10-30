
import pandas as pd

# 读取原始数据
X = pd.read_csv("datasets/raw/credit_features.csv")
y = pd.read_csv("datasets/raw/credit_target.csv")

print("========== 1. 数据规模 ==========")
print("特征数据：", X.shape)
print("标签数据：", y.shape)

print("\n========== 2. 数据类型 ==========")
print(X.dtypes)

print("\n========== 3. 缺失值检查 ==========")
print(X.isnull().sum())

print("\n========== 4. 重复记录检查 ==========")
print("重复特征记录数量：", X.duplicated().sum())

print("\n========== 5. 数值统计 ==========")
print(X.describe().T)

print("\n========== 6. 标签分布 ==========")
print(y["Y"].value_counts())
