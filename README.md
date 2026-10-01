# IoT 环境监测系统 · Python 后端

ESP32-S3 采集温湿度 → MQTT 上报 → 本服务消费 → SQLite → FastAPI 查询。

对应设计文档 `ESP32S3_IoT_AI_工程设计文档_v1.1(1).md` 的 **Phase 6**（§19-§23）。
固件侧见同级目录 `../My_ESP_learn`（独立仓库）。

## 数据流

```
ESP32-S3 ──MQTT──▶ mqtt/consumer ──队列──▶ services ──▶ db/repository ──▶ SQLite
                    (paho 网络线程)   (写库线程)                            │
                                                                           ▼
                                        浏览器 / curl ◀── api/ ◀───────────┘
```

## 环境准备

**Python 解释器不在项目目录里**，这是有意的：

```
/home/ubuntu/venvs/iot-backend          ← 真正的 venv（WSL 原生文件系统）
backend/.venv -> /home/ubuntu/venvs/iot-backend   ← 项目内的软链，方便使用
```

为什么这么做：实测在 `/mnt/c`（DrvFs）上建 venv 要 **72 秒**，在 WSL 原生
文件系统上只要 **1.3 秒**——差 55 倍。而且慢的不只是创建：之后**每次
import** 都要再付一次这个开销，`pytest` 和 `uvicorn --reload` 会难受到没法用。

项目里的 `.venv` 是个软链，所以下面这些写法都正常：

```bash
cd /mnt/c/Users/mgnwo/Desktop/wrok_place/ESP32_project/ESP32_learn/backend
source .venv/bin/activate          # 走软链，实际用的是原生 venv
```

### 首次安装依赖

```bash
python3 -m venv /home/ubuntu/venvs/iot-backend
/home/ubuntu/venvs/iot-backend/bin/pip install \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt -r requirements-dev.txt
ln -sfn /home/ubuntu/venvs/iot-backend .venv
```

用清华源是因为本机代理（Clash）只绑 `127.0.0.1`，WSL 用不了它。

## 配置

```bash
cp .env.example .env
```

`.env` 已被 `.gitignore` 排除，**不会入库**；`.env.example` 是模板，要入库。
每个配置项的取值理由都写在模板文件的注释里。

最容易搞错的两处：

| 场景 | `MQTT_HOST` |
|---|---|
| 本机开发（uvicorn 直跑） | `localhost` |
| 容器里跑 | `mosquitto`（服务名） |

**容器里的 `localhost` 是容器自己**，这是最常见的配置错误，
症状是连接被拒绝而 broker 明明在跑。

## 运行

```bash
# 开发：改代码自动重载
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8005 --reload

# ★ --workers 必须是 1
```

**为什么 `--workers 1` 是硬约束**：多个 worker 会有多个实例用同一个
`client_id` 连 broker，MQTT 规范下它们会互相踢下线，表现是双方轮流掉线、
日志里只有语焉不详的 `DISCONNECTED`。要横向扩展得先解决 client_id 分配。

接口文档（FastAPI 自带）：<http://localhost:8005/docs>

## 用 Docker 跑（常驻）

```bash
# ★ 必须在 WSL 里执行，不是 Windows
cd /mnt/c/Users/mgnwo/Desktop/wrok_place/ESP32_project/ESP32_learn/backend

# 前置：broker 所在的共享网络（只需一次）
sudo docker network create iot-net

sudo docker compose build
sudo docker compose up -d
sudo docker compose ps        # 预期 iot-backend  Up (healthy)
sudo docker compose logs -f
```

从 Windows 访问：<http://localhost:8005/docs>（WSL2 的 localhost 转发，已实测）。

### 为什么需要容器：本地直跑会漏数据

后端不是常驻服务时，**只有它被手动启动的那段时间**才在收数据。
实测踩过：库里只有 11 行，而设备已经发了几千条——中间那段是**永久丢失**的
（broker 不持久化消息）。

`restart: unless-stopped` 让它在 WSL / dockerd 重启后自己回来，于是
"后端没在跑"这个失败模式被消除。**这是做 Step 7 的真正理由**，
不是为了"看起来更工程化"。

### 三个设计点

**① 时区**：容器默认 UTC，`docker logs` 的时间戳会比本地**早 8 小时**。
不影响数据（库里存的是 Unix 毫秒），但排查时你要拿容器日志和串口日志、
broker 日志对齐时间，8 小时的偏差会让"哪个先发生"完全判断反。
所以 compose 里设了 `TZ: Asia/Shanghai`。

**② `CMD` 用 `sh -c "exec uvicorn ..."` 而不是 JSON 形式**：
`exec` 让 uvicorn 成为 PID 1，直接收到 SIGTERM。不加的话 PID 1 是 `/bin/sh`，
它默认不转发信号，`docker compose stop` 会等满超时然后 SIGKILL——
而 SIGKILL 意味着 lifespan 的关闭逻辑不执行，**写库队列里剩的数据直接丢**。
实测确认：日志里有 `写库线程已停止，队列剩余 0 条` 和
`Finished server process [1]`（PID 1 = uvicorn，证明 exec 生效）。

**③ 数据用 bind mount 挂在宿主机**，所以容器和"本机直跑 uvicorn"看到的是
**同一个库文件**。代价与禁忌见下面"数据库"一节。

## Web 仪表盘

**前端在 `web/`**，由后端容器托管，和 API 同源同端口：

```
打开 http://localhost:8005      → 仪表盘
打开 http://localhost:8005/docs → 接口文档
```

不需要额外启动任何东西——镜像里已经包含了前端产物（多阶段构建，
见 `Dockerfile` 的阶段 1）。完整说明见 `web/README.md`。

### 为什么让后端托管，而不是独立容器 + nginx

**同源**。前端和 API 在同一个 origin 下，浏览器不会把请求当成跨域，
于是**开发和生产都不需要 CORS**。少一套只在某个环境才生效的配置，
也就少一类"本地好好的、部署就跨域"的问题。

代价：改前端要重建后端镜像。但依赖层是单独缓存的（`npm ci` 那一步），
改源码的重建在一分钟级。改前端频繁时用 Vite dev server（见 `web/README.md`）。

### 四个必须知道的点

**① 静态文件挂在 `/`，必须在所有 API 路由之后注册**

Starlette 按注册顺序匹配，而挂在 `/` 的 `StaticFiles` 会匹配所有路径。
先挂它的话 `/api/health` 也会被它吃掉并返回 404——症状是
**页面能打开但所有数据都是空的**，很容易误判成前端 JS 的问题。
有测试盯住这个顺序（`test_web_mount_does_not_shadow_api_routes`）。

**② `web/dist` 不存在时后端照常启动，只打一条 WARNING**

后端不该因为"前端还没构建"而起不来。API 是独立可用的，
`/docs` 也是排查工具——让容器起不来等于把排查手段一起关掉。
本机直跑 uvicorn 时必然走这条路径（前端跑在 Vite dev server 上）。

**③ 前端的 TypeScript 类型是 `app/api/schemas.py` 的手写镜像**

改后端响应模型时必须同步改 `web/src/api/types.ts`，否则前端会在
运行期拿到 `undefined`——而 TypeScript 不会报错（它相信你手写的类型）。
这是当前已知的、值得后续改进的一点（见 `web/README.md` 的"已知限制"）。

**④ 画曲线走的是降采样接口 `/api/telemetry/series`，不是 `/api/telemetry`**

`/api/telemetry` 返回**逐条**记录，分页上限 `MAX_LIMIT = 1000`。
设备 2 秒一条 → 只能覆盖 **33 分钟**，画不出更长的窗口。

实测数据（2026-10-01）：半小时窗口内温度跨度 **0.0℃**
（室内环境，DHT11 分辨率 1℃ 确实没变），而 1 小时 1.0℃、
24 小时 2.0℃、3 天 4.0℃。**要看温度变化就必须拉长时间窗口，
而拉长必然超过 1000 条** —— 所以降采样是唯一解。

`/api/telemetry/series?bucket_ms=` 用 SQL 的 `AVG` 按时间桶分组
（`repository.query_telemetry_bucketed`）。24 小时用 5 分钟桶
只有 288 个点。

两个后果必须记住：

- **`ts` 是桶起点，不是采样时刻。** 这条曲线**不能**用来判断
  "设备有没有按时上报" —— 那是原始记录和 `/api/health` 的职责。
- **数据质量面板走的是另一条路**（`/api/telemetry?limit=1` 取逐条
  原始记录）。降采样结果里没有 `received_at` / `seq`：一个桶里有
  很多条记录，保留其中任何一条都会误导，而面板上那两个数字
  **恰恰是用来判断数据真假的**。

详见 `web/README.md` 的"已知限制"第 2 节。

## 目录结构

```
app/
├── main.py            create_app() 工厂 + lifespan
├── config.py          pydantic-settings 配置
├── clock.py           now_ms()，后端时钟的唯一来源
├── logging_conf.py    日志配置 + 限速日志器
├── stats.py           运行期计数器 + seq 跟踪
├── mqtt/              MQTT 接入（paho + 报文契约）
├── db/                持久化（模型 + 仓储，所有 SQL 都在这）
├── services/          业务编排
│   ├── telemetry_service.py  入库（可测入口：handle_batch）
│   └── retention.py          保留策略（可测入口：run_once）
├── api/               HTTP 路由
└── ai/                Phase 8/9 占位（签名写全、实现抛异常）
tools/
├── mqtt_print.py      只连 broker 打印报文（Step 1 的入口，也是排障工具）
└── healthcheck.py     容器 HEALTHCHECK 的探针
web/                   Web 仪表盘（Vue 3 + TS + ECharts）
├── src/api/types.ts     ★ 后端 app/api/schemas.py 的镜像（改后端要同步改它）
├── src/components/      卡片 / 折线图 / 数据质量面板
└── README.md            前端说明与已知限制 ← 改前端先看这个
deploy/                MQTT broker 的 compose + 配置 + 部署说明
├── docker-compose.yml   mosquitto（接入共享网络 iot-net）
├── mosquitto/mosquitto.conf
├── setup-lan-access.ps1 局域网访问（portproxy + 防火墙，需管理员权限）
└── README.md            部署排障手册 ← 部署出问题先看这个
tests/                 pytest
Dockerfile / docker-compose.yml
data/.gitkeep          占位（库文件不入库）
```

**为什么 `deploy/` 在这个仓库里**：它原先在工作区根、不在任何 git 仓库中。
但 `iot-net` 这张网络是 `backend/` 与 `deploy/` 之间的约定——
把耦合的两侧放进同一个仓库，才能一起提交、一起回溯。
完整理由见 `deploy/README.md` 第 8 节。

## 数据库

默认 `sqlite:////home/ubuntu/iot-data/iot.db`（WSL 原生文件系统）。

**绝对不要把库放在 `/mnt/c` 下面。** `/mnt/c` 是 DrvFs（9p），WAL 依赖的
`-shm` 共享内存映射在它上面支持不完整，症状是"明明没有并发却报
`database is locked`"，而且**开发早期不会出现**——等你把数据看得比较重了才炸。

### 两条时间轴

`telemetry` 表同时存两个时间，各司其职：

| 列 | 含义 | 时钟 |
|---|---|---|
| `ts` | 设备**采样**时刻 | 设备时钟（NTP 同步后的 Unix 毫秒） |
| `received_at` | 后端**收到**时刻 | 后端时钟 |

查历史用 `ts`（顺序正确），排查链路用 `received_at`（永远可靠）。
**只有 `received_at` 能发现"设备静默停发"**——实测踩过设备连续发 41 分钟后
彻底停发 2.5 小时，而它本地一切正常；只看 `ts` 的话那段时间就是"没有行"，
分不清"设备没采"和"采了没发出来"。

`find_receive_gaps()` 就是直接回答这个问题的：找出所有超过阈值的入库间隔。
它同时暴露在 `/api/health` 的 `data.recent_gaps` 里。

### 数据保留

`TELEMETRY_RETENTION_DAYS=0`（默认）= **不清理**；`N>0` = 删除 `received_at`
早于"现在减 N 天"的行。

三个细节：

- **按 `received_at` 删，不按 `ts` 删**：`ts` 来自设备时钟，可能失真。
- **只删 `clock='unix'` 的行**：`boot` 行的 `ts` 只有几千，按 `ts` 判"旧"会把
  它们全删掉——而它们是排查"设备为什么没同步上时间"的唯一线索。
- **分块删**（`RETENTION_CHUNK_SIZE`，默认 5000 行/批，每批一次提交）：
  一次删几百万行会让 WAL 在提交前持续增长、写锁一直不释放，
  遥测入库被挡住几分钟。分块把一次长锁拆成一串短锁。

清理跑在独立线程里（`app/services/retention.py`），默认每小时检查一次。
它在 `/api/health` 的 `retention` 段里可见——因为"保留策略没生效"的症状是
**磁盘满**，那时离根因已经很远了。

## 测试

```bash
.venv/bin/python -m pytest            # 全量（覆盖率默认打开）
.venv/bin/python -m pytest tests/test_consumer.py -q
.venv/bin/python -m pytest -k "clock" # 按名字筛
```

**当前：136 个用例，覆盖率 97.5%**（门槛 75%，写在 `.coveragerc` 的 `fail_under`）。

### 核心原则：把"传输"和"业务"切开

```
paho 回调 ──▶ queue ──▶ 写线程 ──▶ TelemetryIngestor.handle_batch()
                                        ▲
                                        └── 测试直接调这一层
```

`handle_batch()` 只接收"一批消息 + 一个 Session"，不认识 paho、不认识网络。
所以**不依赖 broker、不依赖设备**就能覆盖"报文 → 数据库"的全部逻辑。

这不是理论上的好处——是实测撞出来的：调端到端时设备正好静默停发，
如果没有这层隔离，根本分不清是后端的问题还是设备的问题。

### 测试文件的分工

| 文件 | 覆盖什么 |
|---|---|
| `test_ingestor.py` | 报文 → 数据库（去重、boot 行、在线判定、seq 跳号） |
| `test_api.py` | 七个路由 + 边界（404/422） |
| `test_consumer.py` | MQTT 回调（**重连重订阅**、**异常吞掉**、报文校验） |
| `test_retention.py` | 保留策略（**boot 行永不删**、分块、幂等） |
| `test_repository.py` | 查询过滤条件（每个条件真的生效） |
| `test_failure_guards.py` | **静默故障**：写库线程扛异常、schema 版本、队列不阻塞 |
| `test_ai_placeholders.py` | AI 占位层的契约（调用即抛异常） |

### 三个必须避开的坑

**① 测试用临时文件库，不用 `:memory:`**

SQLAlchemy 对 `sqlite:///:memory:` 用 `SingletonThreadPool`，也就是
**每个线程一个独立的空内存库**。而写库线程和测试线程是两个线程——
于是"写入"和"查询"看到的是两个不同的库，表现为**"写了却查不到"
且不报任何错**。

**② 不要 mock 掉 `subscribe()` 的返回值**

`subscribe()` 的结果被解包成 `result, mid`。用 `MagicMock` 的话解包出来
是两个 MagicMock，`result == MQTT_ERR_SUCCESS` 为 False——测试通过，
但走的是"订阅失败"分支。写个显式的小假类（见 `test_consumer.py`）。

**③ 时间用参数注入，不用真实时钟**

`RetentionService.run_once(now=...)` 把"现在"变成显式参数，
所以"删掉 3 天前的数据"不需要真的等三天。依赖隐式当前时间的清理逻辑
只能靠"把库造得很旧"来绕，那种测试要么慢要么脆。

## 版本

对应设计文档 §36 的版本节点。tag 打在**能独立验收**的那个提交上，
不是"功能写完了"的提交上——所以 `v0.5-python` 指向 Step 1 那次提交，
那时它就已经能独立跑通"连上 broker 并打印"了。

| tag | 指向 | 内容 |
|---|---|---|
| `v0.5-python` | Step 1 | MQTT → Python 打印（**不碰数据库**） |
| `v0.6-database` | Step 8 | MQTT → SQLite → FastAPI 七个路由 + 容器化 |

```bash
git tag -l -n99          # 看 tag 和它的说明
git show v0.6-database   # 看那个版本改了什么
```

### 当前状态（2026-09-29 实测）

```
iot-backend  Up (healthy)      mosquitto  Up
mqtt.host = mosquitto   connected = True   connects = 2
db.journal_mode = wal   telemetry rows 持续增长
ingest.running = True   丢弃 = 0
data.stale = False
136 passed, 覆盖率 97.5%
```

端到端验收（计划 §10 的完整清单，15 项）全部通过，
另含文档 §18 的恢复测试：**重启 broker → 1 秒内重连并重新订阅，零丢失**。

### 还没做的

| 项 | 文档 | 说明 |
|---|---|---|
| Web 前端 | §24-§25 | 后端七个路由已就绪，`/docs` 可交互式看接口 |
| AI（异常检测 / 预测） | §26-§33 | 目录与接口占位已就位；**前置条件是先积累几周真实数据** |
| TLS / 鉴权 | §39 | 开发阶段 `allow_anonymous` + `CORS=*` |
| Alembic 迁移 | — | 开发期改 schema 就是删库重建（有 `PRAGMA user_version` 兜底） |

**AI 不是"还没写"，是"还不能写"**：文档 §26 的第一原则是
"先有数据，再训练模型"。库里的数据从 2026-09-28 才开始积累。

