"""运行期计数器与 MQTT 序号（seq）跟踪。

这个模块有三个东西，都很关键——它们决定 /api/health 能不能
回答"数据有没有在流"这个问题。

    IngestStats      数据**进来**的过程：收了多少、入库多少、丢了多少
    RetentionStats   数据**出去**的过程：删了多少、什么时候删的
    SeqTracker       设备侧序号，用来发现"中间少了哪些数据"

为什么把计数器集中在一个模块，而不是散在各个类里：

    /api/health 要一次性报出"收了多少、入库多少、丢了多少、
    跳号多少次"。如果这些数字散在 consumer、service、repository 里，
    健康检查就得持有三个对象的引用并知道每个字段叫什么。
    集中成一个 ``IngestStats`` 之后，health 只看一个对象。

    代价是这个对象被多个线程读写（MQTT 回调线程、写库线程、
    API 请求线程）。所以每个计数都用 **单条原子操作** 更新
    （``+= 1`` 在 CPython 里对 int 是安全的，因为有 GIL），
    而需要"多个字段一起读"的地方接受轻微不一致——
    健康检查读到的 queue_depth 比实际晚几毫秒没有任何影响。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class SeqVerdict(str, Enum):
    """对一条消息的 seq 给出的判定。"""

    FIRST = "first"            # 这台设备的第一次（或后端刚启动后第一次见到它）
    IN_ORDER = "in_order"      # 正好接上
    GAP = "gap"                # 跳号：中间丢了
    DUPLICATE = "duplicate"    # 重复：QoS1 重投，或后端重启后收到旧消息
    OUT_OF_ORDER = "out_of_order"  # 乱序：比上次小，但差值不大
    RESTART = "restart"        # 设备重启：seq 回到小值


@dataclass
class IngestStats:
    """入库管道的全部计数器。

    字段按"数据从哪来 → 到哪去"的顺序排列，方便对着日志读。
    """

    # ── 收到的 ─────────────────────────────────────────────────
    messages_received: int = 0      # 通过 topic 过滤、拿到 payload 的
    topics_ignored: int = 0         # topic 不匹配我们订阅的两种

    # ── 校验 ───────────────────────────────────────────────────
    invalid_messages: int = 0       # JSON 坏 / 字段缺失 / 类型不对 / clock 与 ts 矛盾
    mismatched_device_id: int = 0   # topic 里的 id 与 payload 里的不一致

    # ── 入库 ───────────────────────────────────────────────────
    persisted: int = 0              # 真正写进 telemetry 表的行数
    duplicates_skipped: int = 0     # 唯一约束命中（QoS1 重投），没重复写
    boot_rows_persisted: int = 0    # clock=="boot" 且真的入库了
    boot_rows_dropped: int = 0      # clock=="boot" 且按配置丢弃了

    # ── 队列 ───────────────────────────────────────────────────
    queue_dropped: int = 0          # 队列满被丢掉的（不阻塞回调的代价）
    queue_high_watermark: int = 0   # 队列历史最高占用。持续接近上限说明写库跟不上

    # ── seq ────────────────────────────────────────────────────
    seq_gap_events: int = 0         # 发生过跳号的次数（不是丢的条数）
    seq_missing_messages: int = 0   # 累计丢的条数（跳号缺口之和）

    # ── 设备 ───────────────────────────────────────────────────
    devices_seen: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, object]:
        """给 /api/health 用的纯字典快照。"""
        return {
            "messages_received": self.messages_received,
            "topics_ignored": self.topics_ignored,
            "invalid_messages": self.invalid_messages,
            "mismatched_device_id": self.mismatched_device_id,
            "persisted": self.persisted,
            "duplicates_skipped": self.duplicates_skipped,
            "boot_rows_persisted": self.boot_rows_persisted,
            "boot_rows_dropped": self.boot_rows_dropped,
            "queue_dropped": self.queue_dropped,
            "queue_high_watermark": self.queue_high_watermark,
            "seq_gap_events": self.seq_gap_events,
            "seq_missing_messages": self.seq_missing_messages,
            "devices_seen": sorted(self.devices_seen),
        }


@dataclass
class RetentionStats:
    """保留策略（定期删除过期遥测）的计数器。

    ★ 为什么单独一个类，而不是塞进 IngestStats：

        IngestStats 回答的是"数据进来的过程顺不顺"，
        这个回答的是"数据出去的过程顺不顺"。两者的失败模式、
        排查动作完全不同——前者出问题要去查 broker 和设备，
        后者出问题只跟配置和磁盘有关。混在一起会让 /api/health
        的那个 dict 变成一锅粥，而且以后只会更长。

    只有**已删除**是权威数字。"还剩多少该删的"故意不报：
    那需要在每次健康检查时做一次 COUNT 范围扫描，数据量大时会
    拖慢健康检查，而它对判断"保留策略有没有在工作"没有额外价值——
    看 deleted_total 有没有在涨就够了。
    """

    runs: int = 0                  # 实际执行过的清理次数（不含被跳过的）
    deleted_total: int = 0         # 累计删除行数
    last_deleted: int = 0          # 最近一次删了多少
    last_cutoff_ms: int | None = None   # 最近一次的截止时刻（received_at < 它的被删）
    last_run_ms: int | None = None      # 最近一次的结束时刻（后端时钟）
    last_duration_ms: int = 0      # 最近一次耗时。持续变长说明库在变大

    def snapshot(self) -> dict[str, object]:
        return {
            "runs": self.runs,
            "deleted_total": self.deleted_total,
            "last_deleted": self.last_deleted,
            "last_cutoff_ms": self.last_cutoff_ms,
            "last_run_ms": self.last_run_ms,
            "last_duration_ms": self.last_duration_ms,
        }


class SeqTracker:
    """按设备跟踪 seq，判定每条消息是接上、跳号还是重复。

    ── 为什么在**内存**里做，而不是查库 ─────────────────────────────

        库里的"最后一条 seq"是**按 ts 排**的最后一条，而判断跳号要看的是
        **到达顺序**。这两者不一样：QoS1 重投、网络乱序、后端重启后补发的
        历史消息，都会让"按 ts 的最后一条"落后于"最后到达的一条"。
        用库来判会把重复当成跳号，把跳号当成重复。

    ── 为什么要区分"设备重启"和"跳号" ─────────────────────────────

        设备断电重启后 seq 从 1 重新开始。如果只看"比上次小就是异常"，
        每次设备重启都会报出一个巨大的缺口（比如从 5000 掉到 1，
        报"丢了 4998 条"），把真正的丢包淹没掉。

        判据：``seq <= RESTART_MAX_SEQ 且 seq < last_seq`` → 判为重启。
        取 10 是经验值：正常重启后第一条报文通常是 1~3，
        留到 10 足以覆盖"重启后前几条因队列积压被丢掉"的情况。

    ── 线程安全 ───────────────────────────────────────────────────

        所有方法都在**写库线程**里调用（consumer 把消息入队，
        写库线程统一处理），所以本来不需要锁。但仍然加了锁——
        因为 /api/health 想读 ``last_seq`` 快照时会在 API 线程里访问它。
        加锁的成本在这个量级（2 秒一条）完全可以忽略。
    """

    # 低于这个值且比上次小，判为"设备重启"而不是"跳号"
    RESTART_MAX_SEQ = 10

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, int] = {}
        self._gaps: dict[str, int] = {}
        self._restarts: dict[str, int] = {}

    def observe(self, device_id: str, seq: int) -> SeqVerdict:
        """登记一条消息的 seq，返回判定结果。"""
        with self._lock:
            prev = self._last.get(device_id)
            self._last[device_id] = seq

            if prev is None:
                return SeqVerdict.FIRST

            if seq == prev:
                return SeqVerdict.DUPLICATE

            if seq > prev:
                if seq == prev + 1:
                    return SeqVerdict.IN_ORDER
                # 跳号。注意这**只说明中间有消息没到**，
                # 不区分是设备本地队列溢出还是网络丢包——
                # 两者的区别要看设备日志里的丢弃计数。
                self._gaps[device_id] = self._gaps.get(device_id, 0) + (seq - prev - 1)
                return SeqVerdict.GAP

            # seq < prev
            if seq <= self.RESTART_MAX_SEQ:
                self._restarts[device_id] = self._restarts.get(device_id, 0) + 1
                return SeqVerdict.RESTART
            return SeqVerdict.OUT_OF_ORDER

    def last_seq(self, device_id: str) -> int | None:
        with self._lock:
            return self._last.get(device_id)

    def missing_total(self) -> int:
        """累计跳号缺口之和。"""
        with self._lock:
            return sum(self._gaps.values())

    def gap_events(self) -> int:
        """发生过跳号的设备次数累计。

        注意它的口径：这里返回的是"有多少台设备发生过跳号"，
        而不是"发生过多少次跳号"。要精确到次数得再加一个计数器——
        但 /api/health 想回答的是"有没有丢过数据"，台数已经够了。
        """
        with self._lock:
            return len(self._gaps)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "last_seq": dict(self._last),
                "missing": dict(self._gaps),
                "restarts": dict(self._restarts),
            }
