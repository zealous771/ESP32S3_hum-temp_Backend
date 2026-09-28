"""保留策略：定期删除过期遥测，防止数据库无限增长。

★ 为什么必须要有它，而不是"数据留着就留着"：

    设备每 2 秒一条 = 每天 43200 行 = 每年约 1580 万行。
    按每行约 100 字节算，一年约 1.5 GB。SQLite 单文件到这个量级
    不会崩，但会带来三个实实在在的退化：

      1. 库越大，checkpoint 越慢（WAL 要把页刷回主库）
      2. 时间范围查询虽然走索引，但索引本身也变大、缓存命中率下降
      3. 备份、拷贝、迁移全变成"搬一个 GB 级文件"

    而本工程真正需要的历史数据只有几周——AI 阶段（文档 §26）
    也是"积累几天到几周"就够训练了。所以"留多久"是个必须显式
    回答的问题，默认值是"不清理"只是因为开发期数据少。

★ 为什么跑在独立线程，而不是挂在写库线程或 API 请求里：

    挂在写库线程：删除是几秒级的 IO 操作，期间遥测入库全停。
                  数据流会被"清理"这件事周期性打断。
    挂在 API 请求里：谁来触发？某个 GET 顺手删数据是个很坏的先例——
                    读接口产生副作用，而且并发请求会同时删。
    定时器线程：与数据流完全解耦，代价是多一个线程（约 30 行代码）。

★ 这个线程的失败模式和对策，与写库线程完全一致：

    循环体必须包在 try/except 里。一次未捕获的异常会让线程退出，
    之后**永远不再清理**——而"清理没在跑"这件事没有任何外部症状，
    直到某天发现磁盘满了。所以吞异常、打日志、继续循环。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.clock import now_ms
from app.config import Settings
from app.db import repository as repo
from app.db.database import Database
from app.stats import RetentionStats

logger = logging.getLogger(__name__)

# 一天有多少毫秒。写成常量而不是每次算，是为了让"天 → 毫秒"这个
# 换算在代码里只出现一次。
MS_PER_DAY = 86_400_000

# stop() 等待线程退出的上限。
# 取 5 秒：清理一批通常远快于此。超时不会强杀线程（Python 没有
# 安全的强杀手段），只是记一条告警——线程是 daemon，进程能正常退出。
_STOP_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class PruneOutcome:
    """一次清理的结果。给测试断言和日志用。"""

    cutoff_ms: int      # received_at 早于它的被删
    deleted: int        # 实际删除行数
    chunks: int         # 删了几批（用来确认分块逻辑真的在工作）
    duration_ms: int    # 耗时
    at_ms: int          # 结束时刻（后端时钟）


class RetentionService:
    """定期删除过期遥测。

    生命周期：``start()`` 起线程，``stop()`` 收尾。与 TelemetryIngestor 同构。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        stats: RetentionStats | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self.stats = stats if stats is not None else RetentionStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 配置 ───────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """保留策略是否启用。

        ``telemetry_retention_days == 0`` 表示**不清理**（见 config.py）。
        这不是错误状态，是开发阶段的正常配置，所以走 INFO 不走 WARNING。
        """
        return self._settings.telemetry_retention_days > 0

    def cutoff_ms(self, *, now: int) -> int:
        """算出"过期"的分界时刻。"""
        return now - self._settings.telemetry_retention_days * MS_PER_DAY

    # ── 生命周期 ───────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.enabled:
            logger.info(
                "保留策略未启用（TELEMETRY_RETENTION_DAYS=0）：不会自动删除任何数据"
            )
            return

        self._thread = threading.Thread(
            target=self._run, name="retention", daemon=True
        )
        self._thread.start()
        logger.info(
            "保留策略已启动：保留 %d 天，每 %d 秒检查一次，每批最多删 %d 行",
            self._settings.telemetry_retention_days,
            self._settings.retention_prune_interval_s,
            self._settings.retention_chunk_size,
        )

    def stop(self, *, timeout_s: float = _STOP_TIMEOUT_S) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            logger.warning(
                "保留策略线程在 %.1f 秒内没退出（可能正在删一大批数据）。"
                "它是 daemon 线程，不会阻止进程退出",
                timeout_s,
            )
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── 线程 ───────────────────────────────────────────────────

    def _run(self) -> None:
        """定时循环。

        ★ 用 ``self._stop.wait(interval)`` 而不是 ``time.sleep(interval)``：

            sleep 的问题是 stop() 之后还要干等一整个周期才退出——
            测试里就变成"每个用例多等 1 秒"，实际部署时关机要多等 1 小时。
            Event.wait 会在 stop 被 set 的瞬间返回。

          返回值语义容易记反，所以写成 ``while not wait(...)``：
          wait 返回 True = 收到停止信号 = 退出循环。
        """
        # 启动时先清一次。进程上次退出到现在的这段时间没人清理，
        # 而且这时候库里可能有积压——早清早释放空间。
        self._safe_run_once()

        while not self._stop.wait(self._settings.retention_prune_interval_s):
            self._safe_run_once()

    def _safe_run_once(self) -> None:
        """带异常兜底的清理。见文件头关于"线程退出"的说明。"""
        try:
            self.run_once()
        except Exception:  # noqa: BLE001
            logger.exception(
                "保留策略清理失败——已吞掉异常继续运行，"
                "否则线程退出会导致之后永远不再清理（且没有任何外部症状）"
            )

    # ── 核心：可测的清理逻辑 ───────────────────────────────────

    def run_once(self, *, now: int | None = None) -> PruneOutcome | None:
        """执行一次清理。**这是测试的主要入口。**

        :param now: 覆盖"现在"（测试用，避免依赖真实时钟）
        :return: 清理结果；未启用时返回 None
        """
        if not self.enabled:
            return None

        now = now_ms() if now is None else now
        cutoff = self.cutoff_ms(now=now)
        chunk = self._settings.retention_chunk_size
        started = time.monotonic()

        deleted = 0
        chunks = 0
        while True:
            # ★ 每批一个独立的 Session（也就是一次独立事务、一次提交）。
            #   分块的意义全在这里——放在同一个事务里就退化成"一次删干净"了。
            with self._db.session() as session:
                n = repo.delete_old_telemetry_chunk(
                    session, older_than_ms=cutoff, limit=chunk
                )
            chunks += 1
            deleted += n
            # n < chunk 说明这一批没删满，也就是已经没有符合条件的行了。
            if n < chunk:
                break

        duration_ms = int((time.monotonic() - started) * 1000)

        self.stats.runs += 1
        self.stats.deleted_total += deleted
        self.stats.last_deleted = deleted
        self.stats.last_cutoff_ms = cutoff
        self.stats.last_run_ms = now
        self.stats.last_duration_ms = duration_ms

        if deleted:
            logger.info(
                "已清理过期遥测 %d 行（%d 批，耗时 %d ms，截止 %d）",
                deleted,
                chunks,
                duration_ms,
                cutoff,
            )
        else:
            # 没删到东西是常态（每小时检查一次，而保留期是几天），
            # 用 DEBUG 免得刷日志。
            logger.debug("保留策略检查完成：无过期数据（耗时 %d ms）", duration_ms)

        return PruneOutcome(
            cutoff_ms=cutoff,
            deleted=deleted,
            chunks=chunks,
            duration_ms=duration_ms,
            at_ms=now,
        )
