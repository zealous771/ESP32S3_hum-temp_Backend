"""趋势预测。**当前为占位，未实现。**

对应设计文档 §31：先树模型 / 梯度提升，深入再做 GRU / LSTM / Transformer。

★ 文档 §31 把"先树模型"排在前面是有道理的，值得记下来：

    树模型（XGBoost / LightGBM）在**小数据量**下的表现通常优于
    深度模型，而且不需要调架构、训练只要秒级。传感器数据的日周期
    是低频模式，几十到几百个样本就能被树抓住。

    上 LSTM/Transformer 的代价是：要几万条以上的数据、
    要 GPU 或长时间 CPU 训练、超参一错就完全训不动。
    在没有历史数据时直接上 LSTM 是文档 §26 明确劝阻的做法。

★ 预测值落 predictions 表，列是
  ``(id, device_id, ts, horizon, metric, value)``。

  注意 ``horizon`` 是"预测多远"（比如 5 分钟、10 分钟），
  而 ``ts`` 是**做出预测的时刻**，不是被预测的时刻。
  被预测的时刻 = ts + horizon。这两个语义容易混，
  落库时混了会让画出来的曲线整体错位一段时间，
  而曲线看起来还是"很像"的，不容易发现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 预测跨度（分钟）。文档 §33 的输出示例里给了 t+5m / t+10m。
#
# 为什么不做更长：温湿度的可预测性随时间快速衰减，
# 一小时的预测基本等于"把当前值抄一遍"，没有信息量。
HORIZONS_MINUTES: tuple[int, ...] = (5, 10, 30)

# 可以预测的指标。rssi 故意不在里面——它由无线电环境决定，
# 和温湿度的时间序列没关系，预测它没有物理依据。
FORECAST_METRICS: tuple[str, ...] = ("temperature", "humidity")


@dataclass(frozen=True)
class ForecastPoint:
    """一个预测值。字段与 predictions 表一一对应。"""

    metric: str
    horizon: int      # 分钟
    value: float
    made_at_ms: int   # 做出预测的时刻（写进 predictions.ts）


def forecast(
    rows: Sequence[dict[str, object]],
    *,
    horizons: Sequence[int] = HORIZONS_MINUTES,
) -> list[ForecastPoint]:
    """基于历史遥测预测未来若干个跨度。

    :param rows: 按 ts 升序的遥测行（``clock`` 必须是 ``unix``）
    :param horizons: 预测跨度（分钟）
    :raises NotImplementedError: 本轮（Phase 6）不实现，Phase 8 才做
    """
    raise NotImplementedError(
        "趋势预测属于 Phase 8（文档 §31），本轮不实现。"
        "前置条件：先积累至少几天到几周的真实数据（§26）。"
    )
