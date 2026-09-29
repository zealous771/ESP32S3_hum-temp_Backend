# IoT 后端镜像（含 Web 前端）
#
# 构建（在 WSL 里，从 backend/ 目录）：
#   docker compose build
#   docker compose up -d
#
# 构建出**一个**镜像，里面同时有 API 和 Web 仪表盘。
# 见下面"多阶段构建"一节的说明。
#
# ── 为什么基础镜像写 python:3.12-slim-bookworm，而不是 python:3.12-slim ──
#
#   不带发行版后缀的 slim 会**随上游漂移**到最新的 Debian 版本。
#   对本工程来说这不是小事：SQLite 是系统库（libsqlite3），
#   Debian 大版本一换就可能换掉它，而 WAL 模式的行为、可用的 PRAGMA
#   都跟 SQLite 版本相关。显式写死 bookworm，让"换系统库"变成一次
#   有意识的动作，而不是某天构建出来就变了。
#
#   同理，**不要**用 alpine 跑 Python：它是 musl libc，而本工程依赖的
#   SQLAlchemy/paho 在 musl 上的轮子情况时好时坏，为省几十 MB
#   换一类构建问题不划算。
#
# ── 为什么不装编译工具链 ──
#
#   本工程的 6 个运行时依赖在 x86_64/aarch64 上都有 manylinux 轮子，
#   pip 不需要现场编译。装 gcc 会让镜像大几百 MB，而它永远用不到。
#   （如果哪天加了需要编译的包，pip 会明确报错——那时再装也不迟。）

# ══════════════════════════════════════════════════════════════════
# 阶段 1：构建 Web 前端
# ══════════════════════════════════════════════════════════════════
#
# ★ 为什么要多阶段，而不是"在前端目录里 npm run build 再拷进来"：
#
#   本机 **WSL 里没有 Node**。要构建前端就得装一个（系统包 + 一套工具链），
#   而 Node 只在**构建时**需要——运行期后端只提供静态文件，一行 JS 都不跑。
#   多阶段构建把 Node 留在构建阶段，最终镜像里只有 dist 那几个文件。
#
#   ★ 顺带解决了另一个更隐蔽的问题：**node_modules 的位置**。
#     它有三万多个文件、约 200MB。如果放在项目目录（/mnt/c，DrvFs 上），
#     npm install 会慢到无法接受——我们已经实测过 venv 在 /mnt/c 上
#     要 72 秒、在 ext4 上只要 1.3 秒，差 55 倍。而容器里的 /web
#     是镜像自己的 ext4 层，天然避开了这个问题。
#
#   代价：改前端要重新构建镜像（而不是刷新浏览器）。
#   但依赖层单独缓存了（见下面 npm ci 那一步），改源码的重建是秒级到
#   一分钟级的——因为改 Vue 源码不会让 npm 依赖层失效。
FROM node:22-alpine AS web-build

WORKDIR /web

# 依赖层：先只拷清单。
# 与下面 Python 的依赖层是同一个道理——改一行 Vue 不该重装全部 npm 包。
#
# 用 npm ci 而不是 npm install：
#   ci 严格按 package-lock.json 安装，版本**完全确定**，而且不会去改
#   lockfile。install 会解析版本范围、可能升级间接依赖，
#   于是"同样的代码在不同时间构建出不同的产物"。
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci

# 源码层
COPY web/ ./

# 构建。这个脚本里是 `vue-tsc --noEmit && vite build`——
# 类型检查进构建，接口漂移在构建期就失败，而不是运行期白屏。
RUN npm run build

# ══════════════════════════════════════════════════════════════════
# 阶段 2：运行镜像
# ══════════════════════════════════════════════════════════════════

FROM python:3.12-slim-bookworm

# ── 运行期环境变量 ───────────────────────────────────────────────
#
# PYTHONUNBUFFERED  ★ 这个必须开。
#   默认 Python 会把 stdout 缓冲起来（非 tty 时按块缓冲），于是
#   `docker logs` 看到的日志会**延迟几分钟**才出现，而且顺序错乱。
#   排查问题时"日志不实时"是致命的——你会以为服务卡住了。
#
# PYTHONDONTWRITEBYTECODE
#   不写 .pyc。容器是一次性的，写 __pycache__ 只会让镜像层多出一堆
#   文件，而且挂载覆盖代码时容易留下过期字节码。
#
# PIP_INDEX_URL
#   走清华源。本机有 Clash 但只绑 127.0.0.1，容器访问不到；
#   而 Docker Hub 直连也不通（已实测）。所以构建期必须用国内源。
#   写成 ENV 而不是 --index-url 参数，是为了让 `docker build` 和
#   进容器手动 pip 装包时行为一致。
#
# PIP_NO_CACHE_DIR
#   pip 的下载缓存在镜像里毫无意义（下次构建是新的层），
#   留着只会让镜像大几十 MB。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── 依赖层（★ 单独一层，这是整个 Dockerfile 里最关键的一处）──────
#
# 先只拷 requirements.txt 并安装，再拷代码。原因：
#
#   Docker 的每一层都带一个"输入内容"的哈希，只有输入变了才重建。
#   如果写成 `COPY . .` 再 `RUN pip install -r requirements.txt`，
#   那么**任何一次改代码**都会让 COPY 层变化 → 后面的 pip install
#   层跟着失效 → 每次改一行 Python 都要重装几十个包、等一两分钟。
#
#   拆开之后，只要 requirements.txt 没变，pip 层就一直命中缓存，
#   改代码的重新构建是秒级的。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── 代码层 ────────────────────────────────────────────────────
#
# 只拷需要的目录，不写 `COPY . .`：
#   .dockerignore 虽然也能排除，但那是"黑名单"——将来在 backend/
#   下新建一个目录（比如本机调试脚本），会**默认被拷进镜像**。
#   这里用白名单，新目录必须显式加进来才会进镜像。
COPY app ./app
COPY tools ./tools

# ── Web 前端产物（从构建阶段拷过来）──────────────────────────────
#
# 只拷 dist（编译后的静态文件），不拷源码、不拷 node_modules——
# 那些都在构建阶段，最终镜像里一个字节都没有。
#
# 路径与 app/config.py 的 web_dist_dir 默认值对应：
#   /app/web/dist
# 两者不一致的话，后端启动时会打一条 WARNING 说"前端未构建"，
# 页面会 404 而 API 正常——所以这个路径值得在改动时对一眼。
COPY --from=web-build /web/dist ./web/dist

# ── 非 root 用户 ───────────────────────────────────────────────
#
# ★ uid 固定 1000，和 WSL 里的 ubuntu 用户对齐。
#
#   原因是 SQLite 数据文件通过 **bind mount** 挂进来（见 docker-compose.yml），
#   而 bind mount 不做任何权限映射——容器里的 uid 必须和宿主目录的
#   属主 uid 一致，否则写入直接 Permission denied。
#   用默认的 useradd 随机 uid，会出现"宿主机上能写、容器里写不了"
#   这种很难一眼看穿的现象。
#
#   顺便也是安全实践：容器里的进程不该是 root。虽然 Docker 有
#   命名空间隔离，但一旦出现提权漏洞，root 身份的容器更容易伤到宿主。
#
# --shell /usr/sbin/nologin：这个账号只用来跑服务，不需要登录 shell。
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser

# 数据目录。它会被 bind mount 覆盖，这里建出来只是为了
# **单独运行镜像（不挂卷）时不报错**——那种情况下数据落在容器可写层，
# 容器一删就没了，所以只适合临时验证。
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

EXPOSE 8005

# ── 健康检查 ───────────────────────────────────────────────────
#
# --start-period=20s：给应用启动留出宽限期。
#   这段时间内的失败**不计入** --retries，否则应用还在建表就被判不健康。
#   取 20 秒的依据：建表 + 起写库线程 + 连 broker 实测在 1 秒内完成，
#   留 20 倍余量足够覆盖磁盘慢或 broker 无响应的情况。
#
# --interval=30s / --timeout=5s / --retries=3：
#   探针自己的超时是 3 秒（见 tools/healthcheck.py），比这里的 5 秒小，
#   这样超时错误能由探针打印出具体原因，而不是被 Docker 判成
#   "health check timed out"（那句话没有信息量）。
#
# 用 JSON 形式（不是 shell 形式）：不需要 shell 展开，
# 端口由探针自己从环境变量读。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "tools/healthcheck.py"]

# ── 启动命令 ───────────────────────────────────────────────────
#
# ★ 为什么用 shell 形式 + exec，而不是 JSON 形式：
#
#   端口有两个"知道它的地方"——uvicorn 真正监听的那个，和健康探针
#   （tools/healthcheck.py）去连的那个。探针从 API_PORT 环境变量读，
#   所以 uvicorn 也必须从同一个环境变量读，否则改一次端口要改两处，
#   而漏改一处的表现是"容器一直不健康"（探针连错端口），
#   服务本身其实是好的。
#
#   JSON 形式不展开环境变量，所以只能写死端口——那就把上面这个坑
#   固化下来了。shell 形式能展开 ${API_PORT:-8005}。
#
#   ★ `exec` 不能省：不加的话 PID 1 是 /bin/sh，uvicorn 是它的子进程。
#     Docker 发 SIGTERM 时只发给 PID 1，sh 默认不转发信号，于是
#     `docker compose stop` 会等超时（默认 10 秒）后 SIGKILL ——
#     而 SIGKILL 意味着 lifespan 的关闭逻辑不执行：
#     写库线程来不及把队列里剩的数据落盘，保留策略也不会干净退出。
#     加 exec 之后 uvicorn 变成 PID 1，直接收到信号，优雅退出。
#
# ★ --workers 1 是**硬约束**，不是性能取舍。
#
#   每个 worker 都是一个独立进程，各自建一个 MQTT 客户端。它们用同一个
#   client_id（MQTT_CLIENT_ID）连 broker，于是互相把对方踢下线，
#   形成"连上→被踢→重连→再被踢"的连接风暴，而日志里只有语焉不详的
#   DISCONNECTED，极难定位。
#
#   单 worker 对本工程的负载完全够：写入是 2 秒一条，
#   读取是几个浏览器页面。
#
# 不用 --reload：那是开发用的（会起 watchdog 进程反复重启），
# 本机开发直接跑 uvicorn 更简单，容器里要的是稳定。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${API_PORT:-8005}\" --workers 1"]
