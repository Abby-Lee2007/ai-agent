
import pandas as pd

# 读取数据
X = pd.read_csv("datasets/raw/credit_features.csv")
y = pd.read_csv("datasets/raw/credit_target.csv")

# 合并特征和标签
data = pd.concat([X, y], axis=1)

# 找出所有重复特征对应的记录
duplicate_mask = X.duplicated(keep=False)
duplicates = data.loc[duplicate_mask].sort_values(
    by=list(X.columns)
)

print("========== 重复记录调查 ==========")

print("重复特征涉及的记录总数：", len(duplicates))

print(
    "重复特征组合数量：",
    X.loc[duplicate_mask].drop_duplicates().shape[0]
)

print("\n========== 重复记录的标签分布 ==========")
print(duplicates["Y"].value_counts())

print("\n========== 同一特征对应不同标签的组合 ==========")

label_counts = duplicates.groupby(
    list(X.columns)
)["Y"].nunique()

conflicts = label_counts[label_counts > 1]

print("标签不一致的重复特征组合数量：", len(conflicts))

print("\n========== 重复记录样例 ==========")
print(duplicates[["X1", "X2", "X3", "X4", "X5", "Y"]].head(20))
