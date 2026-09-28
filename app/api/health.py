"""``GET /api/health`` —— 健康检查与运行状态。

★ 这个接口的 HTTP 状态码**恒为 200**，即使 MQTT 断了。

    为什么：它同时被两拨人用——

      1. Docker 的 HEALTHCHECK（决定容器是否算健康、要不要重启）
      2. 人（打开浏览器看"现在什么情况"）

    对第 2 拨人来说，MQTT 断开是**可恢复的正常状态**（broker 重启、
    网络抖动都会导致），不是"服务坏了"。如果返回 503，
    Docker 会把容器判成不健康并重启它——而重启一个本来正常的进程
    解决不了 broker 的问题，只会让情况更糟（还会丢掉写库队列里的数据）。

    "MQTT 断开"这件事通过响应体里的 ``mqtt.connected`` 和
    ``data.stale`` 表达，而不是靠状态码。

★ ``data.stale`` 是专门为"设备静默停发"设计的。

    实测踩过：设备连续发布 41 分钟后彻底停发、持续 2.5 小时，
    而它本地一切正常（MQTT 连接在、队列零丢弃、串口照常打印）。
    从后端看只是"没有新数据"，和"设备没采"长得一模一样。

    ``data`` 这一段就是回答这个问题的：
      last_received_at  最后一条数据是什么时候到的（后端时钟）
      age_ms            距今多久
      stale             age 是否超过阈值
      recent_gaps       最近发生过哪些入库断层（见 repository.find_receive_gaps）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.api.deps import DbSession
from app.clock import now_ms
from app.db import repository as repo

router = APIRouter(tags=["health"])

# 数据流"静默"的判定阈值：这么久没收到任何数据就算停发。
#
# 取 30 秒的依据：设备每 2 秒发一条，30 秒 = 连续丢 15 条。
# 正常的网络抖动不会丢这么多。与 repository.ONLINE_THRESHOLD_MS 同量级
# 但**语义不同**——那个判"设备在不在线"，这个判"后端还在不在收数据"。
# 两者分开是因为：MQTT 断了但设备正常时，前者仍可能是 true。
STALE_THRESHOLD_MS = 30_000

# /api/health 里最多列出几个最近断层。
# 列全部可能很长（设备反复重连会攒很多），而看的人只关心最近几次。
MAX_GAPS_IN_HEALTH = 5


@router.get("/api/health", summary="健康检查与运行状态")
def health(request: Request, db: DbSession) -> dict[str, Any]:
    """一次返回所有能用来判断"现在正常吗"的信息。

    分成四段，对应四个不同的故障域：

        mqtt     连上 broker 了吗、收了多少、断了几次
        ingest   收到的数据进了库吗、丢了多少、队列堵不堵
        db       库文件多大、WAL 生效吗、有多少行
        data     ★ 数据流还活着吗（最后一条多久之前、有没有断层）

    排查时按这个顺序看：mqtt.connected → ingest.persisted →
    db.telemetry_rows → data.age_ms。哪一段断了，问题就在哪一段。
    """
    now = now_ms()

    # ── MQTT ───────────────────────────────────────────────────
    consumer = getattr(request.app.state, "consumer", None)
    if consumer is not None:
        mqtt_info: dict[str, Any] = consumer.snapshot()
    else:
        # mqtt_enabled=False（测试）或初始化被跳过
        mqtt_info = {"enabled": False, "connected": False, "reason": "未启用或未初始化"}

    # ── 入库 ───────────────────────────────────────────────────
    ingestor = getattr(request.app.state, "ingestor", None)
    if ingestor is not None:
        ingest_info: dict[str, Any] = dict(ingestor.stats.snapshot())
        ingest_info["queue_depth"] = ingestor.queue_depth
        ingest_info["seq_detail"] = ingestor.seq_snapshot()
    else:
        ingest_info = {"enabled": False}

    # ── 数据库 ─────────────────────────────────────────────────
    settings = request.app.state.settings
    journal_mode = db.execute(text("PRAGMA journal_mode")).scalar()
    db_path = settings.sqlite_path
    db_info: dict[str, Any] = {
        "url": settings.database_url,
        "path": str(db_path) if db_path else None,
        "size_bytes": db_path.stat().st_size if db_path and db_path.exists() else None,
        "journal_mode": journal_mode,
        "telemetry_rows": repo.count_telemetry(db),
        "devices": repo.count_devices(db),
    }

    # ── ★ 数据流活性 ───────────────────────────────────────────
    last_received = repo.last_received_at(db)
    age_ms = (now - last_received) if last_received is not None else None
    gaps = repo.find_receive_gaps(db, min_gap_ms=STALE_THRESHOLD_MS, limit=MAX_GAPS_IN_HEALTH)

    data_info: dict[str, Any] = {
        "last_received_at": last_received,
        "age_ms": age_ms,
        "stale": (age_ms is None) or (age_ms > STALE_THRESHOLD_MS),
        "stale_threshold_ms": STALE_THRESHOLD_MS,
        "recent_gaps": gaps,
    }

    return {
        "status": "ok",
        "now_ms": now,
        "mqtt": mqtt_info,
        "ingest": ingest_info,
        "db": db_info,
        "data": data_info,
        "settings": {
            "broker": settings.mqtt_broker_repr,
            "client_id": settings.mqtt_client_id,
            "subscriptions": settings.subscriptions,
            "persist_boot_rows": settings.persist_boot_rows,
            "telemetry_retention_days": settings.telemetry_retention_days,
        },
    }
