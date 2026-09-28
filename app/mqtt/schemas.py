"""MQTT 入站报文的契约。

这个文件是**固件与后端之间的接口定义**。它的内容不是从设计文档抄的，
而是从固件源码逐字核对出来的——两者有几处不一致，**以固件为准**：

    字段              固件实际（telemetry_json.c:142）   文档 §14
    ─────────────────────────────────────────────────────────────
    temperature_c     int（DHT11 没有小数位）            26.31 float
    humidity_rh       int                                61.42 float
    rssi              int **或 JSON null**               只有 int
    wifi              bool                               **文档没这个字段**
    clock             "unix" / "boot"                    **文档没这个字段**

温度湿度按 ``float`` 而不是 ``int`` 建模，是为了给将来的 SHT30
（0.01℃ 分辨率）留位置——那时固件换成浮点，后端一行都不用改。
现在 DHT11 的值渲染出来会是 ``32.0``，这只是显示上的小差异。

关于 ``rssi`` 为 null：

    **不要用 0 当"读不到"的哨兵**。固件侧的注释（telemetry_json.c:47-63）
    专门说明过：0 dBm 是合法且极强的信号读数（贴着路由器才有），
    用它表示"未知"会把真实数据污染成"没有数据"。
    所以这里用 ``int | None``，null 就存 SQL NULL，查询时该怎么处理由调用方决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── 时间基准的判定常量 ──────────────────────────────────────────────
#
# 这三个数是"合理时间戳"的边界。它们的作用是拦住明显荒谬的报文
# （设备 RTC 没同步、固件 bug、有人伪造），而不是做精确的时钟校验。

# 2000-01-01T00:00:00Z 的 Unix 毫秒值。
# 取 2000 年而不是 1970 年：任何"ts 是 Unix 时间"的合理报文都不可能
# 早于 2000 年（这个项目是 2026 年做的）。而 boot 行的 ts 只有几千到
# 几百万，用它当分界线正好能把两种基准分开。
MIN_UNIX_MS = 946_684_800_000

# 容忍设备时钟**超前**多久。允许超前是因为设备时钟可能比服务器快一点，
# 但 1 天已经足够宽松——超得更多说明设备时钟坏了。
# 反方向（落后）不用单独设限：落后的 ts 只要 ≥ MIN_UNIX_MS 就接受，
# 历史数据本来就该允许迟到（比如后端重启后补发的积压报文）。
MAX_FUTURE_SKEW_MS = 86_400_000

# 开机毫秒的上限。100_000_000_000 ms ≈ 3.17 年连续开机。
# 超过它就不是"开机以来的毫秒数"了——说明这个报文的 clock 标错了。
MAX_BOOT_MS = 100_000_000_000

# ── topic 契约（文档 §13）───────────────────────────────────────────
#
# iot/<device_id>/telemetry
# iot/<device_id>/status
#
# 用正则一次解析出 device_id 和 kind，而不是 split("/") 后按下标取——
# 下标取值对格式变化毫无抵抗力（多一个层级就静默取错字段），
# 而正则要么匹配要么不匹配。
_TOPIC_RE = re.compile(r"^iot/(?P<device_id>[^/]+)/(?P<kind>telemetry|status)$")

TOPIC_KIND_TELEMETRY = "telemetry"
TOPIC_KIND_STATUS = "status"

# device_id 允许的字符。固件侧来自 Kconfig 的 CONFIG_DEVICE_ID，
# 实际值是 esp32s3_001 这种。限制字符集是为了让 device_id 能安全地
# 用在 URL 路径、SQL 参数、日志里——将来做 Web 前端时 /api/devices/{id}
# 会把它放进路径。
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclass(frozen=True)
class ParsedTopic:
    """topic 解析结果。"""

    device_id: str
    kind: str  # TOPIC_KIND_TELEMETRY 或 TOPIC_KIND_STATUS


def parse_topic(topic: str) -> ParsedTopic | None:
    """解析 ``iot/<device_id>/<kind>``；不匹配返回 None。

    返回 None 是**正常情况**而不是错误——将来会加 ``iot/<id>/cmd``
    之类的下行 topic，那时后端会订阅到自己不认识的 topic。
    调用方应该计个数然后忽略，不要报错。
    """
    match = _TOPIC_RE.match(topic)
    if match is None:
        return None
    return ParsedTopic(device_id=match.group("device_id"), kind=match.group("kind"))


class TelemetryMessage(BaseModel):
    """遥测报文。

    ``extra="ignore"`` 是**刻意**的：

        固件将来加字段（比如加个 battery_v）时，后端不该因为多了一个
        不认识的键就丢掉整条数据。反过来，如果设成 "forbid"，每次固件
        升级都要同步改后端，而部署顺序稍有不对就会大面积丢数据。

        代价是拼错字段名（temperature 写成 temperatuer）会被静默忽略，
        表现为"这个字段永远是默认值"。所以下面的必填字段**没有默认值**——
        缺了就直接报错，不会静默变成 0。
    """

    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(min_length=1, max_length=64)
    ts: int = Field(ge=0)
    seq: int = Field(ge=0, le=2**32 - 1)

    # 必填、无默认值：缺了直接报错，而不是静默变成 0.0
    temperature_c: float
    humidity_rh: float

    # 可选字段。wifi 和 rssi 在设备未联网时是 false / null。
    wifi: bool | None = None
    rssi: int | None = Field(default=None, ge=-150, le=50)
    clock: Literal["unix", "boot"] | None = None

    @field_validator("device_id")
    @classmethod
    def _check_device_id(cls, v: str) -> str:
        if not _DEVICE_ID_RE.match(v):
            raise ValueError(
                f"device_id 含不允许的字符（只允许字母数字和 _ . -）：{v!r}"
            )
        return v

    def resolved_clock(self, *, now_ms: int) -> Literal["unix", "boot"] | None:
        """判定这条报文的时间基准。

        :return: ``"unix"`` / ``"boot"``；**``None`` 表示 clock 与 ts 自相矛盾**，
                 调用方应当拒绝这条报文。

        为什么要检查"自相矛盾"而不是照 clock 存：

            如果 clock 说 unix 而 ts 是 5000（只有 5 秒），把它当成
            1970-01-01 存进去，这条记录会永久污染所有时序查询——
            按时间范围查会把它捞出来，算平均值会被它拉偏，
            而它看起来完全合法（就是个很小的数）。宁可拒收并计数。

        clock 字段缺失时的兜底：按量级推断。
        这是唯一一处用阈值猜基准的地方，而且**只在字段缺失时**发生——
        固件 0729abc 之后一定会带 clock，所以这条路径实际只在
        用 mqtt_print 之类的手工工具造数据时走到。
        """
        if self.clock == "unix":
            if MIN_UNIX_MS <= self.ts <= now_ms + MAX_FUTURE_SKEW_MS:
                return "unix"
            return None

        if self.clock == "boot":
            if 0 <= self.ts <= MAX_BOOT_MS:
                return "boot"
            return None

        # clock 缺失
        if self.ts >= MIN_UNIX_MS:
            return "unix" if self.ts <= now_ms + MAX_FUTURE_SKEW_MS else None
        return "boot" if self.ts <= MAX_BOOT_MS else None


class StatusMessage(BaseModel):
    """在线状态报文（文档 §17）。

    ``online`` 是设备连上后主动发的；``offline`` 是 broker 代发的遗嘱（LWT）。

    ★ 但 **LWT 只在"异常断连"时才会触发**。如果固件本身卡死了
      （任务死锁、看门狗还没到），它不主动断开 TCP，broker 不会发 LWT，
      status 会永远停在 online。所以后端判断"是否在线"时不能只看这个字段，
      还要看 last_seen 有多旧——见 db/repository.py 里的 online 计算。
    """

    model_config = ConfigDict(extra="ignore")

    state: Literal["online", "offline"]
