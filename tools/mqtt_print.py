"""Step 1 入口：MQTT → 打印。**不碰数据库。**

对应设计文档 §20 的硬性要求：

    先实现：MQTT → Python → 打印
    例如收到：device=esp32s3_001 / temperature=26.31 / humidity=61.42
    确认 Python 可以稳定接收。

为什么这一步必须独立存在、先于数据库：

    如果直接上数据库，收不到数据时你要同时怀疑四层——broker 通不通、
    paho 连没连上、Pydantic 校验有没有拒掉、SQLAlchemy 写没写进去。
    而这一步只有两层：paho 和校验。**先把这两层确认掉**，
    后面再出问题就只剩数据库那一层。

它同时也是长期排障工具：数据流断了的时候，先用它确认
"消息到底有没有到 Python"，能立刻把问题切成"broker/设备侧"和"后端侧"。

用法::

    cd backend
    .venv/bin/python tools/mqtt_print.py

    # 容器里跑（broker 用服务名）
    MQTT_HOST=mosquitto .venv/bin/python tools/mqtt_print.py

Ctrl+C 退出，退出时会打印一份统计。
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 让 `python tools/mqtt_print.py` 也能 import 到 app 包。
#
# 为什么不靠 PYTHONPATH：这个脚本是给人手动敲的，
# 要求先 export 一个环境变量才能跑，第一次用的人一定会踩。
# 用 __file__ 推导项目根目录，无论从哪里调用都对。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.logging_conf import setup_logging  # noqa: E402
from app.mqtt.consumer import InboundMessage, MqttConsumer  # noqa: E402
from app.mqtt.schemas import TOPIC_KIND_STATUS  # noqa: E402

logger = logging.getLogger("mqtt_print")

# 每隔多久打一行统计。没有它的话，只能靠"有没有在刷屏"判断死活，
# 而设备静默停发和"一切都好只是没数据"在屏幕上看起来是一样的。
SUMMARY_INTERVAL_S = 30

_stop = threading.Event()
_counter_lock = threading.Lock()
_counts = {"telemetry": 0, "status": 0}


def _fmt_rssi(rssi: int | None) -> str:
    """rssi 为 None 时打 "-"，**不要打 0**。

    0 dBm 是合法且极强的读数（贴着路由器才有），用它表示"读不到"
    会把真实数据污染掉。固件侧也是同样的处理（telemetry_json.c:47-63）。
    """
    return "-" if rssi is None else str(rssi)


def _on_message(item: InboundMessage) -> None:
    """sink：把一条已校验的消息打成一行。

    ★ 这个函数运行在 **paho 的网络线程**里，所以必须尽快返回。
      打印一行到缓冲 stdout 是微秒级，没问题；但**不要**在这里
      加任何会阻塞的调用（写文件、HTTP 请求、sleep）。
    """
    stamp = datetime.fromtimestamp(item.received_ms / 1000).strftime("%H:%M:%S.%f")[:-3]

    with _counter_lock:
        if item.kind == TOPIC_KIND_STATUS:
            _counts["status"] += 1
            n = _counts["status"]
        else:
            _counts["telemetry"] += 1
            n = _counts["telemetry"]

    if item.kind == TOPIC_KIND_STATUS:
        assert item.status is not None
        print(
            f"[{n:6d}] {stamp}  device={item.device_id} / status={item.status.state}",
            flush=True,
        )
        return

    t = item.telemetry
    assert t is not None
    # 格式对齐文档 §20 的例子：device=xxx / temperature=xx / humidity=xx
    print(
        f"[{n:6d}] {stamp}  device={t.device_id}"
        f" / temperature={t.temperature_c:g} / humidity={t.humidity_rh:g}"
        f" / rssi={_fmt_rssi(t.rssi)} / seq={t.seq}"
        f" / wifi={t.wifi} / clock={t.clock}",
        flush=True,
    )


def _summary_loop(consumer: MqttConsumer) -> None:
    """周期打印统计。没有它就无法区分"设备停了"和"链路断了"。"""
    while not _stop.wait(SUMMARY_INTERVAL_S):
        with _counter_lock:
            tel, sta = _counts["telemetry"], _counts["status"]
        s = consumer.stats
        logger.info(
            "统计：telemetry=%d status=%d | 收到=%d 无效=%d id不符=%d 忽略topic=%d | 连接=%s",
            tel,
            sta,
            s.messages_received,
            s.invalid_messages,
            s.mismatched_device_id,
            s.topics_ignored,
            "已连接" if consumer.connected else "未连接",
        )


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, access_log=False)

    logger.info(
        "MQTT → 打印（Step 1，不写数据库）。broker=%s client_id=%s",
        settings.mqtt_broker_repr,
        settings.mqtt_client_id,
    )

    consumer = MqttConsumer(settings, _on_message)
    consumer.start()

    # Ctrl+C 优雅退出。
    #
    # 为什么用 signal 而不是 KeyboardInterrupt：
    #   主线程在 Event.wait() 上等着，KeyboardInterrupt 也能打断，
    #   但那样退出路径会经过异常处理，日志里会多一条 traceback，
    #   看起来像崩溃。用 signal handler 设标志位更干净。
    def _handle_sigint(signum: int, frame: object) -> None:
        del signum, frame
        logger.info("收到 Ctrl+C，正在退出……")
        _stop.set()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    threading.Thread(
        target=_summary_loop, args=(consumer,), name="summary", daemon=True
    ).start()

    _stop.wait()

    consumer.stop()
    with _counter_lock:
        tel, sta = _counts["telemetry"], _counts["status"]
    s = consumer.stats
    print()
    logger.info(
        "退出。本次共收到 telemetry=%d status=%d；无效报文=%d，忽略 topic=%d",
        tel,
        sta,
        s.invalid_messages,
        s.topics_ignored,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
