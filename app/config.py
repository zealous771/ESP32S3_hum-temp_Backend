"""配置管理：从环境变量 / .env 读取配置，并在**启动时**完成类型校验。

为什么用 pydantic-settings，而不是手写 ``os.environ.get()``：

    手写的话，配置项名字拼错（``MQTT_HOTS``）、类型不对（端口填成 "abc"）、
    必填项缺失，全都要等到**真正用到它的那一刻**才炸——而那一刻可能
    是几小时后的某次 MQTT 回调里。pydantic-settings 在构造 Settings
    的瞬间就把所有项校验完，配置错了服务根本起不来，而"起不来"
    远比"跑了两小时后某个功能悄悄不工作"好查。

为什么配置不写死在代码里：

    同一份代码要在两种环境下跑，broker 地址完全不同——
    本机开发（uvicorn 直跑）broker 是 WSL 里 Docker 发布的端口，用 localhost；
    容器里跑 backend 和 mosquitto 在同一 docker 网络，用服务名 mosquitto。
    写死就得改代码。而且 broker 密码属于凭据，不该进代码库。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SQLite URL 的前缀。SQLAlchemy 的写法是 sqlite:/// 加绝对路径，
# 也就是"三个斜杠 + 以 / 开头的路径" = 总共四个斜杠。
_SQLITE_PREFIX = "sqlite:///"


class Settings(BaseSettings):
    """全部运行期配置。

    字段名用小写（``mqtt_host``），对应的环境变量是**大写同名**
    （``MQTT_HOST``）——pydantic-settings 默认大小写不敏感。
    """

    model_config = SettingsConfigDict(
        # .env 只在**当前工作目录**下找。生产环境（容器）不用 .env，
        # 而是直接注入环境变量——容器里塞一个 .env 文件反而会让
        # "配置到底从哪来"变得模糊。
        env_file=".env",
        env_file_encoding="utf-8",
        # .env 里多出来的键（比如只有 docker-compose 用的）不该让服务起不来。
        extra="ignore",
        case_sensitive=False,
    )

    # ── MQTT 连接 ───────────────────────────────────────────────
    mqtt_host: str = "localhost"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)

    # 开发阶段 Mosquitto 是 allow_anonymous true，留空即可
    mqtt_username: str = ""
    mqtt_password: str = ""

    # ★ client_id 必须在 broker 上唯一。
    #   两个实例用同一个 client_id 会互相踢下线，表现是双方轮流掉线、
    #   日志里只有语焉不详的 DISCONNECTED，极难查。
    #   所以 uvicorn 必须 --workers 1；临时起第二个实例调试时要改这里。
    mqtt_client_id: str = "iot-backend-dev"

    # 订阅的 topic。`+` 是 MQTT 单层通配符，iot/+/telemetry 能匹配所有设备，
    # 将来加设备不用改配置。
    mqtt_topic_telemetry: str = "iot/+/telemetry"
    mqtt_topic_status: str = "iot/+/status"
    mqtt_qos: int = Field(default=1, ge=0, le=2)

    # keepalive：本端多久没发任何包就发一个 PINGREQ。
    # 它同时决定"多久能发现对端已死"——broker 挂了而 TCP 没断时，
    # 要等 keepalive×1.5 才察觉。30 秒是个常见折中。
    mqtt_keepalive: int = Field(default=30, ge=5, le=600)

    # 重连退避区间（秒）。paho 的 reconnect_delay_set 用指数退避，
    # 从 min 翻倍到 max。设上下界是为了：下界不至于疯狂重试刷日志，
    # 上界不至于让 broker 恢复后还要等几分钟。
    mqtt_reconnect_min_s: int = Field(default=1, ge=1, le=60)
    mqtt_reconnect_max_s: int = Field(default=30, ge=1, le=300)

    # ★ 是否真的连 MQTT。测试时设 false，这样不需要 broker 也能起 app
    #   （FastAPI 的 lifespan 会跳过整个 MQTT 启动流程）。
    #   没有这个开关的话，每个单元测试都得先起一个 broker。
    mqtt_enabled: bool = True

    # ── 数据库 ─────────────────────────────────────────────────
    # ★ 四个斜杠。写三个会被当成相对路径，库文件落到当前工作目录。
    # ★ 绝不放 /mnt/c —— 见 .env.example 里的详细说明。
    database_url: str = "sqlite:////home/ubuntu/iot-data/iot.db"

    # ── 入库管道 ───────────────────────────────────────────────
    # 队列上限。它的作用是"MQTT 回调不被写库拖慢"：
    # 回调只做解析+入队，写库由独立线程做。
    # 满了就丢并计数（宁可丢也不阻塞回调——回调阻塞会连带
    # 影响 paho 的 keepalive 和 PUBACK）。
    ingest_queue_max: int = Field(default=1000, ge=10, le=100_000)

    # 写线程一次取多少条。批量提交能显著减少 SQLite 的 fsync 次数。
    ingest_batch_size: int = Field(default=50, ge=1, le=1000)

    # clock=="boot" 的行（ts 是开机毫秒，不是 Unix 时间）要不要入库。
    # ★ 默认 true。理由见 .env.example 与 db/repository.py：
    #   如果设备侧 CONFIG_NTP_SERVER 留空（受支持的用法），**所有**报文
    #   都是 boot——丢弃的话数据库会一直是空的，而系统看起来一切正常。
    persist_boot_rows: bool = True

    # 遥测保留天数。
    #
    # ★ 语义是明确的二分，没有"0 天"这个含义：
    #     0  = **不清理**（默认，开发阶段先攒数据）
    #     N>0 = 删除 received_at 早于 (现在 - N 天) 的行
    #
    #   为什么不把 0 解释成"删掉所有比现在旧的行"（那等价于清空）：
    #   配置项的值和"灾难性操作"之间不该有隐式等价。0 是个很容易因为
    #   手滑、模板没填、环境变量拼错而出现的值，让它等于清空太危险。
    #
    # ★ 清理语句必须带 clock='unix' 条件，否则会把 ts 只有几千的
    #   boot 行全删掉——而它们恰恰是排查"设备为什么没同步上时间"的唯一线索。
    telemetry_retention_days: int = Field(default=0, ge=0, le=3650)

    # 保留策略的检查周期（秒）。删是幂等的，所以周期只影响
    # "库里最多多留多久的过期数据"，不影响正确性。
    # 默认 1 小时：清理不是紧急操作，跑得太频繁只是白占 IO。
    retention_prune_interval_s: int = Field(default=3600, ge=60, le=86_400)

    # 每次 DELETE 最多删多少行。
    #
    # ★ 为什么要分块：一次删几百万行会带来两个后果——
    #   1. WAL 文件在事务提交前持续增长（可能涨到几百 MB），
    #      而 checkpoint 要等事务结束，等于把整个库卡住；
    #   2. 写库线程要抢同一把写锁，删得越久，遥测入库被挡住的时间越长。
    #   分块 + 每块单独提交，把一次长锁拆成一串短锁。
    #   代价是总耗时略长（多几次事务开销），这个量级完全可接受。
    retention_chunk_size: int = Field(default=5000, ge=100, le=100_000)

    # ── API 服务 ───────────────────────────────────────────────
    api_host: str = "0.0.0.0"

    # ★ 8005 是实测挑的：8000-8003 已被本机另外 4 个 Flask 服务占用。
    api_port: int = Field(default=8005, ge=1, le=65535)

    log_level: str = "INFO"

    # CORS 允许的来源，逗号分隔；"*" 表示全放开。
    #
    # 为什么用 str + property 而不是直接声明成 list[str]：
    #   pydantic-settings 对复杂类型默认按 **JSON** 解析环境变量，
    #   于是 CORS_ALLOW_ORIGINS=* 会直接报 JSON 解析错误。
    #   绕开它需要 NoDecode 注解（2.3+）或自定义 source，
    #   而"用逗号分隔的字符串 + 一个 property"是零 API 依赖、
    #   一眼能看懂的写法。配置项就一个，不值得为它引入版本相关的 API。
    cors_allow_origins: str = "*"

    # ── 校验 ───────────────────────────────────────────────────
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        """日志级别统一转大写。

        logging 模块对 "info" 这种小写是**不认**的（会静默退回 WARNING），
        而"我明明设了 INFO 却没看到 INFO 日志"是个很费时间的坑。
        在入口处规范化，后面就不用再想这件事。
        """
        v = v.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in allowed:
            raise ValueError(f"LOG_LEVEL 必须是 {sorted(allowed)} 之一，收到 {v!r}")
        return v

    @field_validator("mqtt_reconnect_max_s")
    @classmethod
    def _max_ge_min(cls, v: int, info) -> int:
        """退避上界不能小于下界，否则 paho 的行为不可预期。"""
        lo = info.data.get("mqtt_reconnect_min_s")
        if lo is not None and v < lo:
            raise ValueError(f"MQTT_RECONNECT_MAX_S({v}) 不能小于 MIN({lo})")
        return v

    # ── 派生属性 ───────────────────────────────────────────────
    @property
    def subscriptions(self) -> list[str]:
        """要订阅的 topic 列表，按配置顺序。"""
        return [self.mqtt_topic_telemetry, self.mqtt_topic_status]

    @property
    def cors_origins_list(self) -> list[str]:
        """把逗号分隔的 CORS 配置拆成列表。"""
        raw = self.cors_allow_origins.strip()
        if not raw or raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def sqlite_path(self) -> Path | None:
        """从 database_url 里取出 SQLite 文件路径；不是 SQLite 就返回 None。

        /api/health 要用它报库文件大小，db/database.py 要用它建父目录。
        放在这里而不是各处自己解析 URL，是为了让"URL 长什么样"这件事
        只有一处知道。
        """
        if not self.database_url.startswith(_SQLITE_PREFIX):
            return None
        # "sqlite:///" 去掉之后剩下的是以 / 开头的绝对路径
        return Path(self.database_url[len(_SQLITE_PREFIX):])

    @property
    def mqtt_broker_repr(self) -> str:
        """给日志用的 broker 描述，**不含密码**。"""
        return f"{self.mqtt_host}:{self.mqtt_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取全局配置单例。

    缓存是必要的：Settings 会读 .env 并做校验，每次调用都重来一遍
    纯属浪费（而且 .env 变了不重启本来就该生效，缓存反而更符合直觉）。

    测试不要用这个函数——用 ``create_app(Settings(...))`` 显式注入，
    否则 lru_cache 会让不同测试之间互相污染。
    """
    return Settings()
