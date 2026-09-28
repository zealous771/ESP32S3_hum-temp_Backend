"""MQTT 接入：paho 客户端、订阅、回调。

★ 这个文件里有三个 paho 2.x 的破坏性变更，写错任何一个都会在运行期炸：

    1. ``mqtt.Client()`` **必须**传 ``callback_api_version``。
       不传直接 ValueError。1.x 没有这个参数。

    2. ``on_connect`` 从 4 个参数变成 **5 个**：
       1.x  (client, userdata, flags, rc)
       2.x  (client, userdata, flags, reason_code, properties)

    3. ``on_disconnect`` 从 3 个变成 **5 个**：
       1.x  (client, userdata, rc)
       2.x  (client, userdata, disconnect_flags, reason_code, properties)

   ``on_message`` 的签名**没变**（client, userdata, msg）——所以只改前两个就行。
   报错形态是 ``TypeError: on_connect() takes 4 positional arguments but 5 were given``。

   ★ 还有一个更隐蔽的：``reason_code`` 不是整数。
     ``ReasonCode`` 是普通 object 的子类（**不是 IntEnum**），
     ``int(reason_code)`` 直接抛 TypeError。判断成败要用 ``.is_failure``。
     详见 ``_on_connect`` 里的注释。

★ 为什么用 ``connect_async()`` + ``loop_start()``，而不是 ``connect()`` + ``loop_forever()``：

    ``loop_forever()`` 会**阻塞当前线程**。后端要在同一个进程里跑 FastAPI，
    主线程必须留给 uvicorn。``loop_start()`` 在后台起一个网络线程，
    主线程立刻返回。而且 ``connect_async`` 在 broker 没起时不会抛异常，
    只是后台一直重试——所以"broker 挂了"不会导致后端起不来。

★ 为什么回调里**只解析 + 交给 sink**，绝不写数据库：

    paho 的网络线程同时负责收报文、发 keepalive（PINGREQ）和确认（PUBACK）。
    在回调里做一次 SQLite 写入（一次 fsync 就是几毫秒到几十毫秒），
    这期间它没法回 PINGREQ——broker 会在 keepalive×1.5 之后认为本端已死
    并断开，于是触发重连、重投、雪崩。

    所以回调的工作量必须稳定在微秒级。Step 3 会把 sink 换成
    "往队列里丢一个对象"，写库由独立线程做。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import paho.mqtt.client as mqtt

from app.config import Settings
from app.logging_conf import RateLimitedLogger
from app.mqtt.schemas import (
    TOPIC_KIND_STATUS,
    TOPIC_KIND_TELEMETRY,
    StatusMessage,
    TelemetryMessage,
    parse_topic,
)
from app.stats import IngestStats

logger = logging.getLogger(__name__)

# 断线日志限速：前 3 次全打（开机阶段信息量最大），之后每 10 次一条。
# 与固件侧 wifi_mgr / mqtt_mgr 的做法一致。
_LOG_ALWAYS_DISCONNECTS = 3
_LOG_EVERY_N_DISCONNECTS = 10


@dataclass(frozen=True)
class InboundMessage:
    """一条**已经校验通过**的入站消息，交给 sink 处理。

    为什么校验在回调里做完、而不是把原始 payload 丢给 sink：

        校验失败要计数、要限速告警，这些都需要 stats 和 logger——
        放在回调里做，sink 就只需要关心"怎么处理合法数据"。
        而且 paho 的 payload 是它内部缓冲的一个 memoryview，
        回调返回后可能被复用；在这里就转成不可变对象更安全。
    """

    topic: str
    device_id: str
    kind: str  # TOPIC_KIND_TELEMETRY 或 TOPIC_KIND_STATUS
    received_ms: int  # ★ 后端时钟，不是设备时钟
    telemetry: TelemetryMessage | None = None
    status: StatusMessage | None = None


# sink 的签名：接收一条已校验的消息，**必须尽快返回**。
Sink = Callable[[InboundMessage], None]


class MqttConsumer:
    """MQTT 消费者：负责连接、订阅、校验，把合法消息交给 sink。"""

    def __init__(
        self,
        settings: Settings,
        sink: Sink,
        *,
        stats: IngestStats | None = None,
    ) -> None:
        self._settings = settings
        self._sink = sink
        # stats 允许外部注入，这样 Step 3 的入库层可以把它的计数器
        # 记在**同一个对象**上，/api/health 只读一处。
        self.stats = stats if stats is not None else IngestStats()

        self._client: mqtt.Client | None = None
        self._started = False

        # 连接状态。用 Event 而不是 bool：/api/health 在 API 线程读它，
        # 而 paho 在网络线程写它。Event 自带内存屏障，不用额外加锁。
        self._connected = threading.Event()

        self._connect_count = 0
        self._disconnect_count = 0
        self._last_message_ms = 0
        self._lock = threading.Lock()

        # 限速日志器。这几条路径都可能每 2 秒触发一次。
        self._parse_fail = RateLimitedLogger(logger, every=100)
        self._unknown_topic = RateLimitedLogger(
            logger, every=1000, level=logging.DEBUG
        )

    # ── 生命周期 ───────────────────────────────────────────────

    def start(self) -> None:
        """创建客户端、注册回调、启动网络线程。**不阻塞、不等待连接**。"""
        if self._started:
            return

        client = mqtt.Client(
            # ★ 不传这个参数在 paho 2.x 里直接 ValueError
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._settings.mqtt_client_id,
            # clean_session=True：broker 不保存本端的订阅和未确认消息。
            #
            # 为什么不用 False（持久会话）：改了订阅的 topic 过滤器之后，
            # broker 里旧的订阅**仍然存在**，于是本端会收到重复消息
            # （新旧过滤器都命中）。这个现象极其隐蔽——你改的是代码，
            # 生效的却是上次运行留下的会话状态。
            #
            # 代价：后端重启的那几秒内 QoS1 消息会丢 2~3 条。
            # 这个代价可以接受——历史数据本来就不该依赖 MQTT 会话持久化，
            # 真要一条不丢得让**设备侧**落盘（文档 §37）。
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )

        if self._settings.mqtt_username:
            client.username_pw_set(
                self._settings.mqtt_username, self._settings.mqtt_password or None
            )

        # 指数退避：从 min 翻倍到 max。设上界是为了不让 broker 长时间
        # 不可达时还要等几分钟才重连上。
        client.reconnect_delay_set(
            min_delay=self._settings.mqtt_reconnect_min_s,
            max_delay=self._settings.mqtt_reconnect_max_s,
        )

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        logger.info(
            "启动 MQTT 客户端：broker=%s client_id=%s 订阅=%s",
            self._settings.mqtt_broker_repr,
            self._settings.mqtt_client_id,
            self._settings.subscriptions,
        )

        # connect_async 在 broker 不可达时**不抛异常**，只是后台重试。
        # 这是有意的：后端不该因为 broker 没起就起不来。
        client.connect_async(
            self._settings.mqtt_host,
            self._settings.mqtt_port,
            self._settings.mqtt_keepalive,
        )
        client.loop_start()
        self._client = client
        self._started = True

    def stop(self) -> None:
        """优雅断开。"""
        if not self._started or self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            # 断开本身失败没什么可做的（可能本来就没连上），
            # 但 loop_stop 必须执行，否则网络线程泄漏。
            logger.debug("disconnect() 抛异常，忽略", exc_info=True)
        self._client.loop_stop()
        self._connected.clear()
        self._started = False
        logger.info("MQTT 客户端已停止")

    # ── 状态查询（供 /api/health）──────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            connects = self._connect_count
            disconnects = self._disconnect_count
            last_ms = self._last_message_ms
        return {
            "enabled": True,
            "connected": self.connected,
            "host": self._settings.mqtt_host,
            "port": self._settings.mqtt_port,
            "client_id": self._settings.mqtt_client_id,
            "subscriptions": self._settings.subscriptions,
            "connects": connects,
            "disconnects": disconnects,
            "last_message_ms": last_ms,
            "last_message_age_ms": (
                int(time.time() * 1000) - last_ms if last_ms else None
            ),
        }

    # ── paho 回调（注意参数个数，见文件头）─────────────────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        """连接成功（或失败）时由 paho 的网络线程调用。

        ★ 这里**必须重新订阅**。原因：clean_session=True 时 broker
          在断开时会丢掉本端的全部订阅，重连后不会自动恢复。
          忘了重新订阅的症状是"首次能收到数据，重连后再也收不到"——
          而连接状态显示是正常的，非常难查。
        """
        del userdata, flags, properties  # 用不到

        # ★ 判断成败必须用 ``.is_failure``，**不能**写 ``int(reason_code) != 0``。
        #
        #   实测（paho 2.1.0）：
        #     ReasonCode 是普通 object 的子类，**不是 IntEnum** ——
        #     int(reason_code) 直接抛 TypeError:
        #     "int() argument must be ... not 'ReasonCode'"。
        #     这个错误只在真正收到 CONNACK 时才炸，写代码时看不出来。
        #
        #   也**不能**自己写 ``reason_code.value != 0``：那是碰巧对的，
        #   但它绕过了 paho 的语义。is_failure 的实现是 value >= 0x80，
        #   而 MQTT 3.1.1 的 CONNACK 老返回码是 1~5（全都 < 0x80）——
        #   看起来会把"密码错"判成成功。实际上 paho 在
        #   convert_connack_rc_to_reason_code() 里已经把老码转成了
        #   MQTT5 风格（实测 1→132, 2→133, 3→136, 4→134, 5→135），
        #   所以 is_failure 是对的。
        #   用 is_failure 而不是 .value，是为了跟 paho 的约定保持一致：
        #   哪天它改了内部映射，用 is_failure 的代码不用动。
        if reason_code.is_failure:
            logger.error(
                "broker 拒绝连接：%s（value=%s，host=%s）。"
                "常见原因：用户名密码错、client_id 被拒、协议版本不支持",
                reason_code,
                getattr(reason_code, "value", "?"),
                self._settings.mqtt_broker_repr,
            )
            return

        with self._lock:
            self._connect_count += 1
            connects = self._connect_count
        self._disconnect_count = 0
        self._connected.set()

        logger.info(
            "已连接 broker %s（第 %d 次）", self._settings.mqtt_broker_repr, connects
        )

        # ★ 重新订阅。这一行漏了就是"重连后收不到数据"。
        for topic in self._settings.subscriptions:
            result, mid = client.subscribe(topic, qos=self._settings.mqtt_qos)
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info("订阅 topic=%s qos=%d (mid=%s)", topic, self._settings.mqtt_qos, mid)
            else:
                logger.error("订阅失败 topic=%s: rc=%s", topic, result)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        del client, userdata, disconnect_flags, properties
        self._connected.clear()
        self._disconnect_count += 1
        # 限速：断线时 paho 会按 reconnect_delay 一直重试，
        # 每次都打日志的话一分钟就是十几条，把真正的原因冲走。
        if (
            self._disconnect_count <= _LOG_ALWAYS_DISCONNECTS
            or self._disconnect_count % _LOG_EVERY_N_DISCONNECTS == 0
        ):
            logger.warning(
                "与 broker 断开（累计第 %d 次）：%s",
                self._disconnect_count,
                reason_code,
            )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """收到报文。**整个函数体必须包在 try/except 里。**

        ★ 为什么这一层 try/except 是必需的，而不是"防御性编程"：

          回调由 paho 的网络线程直接调用。它抛出未捕获异常时，
          **那个线程会退出**——而 paho 不会重启它。结果是连接看起来
          还在（socket 没关），但再也没有任何消息被处理，日志里
          一条错误都没有。这是最典型的"静默无数据"故障。

          所以宁可吞掉异常并打日志，也不能让它冒泡出去。
        """
        del client, userdata
        try:
            self._handle_message(msg.topic, msg.payload)
        except Exception:  # noqa: BLE001
            logger.exception(
                "处理消息时出现未捕获异常（topic=%s）——已吞掉，"
                "否则 paho 网络线程会退出，表现为连接正常但再也收不到数据",
                msg.topic,
            )

    # ── 实际处理 ───────────────────────────────────────────────

    def _handle_message(self, topic: str, payload: bytes) -> None:
        self.stats.messages_received += 1
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._last_message_ms = now_ms

        parsed = parse_topic(topic)
        if parsed is None:
            # 正常情况：将来会有 iot/<id>/cmd 之类我们不订阅的 topic。
            # 用 DEBUG 级别——用 WARNING 的话，一旦订阅了 '#' 就会被刷屏。
            self.stats.topics_ignored += 1
            self._unknown_topic.hit("忽略不认识的 topic：%s", topic)
            return

        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.stats.invalid_messages += 1
            self._parse_fail.hit("JSON 解析失败 topic=%s: %s", topic, exc)
            return

        if not isinstance(raw, dict):
            self.stats.invalid_messages += 1
            self._parse_fail.hit("报文不是 JSON 对象 topic=%s: %r", topic, type(raw))
            return

        if parsed.kind == TOPIC_KIND_TELEMETRY:
            self._handle_telemetry(topic, parsed.device_id, raw, now_ms)
        elif parsed.kind == TOPIC_KIND_STATUS:
            self._handle_status(topic, parsed.device_id, raw, now_ms)

    def _handle_telemetry(
        self, topic: str, topic_device_id: str, raw: dict, now_ms: int
    ) -> None:
        try:
            msg = TelemetryMessage.model_validate(raw)
        except Exception as exc:  # noqa: BLE001  pydantic.ValidationError
            self.stats.invalid_messages += 1
            self._parse_fail.hit("遥测报文校验失败 topic=%s: %s", topic, exc)
            return

        # topic 里的 device_id 与 payload 里的必须一致。
        # 契约把两者绑在一起，不一致说明是固件 bug 或有人在伪造——
        # 两种都该拒收，而且要用 payload 里的还是 topic 里的都说不清，
        # 所以直接不要。
        if msg.device_id != topic_device_id:
            self.stats.mismatched_device_id += 1
            self._parse_fail.hit(
                "device_id 不一致 topic=%s payload=%s —— 已拒收",
                topic_device_id,
                msg.device_id,
            )
            return

        # 时间基准自洽性检查。resolved_clock 返回 None 表示
        # clock 字段与 ts 的量级矛盾（比如 clock=unix 而 ts=5000）。
        if msg.resolved_clock(now_ms=now_ms) is None:
            self.stats.invalid_messages += 1
            self._parse_fail.hit(
                "时间基准矛盾 topic=%s clock=%s ts=%s —— 已拒收",
                topic,
                msg.clock,
                msg.ts,
            )
            return

        self.stats.devices_seen.add(msg.device_id)
        self._sink(
            InboundMessage(
                topic=topic,
                device_id=msg.device_id,
                kind=TOPIC_KIND_TELEMETRY,
                received_ms=now_ms,
                telemetry=msg,
            )
        )

    def _handle_status(
        self, topic: str, topic_device_id: str, raw: dict, now_ms: int
    ) -> None:
        try:
            msg = StatusMessage.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            self.stats.invalid_messages += 1
            self._parse_fail.hit("status 报文校验失败 topic=%s: %s", topic, exc)
            return

        self.stats.devices_seen.add(topic_device_id)
        self._sink(
            InboundMessage(
                topic=topic,
                device_id=topic_device_id,
                kind=TOPIC_KIND_STATUS,
                received_ms=now_ms,
                status=msg,
            )
        )
