"""业务编排：把 MQTT 报文变成数据库行。

★ 这一层是整个后端里**唯一可测的核心**。

    数据流被切成两半：

        paho 网络线程 ──▶ 队列 ──▶ 写库线程 ──▶ TelemetryIngestor.handle_batch()
                                                      ▲
                                                      └── 测试直接调这里

    ``handle_batch()`` 只接收"一批已校验的消息 + 一个 Session"，
    不认识 paho、不认识 broker、不认识 FastAPI。所以它可以被
    完整地单元测试——不需要起 broker、不需要真设备、不需要网络。
    这是把"传输"和"业务"分开的最大收益。

为什么回调不直接写库、非要经过队列 + 独立线程：

    paho 的网络线程同时负责收报文、发 PINGREQ（keepalive）、回 PUBACK。
    在里面做一次 SQLite 写入（一次 fsync 是几毫秒到几十毫秒），
    这期间它没法回 PINGREQ，broker 会在 keepalive×1.5 之后判定本端已死
    并断开 → 重连 → 重投 → 雪崩。

    所以回调只做"解析 + 入队"（微秒级）。代价是队列满时会丢数据，
    所以丢弃计数暴露在 /api/health 里，让"丢"这件事可见。

    ★ 一个副作用是好的：积压的消息会**批量**入库。
      每条一次事务的话，2 秒一条、每条一次 fsync；批量提交后
      fsync 次数降一个数量级。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Sequence

from sqlalchemy.orm import Session

from app.clock import now_ms
from app.config import Settings
from app.db import repository as repo
from app.db.database import Database
from app.logging_conf import RateLimitedLogger
from app.mqtt.consumer import InboundMessage
from app.mqtt.schemas import TOPIC_KIND_STATUS
from app.stats import IngestStats, SeqTracker, SeqVerdict

logger = logging.getLogger(__name__)

# 写库线程每次最多连续取多少条（受 settings.ingest_batch_size 限制）。
# 从队列里"能取多少取多少"，不等待——因为等只会推迟落库，
# 不会让批量更大（采样是 2 秒一条，本来就没什么可等的）。
_QUEUE_POLL_TIMEOUT_S = 1.0

# 队列满的告警限速。队列满意味着写库跟不上，是持续状态。
_QUEUE_FULL_LOG_EVERY = 100


class TelemetryIngestor:
    """把入站消息写进数据库。

    生命周期：``start()`` 起写库线程，``stop()`` 优雅收尾。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        stats: IngestStats | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        # stats 由外部注入时，MQTT 消费者和入库层记的是**同一个对象**，
        # /api/health 只读一处。见 app/mqtt/consumer.py 的说明。
        self.stats = stats if stats is not None else IngestStats()

        self._seq_tracker = SeqTracker()
        self._queue: queue.Queue[InboundMessage] = queue.Queue(
            maxsize=settings.ingest_queue_max
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._queue_full = RateLimitedLogger(
            logger, every=_QUEUE_FULL_LOG_EVERY
        )

    # ── 生命周期 ───────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="db-writer", daemon=True
        )
        self._thread.start()
        logger.info(
            "写库线程已启动（队列上限 %d，批量 %d）",
            self._settings.ingest_queue_max,
            self._settings.ingest_batch_size,
        )

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """停止写库线程，尽量把队列里剩的写完。"""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            logger.warning("写库线程在 %.1f 秒内没退出，可能有事务卡住", timeout_s)
        else:
            logger.info("写库线程已停止，队列剩余 %d 条", self._queue.qsize())
        self._thread = None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        """写库线程是否还活着。

        为什么要暴露它：这个线程**退出后没有任何外部症状**——
        MQTT 连接正常、/api/health 的 connected 是 true、队列只进不出，
        但数据不再落库。有了这个属性，健康检查或测试就能直接问
        "它还在吗"，而不是靠"行数没涨"去反推。

        （与 RetentionService.running 同形，两个后台线程的观测方式一致。）
        """
        return self._thread is not None and self._thread.is_alive()

    def seq_snapshot(self) -> dict[str, object]:
        """seq 跟踪的明细，给 /api/health 用。

        里面是每台设备的 last_seq、累计缺口、重启次数。
        /api/health 里的 ``ingest.seq_gap_events`` 是**汇总数**，
        这里给的是**按设备分的明细**——多设备场景下要知道是哪台在丢。
        """
        return self._seq_tracker.snapshot()

    # ── MQTT 回调调用（必须极快返回）────────────────────────────

    def submit(self, item: InboundMessage) -> None:
        """把一条消息放进写库队列。**绝不阻塞。**

        为什么用 ``put_nowait`` 而不是 ``put``：

            回调跑在 paho 的网络线程上。队列满时阻塞等待，
            就等于把 paho 的网络线程交给写库线程的进度决定——
            写库慢一点，PINGREQ 就晚发一点，最后 broker 判定本端已死。

            宁可丢数据也不能阻塞回调。丢了多少会计数并暴露到 /api/health，
            而阻塞的后果（连接被踢、重投风暴）比丢数据严重得多。
        """
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.stats.queue_dropped += 1
            self._queue_full.hit(
                "写库队列已满（上限 %d），本条丢弃", self._settings.ingest_queue_max
            )
            return

        # 记录队列历史最高水位。它持续贴着上限就说明写库跟不上，
        # 而 queue_depth 是瞬时值，看不出"经常接近满"。
        depth = self._queue.qsize()
        if depth > self.stats.queue_high_watermark:
            self.stats.queue_high_watermark = depth

    # ── 写库线程 ───────────────────────────────────────────────

    def _run(self) -> None:
        """写库线程主循环。

        ★ 整个循环体必须包在 try/except 里。

          一次未捕获的异常会让这个线程**退出**，之后队列只进不出、
          迅速填满、所有数据被丢弃——而 MQTT 连接看起来还是好的，
          /api/health 里的 connected 也是 true。这是最难查的故障形态：
          所有"健康指标"都正常，只有数据不见了。

          所以：吞掉异常、打日志、继续循环。丢掉的是这一批，
          不是之后的所有数据。
        """
        while not self._stop.is_set():
            try:
                batch = self._drain_batch()
                if not batch:
                    continue
                with self._db.session() as session:
                    self.handle_batch(session, batch)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "写库线程处理一批数据时失败——已吞掉异常继续运行，"
                    "否则线程退出会导致之后所有数据静默丢失"
                )
                # 防止"一直失败一直重试"把日志刷爆
                time.sleep(0.5)

    def _drain_batch(self) -> list[InboundMessage]:
        """取一批消息。阻塞等第一条，之后的能取多少取多少。"""
        batch: list[InboundMessage] = []
        try:
            batch.append(self._queue.get(timeout=_QUEUE_POLL_TIMEOUT_S))
        except queue.Empty:
            return batch

        limit = self._settings.ingest_batch_size
        while len(batch) < limit:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    # ── 核心：可测的业务逻辑 ───────────────────────────────────

    def handle_batch(
        self, session: Session, items: Sequence[InboundMessage]
    ) -> dict[str, int]:
        """把一批消息写进数据库。**这是测试的主要入口。**

        不做事务控制、不碰队列、不认识 paho——调用方给什么就处理什么。

        :return: 本次的计数摘要（便于测试断言和日志）
        """
        telemetry_rows: list[dict[str, object]] = []
        # 本批里每个设备最后一次出现的时间（后端时钟）。
        # 一个设备在同一批里可能有多条，只需 upsert 一次——
        # 但必须用**最后一条**的时间，否则 last_seen 会倒退。
        device_seen: dict[str, int] = {}
        device_status: dict[str, str] = {}

        for item in items:
            # 任何报文都算"这个设备还活着"（见下面 upsert 的说明）
            device_seen[item.device_id] = max(
                device_seen.get(item.device_id, 0), item.received_ms
            )

            if item.kind == TOPIC_KIND_STATUS:
                assert item.status is not None
                device_status[item.device_id] = item.status.state
                continue

            msg = item.telemetry
            assert msg is not None

            # 时间基准在 consumer 里已经校验过自洽性，这里再解析一次
            clock = msg.resolved_clock(now_ms=now_ms())
            if clock is None:
                # 理论上到不了（consumer 已拒），但既然返回类型允许 None
                # 就必须判——静默把 None 写进库会让 clock 列出现脏值。
                self.stats.invalid_messages += 1
                continue

            # ── seq 跟踪 ──────────────────────────────────────
            verdict = self._seq_tracker.observe(msg.device_id, msg.seq)
            if verdict is SeqVerdict.GAP:
                self.stats.seq_gap_events += 1
                missing = self._seq_tracker.missing_total()
                self.stats.seq_missing_messages = missing
                logger.warning(
                    "检测到跳号：device=%s seq=%d（上一次 %s），累计缺口 %d 条",
                    msg.device_id,
                    msg.seq,
                    self._seq_tracker.last_seq(msg.device_id),
                    missing,
                )

            # ── boot 行的取舍 ──────────────────────────────────
            if clock == "boot" and not self._settings.persist_boot_rows:
                # 只在明确配置为"不存"时才走这里。默认是存的——
                # 因为如果设备侧 NTP 没配（受支持的用法），
                # 丢弃 boot 行会让数据库一直是空的，而系统看起来一切正常。
                self.stats.boot_rows_dropped += 1
                continue

            telemetry_rows.append(
                {
                    "device_id": msg.device_id,
                    "ts": msg.ts,
                    # ★ 后端时钟。见 app/clock.py —— 这一列是
                    # "发现静默停发"的唯一手段。
                    "received_at": item.received_ms,
                    "temperature": float(msg.temperature_c),
                    "humidity": float(msg.humidity_rh),
                    "rssi": msg.rssi,
                    "seq": msg.seq,
                    "clock": clock,
                }
            )

        # ── 设备台账 ───────────────────────────────────────────
        #
        # ★ 对**所有**出现过的设备都 upsert，包括"遥测被丢弃"的情况。
        #
        #   丢遥测 ≠ 丢存活信号。如果只在成功入库遥测时才刷新设备，
        #   那么一台 NTP 没配的设备（所有报文都是 boot、都被丢）会在
        #   /api/devices 里"从未出现过"——而它其实一直在稳定上报。
        #   那会让人以为是设备的问题，实际是后端的统计口径错了。
        for device_id, seen_ms in device_seen.items():
            repo.upsert_device(
                session,
                device_id,
                seen_ms=seen_ms,
                status=device_status.get(device_id),
            )

        # ── 遥测入库 ───────────────────────────────────────────
        inserted = 0
        if telemetry_rows:
            inserted = repo.insert_telemetry(session, telemetry_rows)
            duplicates = len(telemetry_rows) - inserted
            self.stats.persisted += inserted
            self.stats.duplicates_skipped += duplicates
            self.stats.boot_rows_persisted += sum(
                1 for r in telemetry_rows if r["clock"] == "boot"
            )

        return {
            "received": len(items),
            "candidates": len(telemetry_rows),
            "inserted": inserted,
            "duplicates": len(telemetry_rows) - inserted,
        }
