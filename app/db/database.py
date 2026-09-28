"""数据库连接、PRAGMA 设置、建表。

★ 本文件里最重要的一件事：**WAL 模式必须被验证，不能假设它生效了。**

    SQLite 的 ``PRAGMA journal_mode=WAL`` 在有些文件系统上会**静默失败**
    ——它不报错，只是返回 ``delete``（默认模式）而不是 ``wal``。
    最典型的是 Windows 的 DrvFs（也就是 WSL 里的 /mnt/c）：
    WAL 依赖 ``-shm`` 文件的共享内存映射，而 9p 文件系统对 mmap 的支持不完整。

    症状是"明明没有并发却报 database is locked"，而且**开发早期不出现**
    ——等你把数据看得比较重了才炸，那时排查成本很高。

    所以 ``build_engine()`` 在返回前会真的查一次 ``PRAGMA journal_mode``，
    不是 wal 就直接抛异常。**宁可起不来，也不要带着错误的假设跑起来。**

为什么 SQLite 要 WAL：

    默认的 ``delete`` 模式下，写事务会阻塞读事务。本工程的写入来自
    独立的写库线程（2 秒一条），读取来自 FastAPI 的线程池
    （浏览器可能同时开几个页面）。用默认模式就会出现
    "查历史数据时写入被卡住"或反之。

    WAL（Write-Ahead Logging）让读和写可以真正并行：写只追加到
    ``-wal`` 文件，读则同时看主库和 ``-wal``。这是"一写多读"唯一
    可用的模式。

为什么 ``synchronous=NORMAL`` 而不是 FULL：

    FULL 每次提交都 fsync，保证断电不丢**已提交**的事务；
    NORMAL 只在 checkpoint 时 fsync，断电可能丢掉最后几个事务。

    对本工程来说，丢掉最后几秒的遥测数据是可以接受的（设备每 2 秒
    就发下一条），而 FULL 会让每次批量提交都等一次磁盘同步——
    在 2 秒一条的节奏下这是不必要的开销。**这是一个有意的取舍，
    不是偷懒。** 如果哪天数据珍贵到不能丢，改回 FULL 即可。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Base

logger = logging.getLogger(__name__)

# schema 版本号。写在 SQLite 的 PRAGMA user_version 里。
#
# 为什么要它：模型改了而库还是旧的时，报错会是 "no such column: xxx"，
# 看起来像代码 bug。有了版本号，启动时就能明确告诉你
# "库是 v1、代码要 v2，请删掉重建"。
#
# 本轮不引入 Alembic（迁移工具）：开发期数据可以随时丢，
# 改 schema 就删库重来。等开始积累不想丢的数据时（Phase 8 之前）再上。
SCHEMA_VERSION = 1

# SQLite 的忙等超时。
#
# 为什么需要：WAL 模式下写与读可以并行，但**写与写仍然互斥**。
# 本工程只有一个写线程，理论上不会撞；但 FastAPI 的保留策略清理
# 也会写（DELETE），那时就可能撞上。设 15 秒的忙等，
# 让后到的写等待而不是立刻报 "database is locked"。
BUSY_TIMEOUT_MS = 15_000


class Database:
    """engine 与 sessionmaker 的持有者。

    为什么包成一个类而不是两个模块级全局变量：

        测试需要同时存在多个独立数据库（每个测试一个临时文件），
        而模块级全局只能有一个。包成类之后，测试里
        ``Database(Settings(database_url=...))`` 就能各建各的。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.engine = self._build_engine(settings)
        self._session_factory = sessionmaker(
            bind=self.engine,
            # expire_on_commit=False：默认情况下 commit() 会让所有 ORM 对象
            # 过期，之后访问任何属性都会再发一次 SELECT。我们的写库线程
            # 提交完就不再碰那些对象，但 API 层会——关掉它能省掉
            # 一大批"提交后立刻又查一遍"的往返。
            expire_on_commit=False,
            # autoflush=False：默认情况下任何查询前都会先 flush 挂起的改动，
            # 这会在只读查询里意外触发写入（甚至触发约束错误）。
            # 我们显式控制 flush 时机。
            autoflush=False,
        )

    # ── 构造 ───────────────────────────────────────────────────

    @staticmethod
    def _build_engine(settings: Settings) -> Engine:
        url = settings.database_url
        is_memory = ":memory:" in url or "mode=memory" in url

        if not is_memory:
            # SQLite 不会自动创建目录。不建的话报 "unable to open database file"，
            # 看起来像权限问题，其实是目录不存在。
            path = settings.sqlite_path
            if path is not None and path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                logger.info("已创建数据库目录：%s", path.parent)

        engine = create_engine(
            url,
            # ★ check_same_thread=False 是必需的。
            #
            #   SQLite 的 Python 绑定默认禁止跨线程使用同一个连接。
            #   而本工程的连接来自连接池，写库线程和 API 线程会**复用**
            #   同一批连接对象。不开这个开关，第二个线程一用就报
            #   "SQLite objects created in a thread can only be used in that same thread"。
            #
            #   关掉它是安全的，因为：SQLite 本身在串行化模式下是线程安全的
            #   （Python 的 sqlite3 默认 threadsafety=3 / SERIALIZED），
            #   而我们已经把写操作收敛到单个写线程，读走连接池的独立连接。
            connect_args={"check_same_thread": False},
            # 连接池：默认的 QueuePool 对 SQLite 够用。
            # pool_pre_ping 会在取连接时先探活，避免拿到已被服务端关掉的连接。
            pool_pre_ping=True,
        )

        Database._install_pragmas(engine, is_memory=is_memory)
        return engine

    @staticmethod
    def _install_pragmas(engine: Engine, *, is_memory: bool) -> None:
        """在每条新连接上设置 PRAGMA。

        为什么用事件监听而不是在建 engine 后执行一次：

            PRAGMA 是**连接级**的（journal_mode 例外，它写在库文件头里，
            是库级的）。连接池会不断创建新连接，每个新连接都要重新设。
            监听 "connect" 事件是唯一能覆盖所有连接的做法。
        """

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                # WAL：读写在真正意义上并行。见文件头。
                cursor.execute("PRAGMA journal_mode=WAL")
                # 见文件头关于 FULL / NORMAL 的取舍说明。
                cursor.execute("PRAGMA synchronous=NORMAL")
                # 外键约束默认是**关闭**的！不开的话 telemetry.device_id
                # 指向不存在的设备也能插进去，ON DELETE CASCADE 也不会生效。
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()

        if is_memory:
            # 内存库不支持 WAL（PRAGMA 会返回 "memory"），跳过校验。
            logger.warning("使用内存数据库：WAL 不可用，仅供测试")
            return

        # ★ 真的查一次，确认 WAL 生效了。见文件头。
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        if str(mode).lower() != "wal":
            raise RuntimeError(
                f"SQLite 未能启用 WAL 模式（实际为 {mode!r}）。"
                f"当前数据库 URL 是 {engine.url}。"
                "最可能的原因：数据库文件放在 /mnt/c 这类 DrvFs 上——"
                "WAL 依赖 -shm 的共享内存映射，而 9p 文件系统支持不完整。"
                "请把 DATABASE_URL 指向 WSL 自己的文件系统（如 "
                "/home/ubuntu/iot-data/iot.db）。"
            )
        logger.info("数据库已启用 WAL 模式：%s", engine.url)

    # ── 生命周期 ───────────────────────────────────────────────

    def init_schema(self) -> None:
        """建表，并校验 schema 版本。"""
        with self.engine.connect() as conn:
            current = conn.execute(text("PRAGMA user_version")).scalar() or 0

        if current == 0:
            # 全新库：建表并打上版本号
            Base.metadata.create_all(self.engine)
            with self.engine.begin() as conn:
                # 注意 PRAGMA 不支持参数绑定，只能拼字符串。
                # 这里拼的是模块内常量，不是外部输入，没有注入风险。
                conn.execute(text(f"PRAGMA user_version={SCHEMA_VERSION}"))
            logger.info(
                "已建表（schema v%d）：%s",
                SCHEMA_VERSION,
                ", ".join(sorted(Base.metadata.tables)),
            )
            return

        if current != SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本不匹配：库是 v{current}，代码需要 v{SCHEMA_VERSION}。"
                f"开发期请删掉数据库文件重建（{self._settings.sqlite_path}）。"
                "本轮没有引入迁移工具（Alembic），改 schema 就是删库重来。"
            )

        logger.info("schema v%d 已是最新", current)

    def dispose(self) -> None:
        self.engine.dispose()

    # ── 会话 ───────────────────────────────────────────────────

    @contextmanager
    def session(self) -> Iterator[Session]:
        """一个事务性的 Session。

        用法::

            with db.session() as s:
                repo.insert_telemetry(s, rows)

        正常退出时 commit，抛异常时 rollback 并**继续抛出**——
        不吞异常。吞掉的话上层会以为写成功了，而数据其实没进库。
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def open_session(self) -> Session:
        """给 FastAPI 依赖注入用的裸 Session（由调用方负责关闭）。

        API 层用 ``Depends(get_db)`` 拿它，请求结束时关闭。
        不用上面的 contextmanager，因为 FastAPI 的依赖生命周期
        和 with 块不是一回事。
        """
        return self._session_factory()
