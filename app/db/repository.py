"""仓储层：**所有 SQL 都在这里**。

api/ 和 services/ 都不直接拼查询，理由有两条：

    1. 将来从 SQLite 换到 PostgreSQL（文档 §22 的计划）时，
       要改的只有这一个文件。
    2. "哪些查询忘了过滤 clock='unix'" 这种审计只需要看一个地方。
       （boot 行的 ts 是开机毫秒、不是绝对时间，混进时序查询会静默出错。）

本文件里的查询一律遵守一条纪律：

    ★ 凡是**按时间范围查 telemetry** 的，默认都带 ``clock='unix'``。
      想查 boot 行必须显式传 ``include_boot=True``。
      这样"忘了过滤"在代码结构上就不可能发生——默认是对的，
      要犯错得主动多写一个参数。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import Anomaly, Device, Prediction, Telemetry

logger = logging.getLogger(__name__)

# 判定"设备在线"的阈值：最后一次收到报文距今不超过这么久。
#
# 为什么需要它、而不只看 devices.status：
#
#   LWT（遗嘱消息）只在**异常断连**时才由 broker 代发。如果固件本身
#   卡死了（任务死锁、看门狗还没到），它不主动断开 TCP，broker 不会发
#   LWT，status 会永远停在 online。实测就遇到过：设备静默停发 2.5 小时，
#   而 status 一直是 online。
#
#   所以"在线"必须是**读时计算**：status == 'online' 且 last_seen 够新。
#
# 阈值取 30 秒的依据：设备每 2 秒发一条，30 秒 = 连续丢 15 条。
# 正常的网络抖动不会丢这么多；真丢了 15 条也确实该报离线。
ONLINE_THRESHOLD_MS = 30_000

# 分页上限。防止有人传 limit=1000000 把内存打爆。
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


# ── 设备 ────────────────────────────────────────────────────────────


def upsert_device(
    session: Session,
    device_id: str,
    *,
    seen_ms: int,
    status: str | None = None,
) -> None:
    """设备自动注册 + 刷新 last_seen。

    ★ 用 ``INSERT ... ON CONFLICT DO UPDATE`` 一条语句完成，
      而不是"先 SELECT 再决定 INSERT/UPDATE"。三个理由：

        1. 一次往返而不是两次（写库线程是热路径）
        2. 原子——不会出现"检查完到写入之间被别的线程插了一行"的竞态
        3. **不依赖"只有一个写者"这个外部约定**。现在确实只有一个写线程，
           但那是当前实现的巧合，不是接口保证；用原子的 upsert 就
           永远不用担心哪天多了一个写者。

    :param seen_ms: 后端时钟（不是设备的 ts）。理由见 app/clock.py。
    :param status:  明确的状态（来自 status 消息）；None 表示"不改变"。
                    ★ 注意 status=None 时**不能**覆盖成 unknown——
                      否则一条遥测会把设备刚报的 offline 冲掉。
    """
    values: dict[str, Any] = {
        "device_id": device_id,
        "status": status if status is not None else "unknown",
        "last_seen": seen_ms,
        "created_at": seen_ms,
    }

    stmt = sqlite_insert(Device).values(**values)

    # 冲突时要更新的列。**刻意不包含 name 和 created_at**：
    #   created_at 只在首次插入时有意义，每次上报都刷新它就失去意义了
    #   name 是用户手动设的显示名，不该被上报覆盖
    set_: dict[str, Any] = {"last_seen": seen_ms}
    if status is not None:
        set_["status"] = status

    stmt = stmt.on_conflict_do_update(
        index_elements=[Device.device_id],
        set_=set_,
    )
    session.execute(stmt)

    # ★ 让身份映射里可能存在的同一个 Device 对象过期。
    #
    #   上面的 upsert 是 **Core 语句**（直接发 SQL），它绕过了 ORM 的
    #   身份映射。也就是说：如果本次会话之前已经 ``session.get(Device, id)``
    #   把这一行加载进内存，那么执行完 UPDATE 之后，**内存里那个对象
    #   还是旧值**——SQLAlchemy 不知道底层数据变了。
    #
    #   后果是一个很难查的 bug：写路径里 upsert 之后顺手读一下设备
    #   （比如为了打日志），拿到的是更新前的 last_seen / status。
    #   而换一个会话读又是对的——于是表现为"有时候对有时候不对"。
    #
    #   为什么用 expire_all() 而不是精确过期那一个对象：
    #   精确过期需要拼 identity_key，用内部 API 不划算。而这里的代价
    #   几乎为零——写路径（每 2 秒一次）执行 upsert 时，会话里通常
    #   只有这一个对象；expire 只是打个标记，下次访问才真的重查，
    #   而写路径之后并不会读回它。
    #
    #   注意 expire_all() 会丢弃**未提交的 ORM 改动**。本工程的写路径
    #   只用 Core 语句（insert/upsert），不通过 ORM 修改对象，
    #   所以不存在这个风险。
    session.expire_all()


def is_online(device: Device, *, now_ms: int) -> bool:
    """计算设备当前是否在线（读时计算，见 ONLINE_THRESHOLD_MS 的说明）。"""
    if device.status != "online":
        return False
    if device.last_seen is None:
        return False
    return (now_ms - device.last_seen) <= ONLINE_THRESHOLD_MS


def count_devices(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(Device)).scalar() or 0)


def list_devices(session: Session, *, limit: int, offset: int) -> Sequence[Device]:
    """设备列表，按最近活跃排序。

    为什么按 last_seen 倒序而不是 device_id：
        界面上"最近在报数据的设备"更有用；而且 last_seen 可能为 NULL
        （理论上不会，但列定义允许），用 NULLS LAST 语义由 SQLite 的
        默认行为处理（NULL 排最前）——这里显式用 coalesce 归零，
        让没见过的设备排最后。
    """
    stmt = (
        select(Device)
        .order_by(func.coalesce(Device.last_seen, 0).desc(), Device.device_id)
        .limit(limit)
        .offset(offset)
    )
    return session.execute(stmt).scalars().all()


def get_device(session: Session, device_id: str) -> Device | None:
    return session.get(Device, device_id)


def get_device_stats(session: Session, device_id: str) -> dict[str, Any]:
    """单个设备的遥测统计（给 /api/devices/{id} 用）。

    注意 ``last_ts`` / ``last_seq`` 是从**库里**取的，与内存里的
    SeqTracker 无关——SeqTracker 会因为后端重启而清零，
    而这里反映的是持久化的历史。
    """
    # 只统计 unix 行：boot 行的 ts 是开机毫秒，算进 first_ts/last_ts
    # 会让"数据覆盖的时间范围"看起来只有几秒。
    stmt = select(
        func.count(Telemetry.id),
        func.min(Telemetry.ts),
        func.max(Telemetry.ts),
        func.max(Telemetry.received_at),
    ).where(Telemetry.device_id == device_id, Telemetry.clock == "unix")
    count, first_ts, last_ts, last_received = session.execute(stmt).one()

    # seq 单独取：要的是"ts 最大的那一行的 seq"，
    # 而不是"seq 的最大值"——seq 在设备重启后会回到 1，
    # 取最大值会得到重启前的旧值。
    last_seq = None
    if last_ts is not None:
        last_seq = session.execute(
            select(Telemetry.seq)
            .where(
                Telemetry.device_id == device_id,
                Telemetry.ts == last_ts,
                Telemetry.clock == "unix",
            )
            .limit(1)
        ).scalar()

    return {
        "telemetry_count": int(count or 0),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_received_at": last_received,
        "last_seq": last_seq,
    }


# ── 遥测 ────────────────────────────────────────────────────────────


def insert_telemetry(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    """批量插入遥测，返回**实际写入**的行数。

    ★ 用 ``ON CONFLICT DO NOTHING`` 处理重复。

      QoS1 是"至少一次"语义：broker 在没收到 PUBACK 时会重投，
      所以后端**必然会**收到重复报文。而唯一约束
      (device_id, ts, seq) 正好能识别它们——同一个设备的同一个采样点
      只可能有一条。于是"去重"这件事交给数据库做，
      应用层不需要维护"见过哪些消息"的状态（那个状态在重启后会丢）。

      返回值是**实际插入数**，不是输入行数。差值就是被去重的条数，
      这个数字会暴露到 /api/health 的 duplicates_skipped 里。
    """
    if not rows:
        return 0

    stmt = sqlite_insert(Telemetry).values(list(rows))
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["device_id", "ts", "seq"]
    )
    result = session.execute(stmt)
    # SQLite 的 rowcount 对多行 INSERT 返回实际影响的行数
    # （ON CONFLICT DO NOTHING 跳过的行不计入）。用 or 0 兜住 None。
    return int(result.rowcount or 0)


def get_latest_telemetry(
    session: Session, device_id: str, *, include_boot: bool = False
) -> Telemetry | None:
    """取某设备最新的一条遥测（按 ts）。

    ``include_boot=False``（默认）时只考虑 unix 行：boot 行的 ts 只有
    几千，按 ts 排序永远排在最后（最旧），所以正常情况下不会干扰结果。
    但**设备刚开机、NTP 还没同步**时库里可能只有 boot 行——
    那时按 ts 取"最新"会取到一条 ts=5000 的记录，
    它的 ts 在 1970 年，前端画图会把它画到最左边。
    默认排除掉，想看得显式要。
    """
    stmt = select(Telemetry).where(Telemetry.device_id == device_id)
    if not include_boot:
        stmt = stmt.where(Telemetry.clock == "unix")
    stmt = stmt.order_by(Telemetry.ts.desc()).limit(1)
    return session.execute(stmt).scalars().first()


def query_telemetry(
    session: Session,
    *,
    device_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    descending: bool = True,
    include_boot: bool = False,
) -> tuple[int, Sequence[Telemetry]]:
    """按条件查遥测，返回 ``(总数, 当前页)``。

    ★ ``include_boot=False`` 时查询里一定会带 ``clock='unix'``。
      这既是正确性要求（boot 行的 ts 不是绝对时间，混进来会污染
      时间范围查询），也是性能要求（部分索引只在带这个条件时才用得上）。
    """
    conditions = []
    if device_id is not None:
        conditions.append(Telemetry.device_id == device_id)
    if start_ms is not None:
        conditions.append(Telemetry.ts >= start_ms)
    if end_ms is not None:
        conditions.append(Telemetry.ts <= end_ms)
    if not include_boot:
        conditions.append(Telemetry.clock == "unix")

    total = int(
        session.execute(
            select(func.count()).select_from(Telemetry).where(*conditions)
        ).scalar()
        or 0
    )

    order = Telemetry.ts.desc() if descending else Telemetry.ts.asc()
    stmt = (
        select(Telemetry)
        # 二级排序键用 id：同一 ts 理论上不会有多条（有唯一约束），
        # 但加上它能保证分页时顺序稳定，不会出现"翻页看到重复行"。
        .order_by(order, Telemetry.id.desc() if descending else Telemetry.id.asc())
        .where(*conditions)
        .limit(limit)
        .offset(offset)
    )
    return total, session.execute(stmt).scalars().all()


def count_telemetry(session: Session) -> int:
    return int(
        session.execute(select(func.count()).select_from(Telemetry)).scalar() or 0
    )


def prune_telemetry(session: Session, *, older_than_ms: int) -> int:
    """删除旧的遥测，返回删除行数。

    ★ 两个刻意的选择：

      1. **按 received_at 删，不按 ts 删。**
         ts 来自设备时钟，可能因为设备没同步 NTP 而失真；
         received_at 是后端时钟，永远可靠。而且按 received_at 删
         正好对应"这条数据在我这儿放了多久"这个真实语义。

      2. **只删 clock='unix' 的行。**
         boot 行的 ts 只有几千，如果按 ts 判"旧"会把它们全部误删；
         而它们恰恰是排查"设备为什么没同步上时间"的唯一线索。
         这类行数量极少（每次开机只有几秒），留着没有成本。
    """
    from sqlalchemy import delete

    stmt = (
        delete(Telemetry)
        .where(Telemetry.clock == "unix", Telemetry.received_at < older_than_ms)
    )
    result = session.execute(stmt)
    return int(result.rowcount or 0)


# ── ★ 数据中断检测（回答"数据有没有丢、什么时候断的"）───────────────


def find_receive_gaps(
    session: Session,
    *,
    device_id: str | None = None,
    min_gap_ms: int = 30_000,
    since_ms: int | None = None,
    limit: int = 50,
) -> Sequence[dict[str, Any]]:
    """找出**入库时间**上的断层。

    这是 ``received_at`` 这一列最核心的用途。

    ── 为什么必须用它，而不是用 ts 或行数 ──────────────────────────

        实测踩到过：设备连续发布 41 分钟后**彻底停发**，持续 2.5 小时，
        而它本地一切正常（MQTT 连接在、队列零丢弃、串口照常打印）。
        如果只按 ts 看，那段时间就是"没有行"——你无法区分：

            设备没采（传感器坏了）
            采了没发出来（固件/网络问题）

        更看不出它**停了多久**。而按 received_at 查相邻两行的差值，
        一个 2.5 小时的断层一眼就能看见，还能精确定位到从哪一条开始停的。

    ── 为什么用窗口函数 LAG ────────────────────────────────────────

        "相邻两行的差"这件事，在应用层做需要把整段时间的数据全拉出来
        逐条比。用 SQL 的 ``LAG(received_at) OVER (PARTITION BY device_id
        ORDER BY received_at)`` 则只把**有断层的那些行**返回回来——
        正常情况下这个查询返回空集，开销可以忽略。

        SQLite 从 3.25 起支持窗口函数，Python 3.12 内置的 sqlite3
        版本远高于此（实测 3.4x）。

    :param min_gap_ms: 多大的间隔算"断层"。默认 30 秒 = 正常 2 秒
                       采样节奏下连续丢 15 条。设太小会把正常的
                       网络抖动全报出来。
    :param since_ms:   只查这个时刻之后的（按 received_at）。
    :return: 每项形如
             ``{"device_id": ..., "gap_start_ms": ..., "gap_end_ms": ...,
                "gap_ms": ..., "missed_estimate": ...}``
    """
    prev_received = func.lag(Telemetry.received_at).over(
        partition_by=Telemetry.device_id,
        order_by=Telemetry.received_at,
    )

    inner_conditions = []
    if device_id is not None:
        inner_conditions.append(Telemetry.device_id == device_id)
    if since_ms is not None:
        inner_conditions.append(Telemetry.received_at >= since_ms)

    sub = (
        select(
            Telemetry.device_id.label("device_id"),
            Telemetry.received_at.label("gap_end_ms"),
            prev_received.label("gap_start_ms"),
        )
        .where(*inner_conditions)
        .subquery()
    )

    gap_ms = sub.c.gap_end_ms - sub.c.gap_start_ms
    stmt = (
        select(
            sub.c.device_id,
            sub.c.gap_start_ms,
            sub.c.gap_end_ms,
            gap_ms.label("gap_ms"),
        )
        .where(sub.c.gap_start_ms.is_not(None), gap_ms >= min_gap_ms)
        .order_by(sub.c.gap_end_ms.desc())
        .limit(limit)
    )

    rows = session.execute(stmt).all()
    return [
        {
            "device_id": r.device_id,
            "gap_start_ms": r.gap_start_ms,
            "gap_end_ms": r.gap_end_ms,
            "gap_ms": r.gap_ms,
            # 按 2 秒采样周期估算这段时间本该有多少条没进来。
            # 只是**估算**——设备的实际采样周期是可配的（Kconfig），
            # 而且断层期间设备可能也在丢数据。
            "missed_estimate": max(0, int(r.gap_ms // 2000) - 1),
        }
        for r in rows
    ]


def last_received_at(session: Session, device_id: str | None = None) -> int | None:
    """最后一次收到报文的时间（后端时钟）。

    /api/health 用它算"数据流有多久没动了"——这是判断
    "后端还在正常收数据吗"的最直接指标。
    """
    stmt = select(func.max(Telemetry.received_at))
    if device_id is not None:
        stmt = stmt.where(Telemetry.device_id == device_id)
    return session.execute(stmt).scalar()


# ── 异常与预测（本轮只读，表是空的）─────────────────────────────────


def query_anomalies(
    session: Session,
    *,
    device_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    metric: str | None = None,
    level: str | None = None,
    min_score: float | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[int, Sequence[Anomaly]]:
    """查异常记录。本轮表是空的，返回 ``(0, [])``。

    参数和模型现在就定好，是因为 Phase 8 写数据时接口不该再变——
    前端可以先把页面搭起来。
    """
    conditions = []
    if device_id is not None:
        conditions.append(Anomaly.device_id == device_id)
    if start_ms is not None:
        conditions.append(Anomaly.ts >= start_ms)
    if end_ms is not None:
        conditions.append(Anomaly.ts <= end_ms)
    if metric is not None:
        conditions.append(Anomaly.metric == metric)
    if level is not None:
        conditions.append(Anomaly.level == level)
    if min_score is not None:
        conditions.append(Anomaly.score >= min_score)

    total = int(
        session.execute(
            select(func.count()).select_from(Anomaly).where(*conditions)
        ).scalar()
        or 0
    )
    stmt = (
        select(Anomaly)
        .where(*conditions)
        .order_by(Anomaly.ts.desc(), Anomaly.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return total, session.execute(stmt).scalars().all()


def query_predictions(
    session: Session,
    *,
    device_id: str | None = None,
    metric: str | None = None,
    horizon: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[int, Sequence[Prediction]]:
    """查预测记录。本轮表是空的，返回 ``(0, [])``。"""
    conditions = []
    if device_id is not None:
        conditions.append(Prediction.device_id == device_id)
    if metric is not None:
        conditions.append(Prediction.metric == metric)
    if horizon is not None:
        conditions.append(Prediction.horizon == horizon)
    if start_ms is not None:
        conditions.append(Prediction.ts >= start_ms)
    if end_ms is not None:
        conditions.append(Prediction.ts <= end_ms)

    total = int(
        session.execute(
            select(func.count()).select_from(Prediction).where(*conditions)
        ).scalar()
        or 0
    )
    stmt = (
        select(Prediction)
        .where(*conditions)
        .order_by(Prediction.ts.desc(), Prediction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return total, session.execute(stmt).scalars().all()
