
from ucimlrepo import fetch_ucirepo
from pathlib import Path

print("正在连接 UCI 数据仓库...")

# 1. 获取信用卡违约数据集
dataset = fetch_ucirepo(id=350)

print("数据获取成功，正在提取数据...")

# 2. 提取特征数据 X 和目标标签 y
X = dataset.data.features
y = dataset.data.targets

# 3. 创建数据保存目录
output_dir = Path("datasets/raw")
output_dir.mkdir(parents=True, exist_ok=True)

# 4. 保存数据
X.to_csv(output_dir / "credit_features.csv", index=False)
y.to_csv(output_dir / "credit_target.csv", index=False)

# 5. 显示下载结果
print("\n========== 下载结果 ==========")
print("特征数据规模：", X.shape)
print("标签数据规模：", y.shape)
print("目标字段：", y.columns.tolist())

print("\n前五条标签数据：")
print(y.head())

print("\n数据下载完成！")
