
import pandas as pd
import matplotlib.pyplot as plt

# 1. 整理测试集实验结果
results = {
    "Model": [
        "Logistic Regression",
        "Balanced Logistic Regression",
        "Random Forest"
    ],
    "Accuracy": [
        0.8168,
        0.7735,
        0.7765
    ],
    "Precision": [
        0.6615,
        0.4893,
        0.4956
    ],
    "Recall": [
        0.3519,
        0.5531,
        0.5991
    ],
    "F1": [
        0.4594,
        0.5193,
        0.5425
    ],
    "ROC-AUC": [
        0.7583,
        0.7588,
        0.7764
    ]
}

df = pd.DataFrame(results)

# 2. 输出结果表格
print("\n========== 模型实验结果对比 ==========\n")

print(df.round(4).to_string(index=False))

# 3. 保存 CSV 文件
df.to_csv(
    "ml/credit_risk/model_comparison.csv",
    index=False
)

print("\n结果表格已保存：")
print("ml/credit_risk/model_comparison.csv")

# 4. 绘制模型指标对比图
metrics = [
    "Accuracy",
    "Precision",
    "Recall",
    "F1",
    "ROC-AUC"
]

plot_df = df.set_index("Model")[metrics]

ax = plot_df.plot(
    kind="bar",
    figsize=(12, 6),
    width=0.8
)

ax.set_title("Credit Risk Model Performance Comparison")
ax.set_ylabel("Score")
ax.set_xlabel("Model")
ax.set_ylim(0, 1)
ax.legend(loc="lower right")
ax.grid(axis="y", linestyle="--", alpha=0.4)

plt.xticks(rotation=15, ha="right")
plt.tight_layout()

plt.savefig(
    "ml/credit_risk/model_comparison.png",
    dpi=300
)

plt.show()

print("\n模型对比图已保存：")
print("ml/credit_risk/model_comparison.png")
