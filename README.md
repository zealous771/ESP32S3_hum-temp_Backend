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

## 目录结构

```
app/
├── main.py            create_app() 工厂 + lifespan
├── config.py          pydantic-settings 配置
├── logging_conf.py    日志配置 + 限速日志器
├── stats.py           运行期计数器 + seq 跟踪
├── mqtt/              MQTT 接入（paho + 报文契约）
├── db/                持久化（模型 + 仓储）
├── services/          业务编排
├── api/               HTTP 路由
└── ai/                Phase 8/9 占位，本轮不实现
tools/                 开发期排障脚本
tests/                 pytest
```

## 数据库

默认 `sqlite:////home/ubuntu/iot-data/iot.db`（WSL 原生文件系统）。

**绝对不要把库放在 `/mnt/c` 下面。** `/mnt/c` 是 DrvFs（9p），WAL 依赖的
`-shm` 共享内存映射在它上面支持不完整，症状是"明明没有并发却报
`database is locked`"，而且**开发早期不会出现**——等你把数据看得比较重了才炸。

## 版本

对应设计文档 §36 的版本节点：

| tag | 内容 |
|---|---|
| `v0.5-python` | MQTT → Python 打印（不碰数据库） |
| `v0.6-database` | MQTT → SQLite → FastAPI 七个路由 |
