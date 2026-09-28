"""异常检测。**当前为占位，未实现。**

对应设计文档 §27（规则检测）→ §28（变化率）→ §29（Rolling Z-Score）
→ §30（Isolation Forest），四版逐步升级。

★ 为什么要排四版、而不是直接上 Isolation Forest：

    文档 §27 自己就说了第一版"这不算真正的 AI"。它仍然值得先做，
    原因是**判据的可解释性**：规则版能明确告诉你"因为温度超过 50℃"，
    而模型版只能给一个分数。在系统还没跑稳的时候，一个可解释的
    假警报比一个不可解释的真警报更有价值——前者能帮你发现
    "原来传感器在开机头几分钟会读到 0"，后者只会让你怀疑模型。

    还有一个更实际的理由：前三版都**不需要训练**，也不需要有标签。
    先跑起来攒数据，等数据够了再上模型，路径是连续的。

★ 输出格式在文档 §33 已经定死，不要改：

    {"device_id": ..., "ts": ..., "metric": ...,
     "anomaly_score": ..., "is_anomaly": ..., "reason": ...}

    Web 页面直接用这套数据，所以它比"内部实现长什么样"更需要稳定。
    落库对应 anomalies 表的列（metric / score / level / reason）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 告警级别。与 anomalies.level 列的取值集合对应。
#
# 为什么要分三级而不是一个布尔 is_anomaly：
#   布尔只能回答"要不要报警"，回答不了"要不要现在起床看"。
#   info 可以在页面上标个黄点，critical 才值得推通知。
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
LEVELS: tuple[str, ...] = (LEVEL_INFO, LEVEL_WARNING, LEVEL_CRITICAL)

# 检测方法名。写进 anomalies.reason 便于事后区分"这条是谁报的"——
# 换方法之后，旧数据的判据不能按新方法的逻辑去解释。
METHOD_RULES = "rules"          # §27
METHOD_DELTA = "delta"          # §28
METHOD_ZSCORE = "zscore"        # §29
METHOD_ISOLATION_FOREST = "iforest"  # §30


@dataclass(frozen=True)
class AnomalyHit:
    """一条异常判定结果。字段与 anomalies 表一一对应。"""

    ts: int
    metric: str
    score: float
    level: str
    reason: str


def detect(
    rows: Sequence[dict[str, object]],
    *,
    method: str = METHOD_RULES,
) -> list[AnomalyHit]:
    """对一批遥测行做异常检测。

    :param rows: 按 ts 升序的遥测行（``clock`` 必须是 ``unix``）
    :param method: 见 ``METHOD_*`` 常量
    :raises NotImplementedError: 本轮（Phase 6）不实现，Phase 8 才做
    """
    raise NotImplementedError(
        "异常检测属于 Phase 8（文档 §27-§30），本轮不实现。"
        "前置条件：先积累至少几天到几周的真实数据（§26）。"
    )
