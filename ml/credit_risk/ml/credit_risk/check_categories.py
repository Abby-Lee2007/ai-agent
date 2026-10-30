import pandas as pd

X = pd.read_csv("datasets/raw/credit_features.csv")

category_columns = [
    "X2", "X3", "X4",
    "X6", "X7", "X8",
    "X9", "X10", "X11"
]

print("========== 类别字段取值检查 ==========")

for column in category_columns:
    print(f"\n字段：{column}")
    print("不同取值：", sorted(X[column].unique()))
    print("各取值出现次数：")
    print(X[column].value_counts().sort_index())