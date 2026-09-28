"""特征工程与数据集切分。**当前为占位，未实现。**

对应设计文档 §28（变化率）、§30（Isolation Forest 的特征清单）、
§32（时间序列切分规则）。

★ 为什么这个文件是"占位但函数签名写全"，而不是干脆不建：

    文档 §19 的目录结构里列了它，而"什么时候建文件"这件事上，
    早建有一个具体的好处：**接口形状先定下来，算法可以换**。

    §26 的第一原则是"先有数据，再训练模型"，所以本文件在未来几周
    都不会有实现。但如果现在不把"特征长什么样、切分怎么切"写下来，
    等真要训练时就会一边想算法一边改接口，两件事互相干扰。

★ 两个现在就要钉死的约束：

    1. **特征只能是"从过去算出来的"**（§28）。
       比如 delta_temperature 是"和上一条比变了多少"，
       绝不能引入任何"用了未来数据"的量（比如整段均值）。
       这类错误叫数据泄漏，离线指标会好得离谱，上线直接废，
       而且**从指标上看不出来**。

    2. **切分必须按时间顺序**（§32）：
       前 70% 训练 / 中 15% 验证 / 后 15% 测试。
       随机切分会让模型"看到未来"——训练集里有测试时刻之后的数据，
       于是模型记住了那段时间的分布。这是同一个泄漏的另一副面孔。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 文档 §30 给的特征清单。写成常量而不是散在代码里，
# 是为了让"模型输入是什么"这件事有一个可以被断言、被打印的地方
# （训练和推理两侧必须用完全相同的顺序，否则模型会静默读错列）。
FEATURE_COLUMNS: tuple[str, ...] = (
    "temperature",
    "humidity",
    "delta_temperature",   # §28 变化率
    "delta_humidity",      # §28 变化率
    "temp_roll_mean",      # §29 滚动均值（窗口见 ROLLING_WINDOW_POINTS）
    "temp_roll_std",       # §29 滚动标准差
    "humidity_roll_mean",
    "humidity_roll_std",
    "hour_of_day",         # 周期性特征：温湿度有日周期
    "rssi",                # 链路质量，和物理量无因果关系，
                           # 但异常低时往往说明设备要掉线了
)

# 滚动窗口的点数。设备 2 秒一条 → 900 点 = 30 分钟（§29 的建议窗口）。
ROLLING_WINDOW_POINTS = 900

# 时间序列切分比例（§32）。三段之和必须是 1.0。
SPLIT_RATIOS = (0.70, 0.15, 0.15)


@dataclass(frozen=True)
class FeatureSet:
    """一批特征与对应的标签/时间。

    ``timestamps`` 单独留着（不塞进 ``values``），因为它是"用来排序和切分"
    的，不是"喂给模型的"。混在一起最容易犯的错就是把时间戳也当成一个特征
    训进去——模型会学到"时间越大越正常"，而这是纯粹的过拟合。
    """

    timestamps: list[int]      # Unix 毫秒，严格递增
    columns: tuple[str, ...]   # 与 values 的列一一对应
    values: list[list[float]]


def build_features(
    rows: Sequence[dict[str, object]],
    *,
    window_points: int = ROLLING_WINDOW_POINTS,
) -> FeatureSet:
    """把遥测行转成特征矩阵。

    :param rows: 按 ts 升序排列的遥测行（``clock`` 必须是 ``unix``）
    :param window_points: 滚动窗口的点数
    :raises NotImplementedError: 本轮（Phase 6）不实现，Phase 8 才做
    """
    raise NotImplementedError(
        "特征工程属于 Phase 8（文档 §28-§30），本轮不实现。"
        "前置条件：先积累至少几天到几周的真实数据（§26）。"
    )


def time_ordered_split(
    feature_set: FeatureSet,
    *,
    ratios: tuple[float, float, float] = SPLIT_RATIOS,
) -> tuple[FeatureSet, FeatureSet, FeatureSet]:
    """按**时间顺序**切成 训练 / 验证 / 测试 三段（文档 §32）。

    ★ 绝对不能用 sklearn 的 ``train_test_split``（它默认随机打乱）。
      随机切分在时间序列上是错的，原因见文件头。

    :raises NotImplementedError: 本轮（Phase 6）不实现，Phase 8 才做
    """
    raise NotImplementedError(
        "数据集切分属于 Phase 8（文档 §32），本轮不实现。"
    )
