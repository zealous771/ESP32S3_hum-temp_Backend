"""HTTP 响应模型。

与 ``app/mqtt/schemas.py``（入站契约）分开：

    mqtt/schemas.py   **设备 → 后端**的报文格式，由固件决定
    api/schemas.py    **后端 → 前端**的响应格式，由我们决定

两者不是一回事。设备报文改字段是固件的事，接口响应改字段是我们的决定，
变化的原因不同，所以放两个文件。

★ 关于时间字段的格式：一律用 **Unix 毫秒 int**，不加 ISO 字符串版本。

    理由：固件、数据库、文档 §23 的示例全都是毫秒整数，保持一致就没有
    任何时区歧义。而"渲染成人能读的格式"是展示层的事——前端 JS 里
    `new Date(ts)` 一行就够，后端多给一个字段反而要维护两套语义。
    将来真有人需要，再加一个可选的 `?format=iso` 参数，而不是默认塞两遍。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页信封。

    为什么不直接返回数组：前端需要知道**总数**才能画分页控件。
    只给当前页的话，得靠"返回条数 < limit 说明到底了"来推断，
    而"恰好整除"时这个推断是错的。

    ``total`` 是**符合筛选条件的总数**，不是当前页的条数。
    """

    total: int = Field(description="符合筛选条件的总条数（不是当前页条数）")
    limit: int
    offset: int
    items: list[T]


# ── 遥测 ────────────────────────────────────────────────────────────


class TelemetryOut(BaseModel):
    """一条遥测记录。

    字段名与数据库列名一致（temperature / humidity，不带 _c / _rh 后缀）——
    因为这是**数据库视图**，而 device 报文里的 temperature_c 是**报文契约**。
    两者刻意不同名，避免有人以为可以直接把响应塞回 MQTT。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: int = Field(description="设备采样时刻（Unix 毫秒）")
    received_at: int = Field(description="后端收到时刻（Unix 毫秒，后端时钟）")
    temperature: float
    humidity: float
    rssi: int | None = None
    seq: int
    clock: str = Field(description="ts 的时间基准：unix / boot")


class LatestTelemetryOut(BaseModel):
    """``GET /api/devices/{id}/latest`` 的响应。

    ★ 字段集合**恰好**是设计文档 §23 示例里的那 5 个：
      device_id / temperature / humidity / rssi / ts

      刻意**不多给**（比如不带 received_at / seq）：
      这个接口会被 Web 首页每 5 秒调一次（文档 §25），
      响应体越小越好。要更多字段就用 /api/telemetry。
      有测试盯住这个键集合——多一个少一个都会失败。
    """

    device_id: str
    temperature: float
    humidity: float
    rssi: int | None = None
    ts: int


# ── 设备 ────────────────────────────────────────────────────────────


class DeviceOut(BaseModel):
    """设备台账的一条。

    同时给 ``status`` 和 ``online``，因为它们是**两个不同的东西**：

        status   设备最后上报的状态（来自 iot/<id>/status 的 online/offline）
                 它是"设备说自己在不在线"
        online   后端**读时计算**出来的（status=='online' 且 last_seen 够新）

    为什么两个都要给：LWT 只在**异常断连**时由 broker 代发，
    固件卡死时它不主动断开 TCP，status 会永远停在 online。
    只看 status 的前端会一直显示"在线"。
    """

    model_config = ConfigDict(from_attributes=True)

    device_id: str
    name: str | None = None
    status: str
    online: bool = Field(description="读时计算：status=='online' 且 last_seen 距今 <=30 秒")
    last_seen: int | None = Field(default=None, description="最后收到报文的后端时刻")
    created_at: int


class DeviceDetailOut(DeviceOut):
    """``GET /api/devices/{id}`` 在设备基本信息之外多给几个统计量。

    这些数从**库里**算，与内存里的 SeqTracker 无关——SeqTracker 会因为
    后端重启而清零，而这里反映的是持久化的历史。
    """

    telemetry_count: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    last_seq: int | None = Field(
        default=None,
        description="ts 最大的那一行的 seq（不是 seq 的最大值——设备重启后 seq 会回到 1）",
    )


# ── 异常与预测（本轮恒为空）──────────────────────────────────────────


class AnomalyOut(BaseModel):
    """异常记录。字段对应设计文档 §21 的 anomalies 表。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    ts: int
    metric: str
    score: float
    level: str = Field(description="info / warning / critical")
    reason: str | None = None


class PredictionOut(BaseModel):
    """预测记录。

    ``ts`` 是**预测生成时刻**，不是被预测的时刻。
    被预测的时刻 = ``ts + horizon * 1000``。这个语义在文档 §33 之外
    没有明说，但必须定死，否则将来没法回溯"当初预测得准不准"。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    ts: int = Field(description="预测生成时刻（Unix 毫秒）")
    horizon: int = Field(description="向前预测多少秒；被预测时刻 = ts + horizon*1000")
    metric: str
    value: float
