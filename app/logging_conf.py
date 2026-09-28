"""日志配置。

为什么不用 structlog / loguru：

    标准库 logging 已经够用，而且 **uvicorn 自己就用它**。
    引入第三方日志库要么得接管 uvicorn 的输出（配置反而更复杂），
    要么两套格式并存（日志更难读）。本轮也不需要结构化日志——
    等真要接 ELK/Loki 时需求才明确，那时再换不迟。

本模块提供两样东西：

    setup_logging()      一次性配置，让全进程（含 uvicorn）格式统一
    RateLimitedLogger    给"可能每 2 秒触发一次"的路径用的限速日志
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# 格式里带上时间戳和 logger 名字。
# 不带时间戳的日志在排查"数据什么时候断的"时基本没用；
# 不带 logger 名字则分不清是 mqtt、db 还是 api 出的问题。
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", *, access_log: bool = True) -> None:
    """配置根 logger，并把 uvicorn 的 logger 收编进来。

    为什么要显式清掉 uvicorn 自己的 handler：

        uvicorn 默认会给自己装 handler，格式是它自己的那一套
        （没有时间戳）。不清掉的话，同一个进程里会出现两种格式的日志，
        而且时间戳那一栏时有时无——排查时会下意识忽略它。

    :param access_log: 是否输出 uvicorn 的访问日志。
        每条 HTTP 请求一行。开发时有用；但遥测是 2 秒一条、日志量大，
        嫌吵就设 False。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 先清再装，避免重复调用 setup_logging() 时 handler 叠加
    # （叠加的症状是同一条日志打两遍）。
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(handler)

    # 让 uvicorn 的三个 logger 走 root，保证格式一致。
    # uvicorn.error 是它用来打启动/关闭信息的（名字叫 error 但级别是 INFO）。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True

    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if access_log else logging.WARNING
    )

    # paho 的日志很啰嗦（每条报文都打），而且它自己会重连——
    # 重连失败时它按 reconnect_delay 一直打，能把日志刷满。
    # 提到 WARNING：真正的连接问题我们会用自己的日志报（见 consumer.py）。
    logging.getLogger("paho").setLevel(logging.WARNING)


class RateLimitedLogger:
    """按"第 1 次 + 每 N 次"限速的日志包装。

    为什么需要它：

        MQTT 回调、入库失败、报文校验拒绝这几条路径都可能**每 2 秒触发一次**
        （采样周期就是 2 秒）。每条都打会把日志冲垮，真正有用的那几条
        会被淹没；完全不打印又会让"出问题了"这件事静默无声。

        实测过这个场景：设备断网时 MQTT 每 5 秒重试一次，
        如果每次都打日志，一分钟就是 12 条，而排查时你要找的那条
        （第一次失败的原因）早被冲走了。

    策略：第 1 次必打（这一次信息量最大，通常带着根因），
    之后每 ``every`` 次打一条，并附上累计次数。
    累计次数本身也是信息——它直接告诉你问题持续了多久。

    用法::

        self._parse_fail = RateLimitedLogger(logger, every=100)
        ...
        self._parse_fail.hit("报文解析失败 topic=%s: %s", topic, exc)
    """

    __slots__ = ("_log", "_every", "_level", "_count")

    def __init__(
        self,
        logger: logging.Logger,
        *,
        every: int = 100,
        level: int = logging.WARNING,
    ) -> None:
        self._log = logger
        self._every = max(1, every)
        self._level = level
        self._count = 0

    def hit(self, msg: str, *args: Any) -> int:
        """记一次事件，必要时真的打日志。返回累计次数。

        参数用 ``%`` 风格的延迟格式化：只有在真的要打日志时才
        做字符串拼接。热路径上这个差别是可测量的。
        """
        self._count += 1
        if self._count == 1 or self._count % self._every == 0:
            text = (msg % args) if args else msg
            self._log.log(self._level, "%s（累计 %d 次）", text, self._count)
        return self._count

    @property
    def count(self) -> int:
        """累计命中次数。这个数字要暴露到 /api/health 里。"""
        return self._count
