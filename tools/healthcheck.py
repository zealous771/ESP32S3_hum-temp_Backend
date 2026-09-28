"""容器 HEALTHCHECK 用的探针脚本。

★ 为什么单独一个文件，而不是把这段塞进 Dockerfile 的 ``HEALTHCHECK CMD``：

  1. Dockerfile 里的 ``HEALTHCHECK CMD`` 有两种写法，各有毛病——
     JSON 形式（``["python","-c","..."]``）**不展开环境变量**，
     于是端口只能写死，和 ``API_PORT`` 这个配置项脱节；
     shell 形式会展开变量，但内嵌的引号层级会变得很难读、很难改。
     写成脚本文件后，端口从环境变量读，Dockerfile 里只有一行。

  2. 它能被**本地直接运行**（``python tools/healthcheck.py``），
     所以"探针本身对不对"可以单独验证，不用等镜像构建完。

★ 为什么探针查的是 ``/api/health``：

  那个接口的 HTTP 码**恒为 200**（见 app/api/health.py 的说明）。
  所以这个探针实际回答的是"**Web 进程还活着、还能接连接**"，
  而不是"MQTT 连上了、数据在流"。

  这是刻意的：MQTT 断开是可恢复的正常状态，把它算作"不健康"会让
  Docker 去重启容器，而重启解决不了 broker 的问题，还会丢掉写库
  队列里没落盘的数据。"数据流是否正常"应该由人看 /api/health 的
  响应体，或者由将来的告警规则去判断。

★ 退出码约定：0 = 健康，1 = 不健康。任何异常都算不健康。
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

# 探针自身的超时。必须**明显小于** Docker 的 --timeout（见 Dockerfile），
# 否则 Docker 会在我们还没返回时就把这次探测判为超时，
# 于是日志里只有"health check timed out"，看不到真正的错误原因。
TIMEOUT_S = 3


def main() -> int:
    port = os.environ.get("API_PORT", "8005")
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            if resp.status == 200:
                return 0
            print(f"healthcheck: {url} 返回 {resp.status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as exc:
        # 服务在跑但返回了错误码——比连不上更值得报出来
        print(f"healthcheck: {url} 返回 HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        # 连不上（进程没起、还在启动、端口不对）。
        # 这里打印原因而不是让异常抛出——抛出的 traceback 会淹没在
        # `docker inspect` 的 Health.Log 里，而那一行文字才是线索。
        print(f"healthcheck: 无法访问 {url}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
