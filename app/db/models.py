"""ORM 模型：四张表。

字段名来自设计文档 §21，但文档**只给了字段名，没给类型、主键、索引**——
那些是这里补的，每处都写了理由。

    devices     设备台账（自动注册）
    telemetry   遥测数据（主体）
    anomalies   异常记录（Phase 8 才有数据，本轮建表占位）
    predictions 预测记录（Phase 9 才有数据，本轮建表占位）

为什么 anomalies / predictions 现在就建、API 也一起做：

    把"表结构和接口形状"与"算法实现"解耦。算法可以换
    （规则 → Z-Score → Isolation Forest，见文档 §27-§30），
    表结构和接口不用动。等 Phase 8 开始时，只需要往这两张表写数据。

★ 两条时间轴（本工程最重要的一个数据设计）

    telemetry 表同时存 ts 和 received_at：

      ts           设备**采样**时刻（设备时钟，Unix 毫秒）
      received_at  后端**收到**时刻（后端时钟，Unix 毫秒）

    两者的差 = 这条数据在路上走了多久。更关键的是，**只有 received_at
    能发现"设备静默停发"**——实测踩过：设备连续发 41 分钟后彻底停发，
    持续 2.5 小时，本地一切正常。只有 ts 的话那段时间就是"没有行"，
    你分不清"没采"和"采了没发出来"，更看不出停了多久。

    详见 app/clock.py 的说明。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有模型的基类（SQLAlchemy 2.0 风格）。"""


class Device(Base):
    """设备台账。

    每收到一个**没见过**的 device_id 就自动插一行——否则 /api/devices
    永远是空的，而数据其实一直在进来。

    ★ 注意 status / last_seen 的语义（文档 §17）：

        status      存的是**设备最后上报的状态**（来自 iot/<id>/status 的
                    online/offline）。它是"设备说自己在不在线"。
        last_seen   用**后端时钟**记录最后一次收到任何报文的时间。

      为什么不能只看 status 判断在线：LWT（遗嘱消息）只在**异常断连**时
      才由 broker 代发。如果固件本身卡死了（任务死锁、看门狗还没到），
      它不主动断开 TCP，broker 不会发 LWT，status 会永远停在 online。
      所以真正的"在线"必须是**读时计算**：status == 'online' 且
      last_seen 距今不超过阈值。计算逻辑在 repository.py 里。
    """

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 显示名。本轮没有改名入口，留 NULL，将来做设备管理页时填。
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # online / offline / unknown（约束见下面的 __table_args__）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")

    # 最后一次收到该设备**任何**报文的时间（后端时钟，Unix 毫秒）
    last_seen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 首次出现的时间（后端时钟）。只在真正 INSERT 时写一次
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('online', 'offline', 'unknown')", name="ck_devices_status"
        ),
    )


class Telemetry(Base):
    """遥测数据。

    这是数据量最大的一张表（单设备 2 秒一条 ≈ 每天 43200 行）。
    """

    __tablename__ = "telemetry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── 时间轴一：设备采样时刻 ──────────────────────────────────
    # 单位毫秒。基准由 clock 列决定：
    #   clock == "unix" → 自 1970 起的毫秒（NTP 已同步，正常情况）
    #   clock == "boot" → 开机以来的毫秒（NTP 还没同步时的降级值，只有几千）
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ── 时间轴二：后端收到时刻 ──────────────────────────────────
    # ★ 后端时钟，永远可用、永远是绝对时间（不依赖设备 NTP 是否成功）。
    #   用途：发现静默停发、计算链路延迟、按"入库时间"回溯。
    #   见 app/clock.py 和本文件顶部的说明。
    received_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ── 测量值 ─────────────────────────────────────────────────
    # 用 Float 而不是 Integer：现在 DHT11 报的是整数，但固件侧将来换
    # SHT30（0.01℃ 分辨率）会变成浮点。Float 现在也能存整数，
    # 提前用它就不用到时改表。
    #
    # ★ 故意**不加**上下界约束：超范围的读数（比如 80℃）正是 Phase 8
    #   要抓的异常。数据库层把它拦掉的话，异常检测就没有输入了。
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    humidity: Mapped[float] = mapped_column(Float, nullable=False)

    # 可为 NULL：设备未联网时固件报的是 JSON null。
    # ★ 不要用 0 当"读不到"的哨兵——0 dBm 是合法且极强的读数。
    rssi: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 设备侧单调递增的消息序号（从 1 开始）。
    # 用途见文档 §15：跳号 = 中间丢了数据。后端用 SeqTracker 在内存里判，
    # 库里这一列是留给将来做"丢包率审计"的。
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # "unix" / "boot"，见 ts 的说明
    clock: Mapped[str] = mapped_column(String(8), nullable=False, default="unix")

    __table_args__ = (
        # ── 索引 1：唯一约束 ────────────────────────────────────
        # 服务三个用途：
        #   1. /api/telemetry 的按设备+时间范围扫描
        #   2. /api/devices/{id}/latest 的按设备取最大 ts
        #   3. ★ QoS1 重投的**天然幂等**——QoS1 是"至少一次"语义，
        #      broker 在没收到 PUBACK 时会重投，所以后端必然会收到重复报文。
        #      有了这个唯一约束，插入时 ON CONFLICT DO NOTHING 就自动去重，
        #      不用在应用层维护"见过哪些消息"的状态。
        UniqueConstraint(
            "device_id", "ts", "seq", name="uq_telemetry_device_ts_seq"
        ),

        # ── 索引 2：部分索引（只索引 clock='unix' 的行）─────────
        # 服务跨设备的时间范围查询（图表、统计）。
        #
        # 为什么用部分索引而不是全表 ts 索引：
        #   ① 体积小——boot 行（ts 只有几千）永远不在时间范围查询里，
        #      没必要为它们维护索引项。
        #   ② ★ 副作用是好的：如果哪天有人写查询时忘了带 clock='unix'
        #      条件，SQLite 就用不上这个索引，**查询变慢而不是变错**。
        #      这是用性能换正确性的一道软保护。
        Index(
            "ix_telemetry_ts_unix",
            "ts",
            sqlite_where=text("clock = 'unix'"),
        ),

        # ── 索引 3：跳号审计用 ──────────────────────────────────
        # 本轮只建不查。将来做"某设备某段时间丢了多少条"的分析时，
        # 按 (device_id, seq) 排序扫一遍就够了。
        #
        # ★ 注意**不能**给 (device_id, seq) 加 UNIQUE：
        #   设备重启后 seq 从 1 重新开始，同一设备在历史上会有多个 seq=1。
        #   加唯一约束会直接**拒绝合法的重启后数据**——这是那种
        #   "上线几周后才在设备第一次重启时炸掉"的错误。
        Index("ix_telemetry_device_seq", "device_id", "seq"),

        # ── 索引 4：按入库时间查询 ──────────────────────────────
        # 服务两件事：
        #   ① 发现"静默停发"——查相邻 received_at 的断层
        #   ② 保留策略清理（按 received_at 删旧数据，比按 ts 删更可靠，
        #      因为 ts 可能因设备时钟不对而失真）
        Index("ix_telemetry_received_at", "received_at"),
    )


class Anomaly(Base):
    """异常记录（文档 §21）。**本轮不会有数据**，表先建好。

    Phase 8 的四种检测（规则 / 变化率 / Rolling Z-Score /
    Isolation Forest，见文档 §27-§30）都会往这张表写。
    """

    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )

    # 异常发生的时刻（用**被判定那条数据**的 ts，不是检测运行的时刻）
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 被判定异常的指标名：temperature / humidity / rssi / ...
    metric: Mapped[str] = mapped_column(String(32), nullable=False)

    # 异常分数。不同算法量纲不同（Z-Score 是标准差倍数、
    # Isolation Forest 是 0~1），所以**不要在列上假设范围**。
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # info / warning / critical
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")

    # 人可读的原因，会直接显示在 Web 页面上（文档 §33）
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "level IN ('info', 'warning', 'critical')", name="ck_anomalies_level"
        ),
        # 查"某设备最近的异常"是最常见的用法
        Index("ix_anomalies_device_ts", "device_id", "ts"),
        Index("ix_anomalies_ts", "ts"),
    )


class Prediction(Base):
    """预测记录（文档 §21）。**本轮不会有数据**，表先建好。"""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )

    # ★ 这一列是**预测生成时刻**，不是被预测的时刻。
    #   被预测的时刻 = ts + horizon * 1000。
    #
    #   为什么这样定：预测是"在某时刻做出的一个关于未来的陈述"，
    #   同一个未来时刻可能被预测多次（模型改进后重算）。
    #   如果 ts 存的是被预测时刻，两次不同时间做出的预测会混在一起，
    #   分不清哪个是后来修正的。
    #
    #   这个语义必须在 Phase 9 之前定死，否则将来没法回溯"当初预测得准不准"。
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 向前预测多少秒。例：300 = 预测 5 分钟后的值（文档 §33 的 "t+5m"）
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)

    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_predictions_device_ts", "device_id", "ts"),
    )
