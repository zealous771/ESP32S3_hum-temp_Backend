# deploy —— 开发阶段的 MQTT broker

给 ESP32-S3 节点提供一个局域网内可访问的 MQTT broker。
按设计文档 §12：**开发阶段先在本地跑，不要一上来上云**——出问题时
你能在同一个终端里直接看到数据，排查成本最低。

## 0. 拓扑

```
ESP32-S3                Windows                  WSL2 (Ubuntu-24.04)
192.168.31.x  ──LAN──▶  192.168.31.128  ──portproxy──▶  eth0:1883
                        （物理网卡）      （1883 转发）        │
                                                             ▼
                                              ┌──────── Docker ────────┐
                                              │  网络 iot-net          │
                                              │  ┌──────────────────┐  │
                                              │  │ mosquitto:1883   │  │
                                              │  └────────┬─────────┘  │
                                              │           │ 服务名直连  │
                                              │  ┌────────▼─────────┐  │
                                              │  │ iot-backend:8005 │  │
                                              │  └──────────────────┘  │
                                              └────────────────────────┘
                                                    ▲
                                    Windows localhost:8005（WSL 转发）
```

四个必须理解的点，否则会卡很久：

1. **容器端口只在 WSL 的网络命名空间里。** Docker 的 `-p 1883:1883`
   绑的是 WSL 的 `eth0`，局域网设备根本看不到 WSL 的内部地址
   （`172.22.x.x` 网段）。所以 Windows 侧必须再做一次转发（portproxy）。
2. **WSL 发行版会在没有 `wsl.exe` 客户端连接时自行停止**，
   里面跑的 systemd 服务和容器一起被杀。这是本文最容易漏掉、
   后果最严重的一环——见第 4 节。
3. **两个容器之间走 Docker 网络，不走宿主机端口。** 后端用服务名
   `mosquitto` 直接连 broker，比"容器→宿主机 localhost→端口映射→
   再回容器"少绕两跳，也少了"宿主端口被占用"这类失败模式。
   见 2.1 节。
4. **`-p 1883:1883` 在接了共享网络之后仍然需要**：后端走容器网络，
   但 ESP32 是从局域网进来的，它必须经过这个映射 + portproxy。
   两者互不替代。

本机实测环境：Windows 局域网 `192.168.31.128/24`，网关 `192.168.31.1`；
WSL 虚拟网卡 `172.22.112.1/20`。

## 当前配置状态（2026-09-29 实测）

| 项目 | 状态 |
|---|---|
| Ubuntu-24.04 里的 Docker Engine | ✅ 已装 `docker.io` 29.1.3 + Compose 2.40.3，服务 active |
| 镜像加速器 | ✅ `/etc/docker/daemon.json`，`docker.xuanyuan.me` 实测可用 |
| mosquitto 容器 | ✅ 运行中，容器内监听 `0.0.0.0:1883`，pub/sub 自测通过 |
| 共享网络 `iot-net` | ✅ 已创建，mosquitto 与 iot-backend 都在上面 |
| iot-backend 容器 | ✅ `Up (healthy)`，`/api/health` 里 `mqtt.host=mosquitto`、`db.journal_mode=wal` |
| WSL 固定辅助 IP | ✅ `172.22.120.100/20`（`wsl-fixed-ip.service` 早已 enabled+active） |
| **portproxy 转发 1883** | ✅ 已存在：`0.0.0.0:1883 → 172.22.120.100:1883` |
| **防火墙放行 1883** | ✅ 规则 `MQTT Broker 1883`，`RemoteIP=192.168.31.0/24`，Enabled |
| 局域网实测 | ✅ ESP32 能连上并持续上报（`MQTT connected`，`rssi=-28`） |
| WSL 保活 | ⚠️ 脚本在启动文件夹里，但**需要登录后真正执行**才生效（见第 4 节） |

也就是说：**整条链路（设备 → 局域网 → portproxy → WSL → broker → 后端 → SQLite）已经全通**。

> ⚠️ 唯一还需要盯的是最后一行：WSL 发行版在没有 `wsl.exe` 客户端连接时会自行停止。
> 判断方法（在 WSL 里跑）：
>
> ```bash
> ps -eo args | grep -w sleep      # 有 "sleep infinity" = 保活在跑；空 = 随时会停
> ```

## 1. 在 Ubuntu-24.04 里装 Docker Engine

不用 Docker Desktop——那样容器会跑在 `docker-desktop` 那个独立发行版里，
和本机的 WSL 环境隔开，后面想调网络会多一层。

```bash
wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y docker.io docker-compose-v2
wsl.exe -d Ubuntu-24.04 -u root -- systemctl enable --now docker
wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active docker    # 预期 active
```

`systemctl` 能用是因为 Ubuntu-24.04 里 systemd 已经启用了
（`systemctl is-system-running` 返回 `running`）。

给日常用户加 docker 组，省得每次都 `sudo`：

```bash
wsl.exe -d Ubuntu-24.04 -u root -- usermod -aG docker mgnwo
```

> **与 Docker Desktop 的关系**：本机装了 Docker Desktop 但没在运行，
> 也没给 Ubuntu-24.04 开 WSL 集成。装了原生 Docker 之后
> **不要**再去开那个集成——否则 `docker` CLI 的 context 会被
> Docker Desktop 覆盖，容器会跑到另一个 VM 里去，网络配置全部作废。

### 1.1 ★ 必须配镜像加速器（国内网络直连 Docker Hub 会超时）

实测直连 `registry-1.docker.io` 报 `dial tcp ...: i/o timeout`。
写一份 `/etc/docker/daemon.json`（本仓库的 `.tmp/daemon.json` 是现成的）：

```json
{
  "registry-mirrors": [
    "https://docker.xuanyuan.me",
    "https://docker.1ms.run",
    "https://docker.1panel.live",
    "https://docker.m.daocloud.io"
  ]
}
```

**顺序是有讲究的——按实测结果排的**。本次踩坑记录：

| 加速器 | `/v2/` 探测 | 实际拉镜像 |
|---|---|---|
| `docker.xuanyuan.me` | 401 | ✅ **成功** |
| `docker.1ms.run` | 401 | 未单独验证 |
| `docker.1panel.live` | 200 | ❌ blob 下载 `EOF` |
| `docker.m.daocloud.io` | 401 | ❌ blob 下载 `EOF`（R2 存储） |

**教训**：`/v2/` 返回 401/200 只能说明"鉴权端点活着"，**不代表能拉下镜像**
——真正的瓶颈在 blob 存储。所以探测只能用来排除明显死掉的，最终必须
真的 `docker pull` 一次才算验证过。遇到 `failed to copy ... EOF`
就把它往后排，换下一个。

```bash
wsl.exe -d Ubuntu-24.04 -u root -- cp /mnt/c/.../.tmp/daemon.json /etc/docker/daemon.json
wsl.exe -d Ubuntu-24.04 -u root -- systemctl restart docker
```

**探测某个加速器还活着没有**（`/v2/` 返回 401 是正常的，那是要求鉴权，
说明服务在；000 才是不可达）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 https://<加速器域名>/v2/
```

> 公共加速器时好时坏，列多个让它自动回退。**注意**：镜像加速器只加速
> `docker.io` 的官方镜像；加速器自己的 blob 存储（如 daocloud 的 R2）
> 也可能失败，报 `failed to copy ... EOF`。遇到这种情况就换一个排在前面。
>
> 如果本机开着代理（Clash 等）而加速器都不好用，可以让 Docker 走代理：
> 在 `daemon.json` 里加 `"proxies": {"http-proxy": "...", "https-proxy": "..."}`。
> 但**前提是代理允许局域网访问**——Clash Verge 默认只绑 `127.0.0.1`，
> WSL 里的容器够不着，需要先在设置里打开「允许局域网」，
> 然后地址填 Windows 侧的 WSL 网关（`172.22.112.1`）而不是 `127.0.0.1`。

---

## 2. 起 broker

```bash
cd /mnt/c/Users/mgnwo/Desktop/wrok_place/ESP32_project/ESP32_learn/backend/deploy
sudo docker compose up -d
sudo docker compose ps            # 预期 State = running
```

先在本机自测，确认容器本身没问题（这一步**不涉及**局域网）：

```bash
# 在 WSL 里
sudo docker exec mosquitto mosquitto_sub -t 'test' -C 1 &
sudo docker exec mosquitto mosquitto_pub -t 'test' -m 'hello'
```

### 2.1 共享网络 `iot-net`（后端容器要用）

后端（`../backend/`）跑在另一个容器里，它连 broker 有两条路：

| 方式 | 地址 | 问题 |
|---|---|---|
| 走宿主机端口 | `172.17.0.1:1883`（docker0 网关） | 依赖宿主端口映射，绕两跳，且"宿主机端口被占用"会成为一个新失败模式 |
| **走共享网络** | **`mosquitto:1883`**（服务名） | 无——Docker 内嵌 DNS 直接解析 |

选第二条。要建一张两个 compose 项目**都接入**的网络：

```bash
sudo docker network create iot-net      # 只需一次
```

然后两边各自在 compose 里声明（**都已改好**，见各自文件）：

```yaml
services:
  mosquitto:
    networks: [iot-net]
networks:
  iot-net:
    external: true
    name: iot-net
```

**为什么必须是 `external: true`**：Compose 默认会给每个项目建一张
`<项目名>_default` 网络，项目之间互相看不见。而 `deploy_default` 这个名字
是 Compose **从目录名推出来的**——哪天目录改名、或者用 `-p` 指定了别的
项目名，名字就变了，后端会连不上 broker，而没人会记得这两个仓库有依赖关系。

显式建一张名字固定的 `iot-net` 就没有这个问题。

**代价**：这张网络的名字成了两个仓库之间唯一的隐式约定。
改了只改一边的症状是"后端连不上 broker"，而 `docker network inspect`
才能看出两个容器挂在不同网络上。排障清单里记了这一条。

**改完要重建容器**（网络变更不会热应用）：

```bash
cd deploy && sudo docker compose up -d      # mosquitto 会 Recreate
cd ../backend && sudo docker compose up -d  # 后端同样
```

设备会自动重连（已实测）。

---

## 3. 让局域网能访问

### 3.1 给 WSL 的 eth0 加一个固定辅助 IP

WSL 的 `eth0` 地址由 DHCP 分配、每次重启都可能变，而 portproxy 规则里
写的是固定地址。地址一变，转发就指向空气。

**WSL2 的网络不走 netplan**（实测 `/etc/netplan/` 为空、
`systemd-networkd` 与 `NetworkManager` 都是 inactive，eth0 由内核
启动参数直接配置），所以常规"改 netplan 设静态 IP"在这里完全无效。
正确做法是**附加一个固定的辅助地址**：

```bash
# 选一个 172.22.112.0/20 里未被占用的地址，先 ping 确认
sudo ip addr replace 172.22.120.100/20 dev eth0
```

用 `replace` 而不是 `add`：`add` 重复执行会报 `RTNETLINK answers: File exists`，
`replace` 是幂等的。

持久化（`/etc/systemd/system/wsl-fixed-ip.service`）：

```ini
[Unit]
Description=Add fixed secondary IP to eth0 as stable target for Windows portproxy
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/ip addr replace 172.22.120.100/20 dev eth0
ExecStop=-/usr/sbin/ip addr del 172.22.120.100/20 dev eth0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now wsl-fixed-ip.service
```

注意 `ip` 的绝对路径是 `/usr/sbin/ip`（systemd 的 `ExecStart` 要求绝对路径）。

### 3.2 portproxy 端口转发 + 3.3 防火墙（**需要管理员权限**）

**已经脚本化了，直接跑**：

```powershell
# 以管理员身份运行 PowerShell
cd C:\Users\mgnwo\Desktop\wrok_place\ESP32_project\ESP32_learn\deploy
.\setup-lan-access.ps1
```

脚本做两件事，都是幂等的（先删后加，可以反复执行）：
1. `netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=1883
   connectaddress=172.22.120.100 connectport=1883`
2. `netsh advfirewall firewall add rule name="MQTT Broker 1883" dir=in action=allow
   protocol=TCP localport=1883 remoteip=192.168.31.0/24 profile=any`

规则写在注册表里，**重启后保留**。

> **为什么 `remoteip` 要写网段而不是靠 `-Profile`**：开 VPN 时网络分类可能
> 变化，靠源 IP 约束不受影响。调试阶段可以先改成 `any` 确认全链路能通，
> 再收窄——分两步走能避免把"网段写错"和"网络隔离"两种问题混在一起排查。
>
> **网段必须按实际子网掩码算，不要凭 IP 猜**。实测遇到过 IP 是
> `10.158.241.128` 但掩码是 `255.255.254.0`（/23）的情况，网段横跨两个
> C 段；按 /24 写会让同网段内落在另一个 C 段的设备被防火墙**静默丢弃**，
> 表现为"本机可访问、其他设备连不上"。

### 3.4 验证

```powershell
Test-NetConnection 192.168.31.128 -Port 1883
# 预期 TcpTestSucceeded : True
```

更详细地看 TCP 层：
`netstat -ano | findstr :1883` —— 预期看到 `0.0.0.0:1883`，
**不是** `127.0.0.1:1883`。

---

## 4. 保活：最容易漏、后果最严重

### 问题

**WSL2 发行版在没有 `wsl.exe` 客户端连接时会完全停止**，
里面跑的 systemd 服务和 Docker 容器随之全部被杀。

症状很迷惑：`localhost` 偶尔能通（那只是恰好有 `wsl` 命令在执行、
把发行版唤醒了），命令结束约 8 秒后又全断。ESP32 侧表现为
"上报时断时续、过一会儿全停"。

### 实测结论（2026-09-28，已确诊）

静默等待 **95 秒**（期间不执行任何 `wsl.exe` 命令）后：

```
  NAME              STATE           VERSION
* Ubuntu-24.04      Stopped         2
```

**发行版确实会自行停止**，Docker 容器一起被杀。所以保活是必需的，
不是"以防万一"。

### 已做的处理

创建了
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WSL-KeepAlive.bat`
（无 BOM、CRLF 行尾）：

```bat
@echo off
start "" /min "C:\Windows\System32\wsl.exe" -d Ubuntu-24.04 -u root -- sleep infinity
exit
```

提供一个常驻的 `wsl.exe` 客户端连接。
**代价是任务栏会多一个最小化的控制台窗口，而且不能关**——关了 broker 就停。

- **取消保活**：删掉那个 `.bat`（下次登录生效），或直接关掉那个窗口（本次生效）。
- **本次会话立即生效**：双击运行一次那个 `.bat`。
- **待你确认**：下次登录后看任务栏有没有那个最小化窗口。沙箱无法启动
  独立于会话的持久进程，所以这一步只能由你实测。

> 注意：写这个 `.bat` 要用专门的写文件工具，别用 shell heredoc。
> Git Bash 会把 `C:\Windows\...` 转成 `C:/Windows/...`，批处理不认正斜杠。
> 另外 `Write` 工具有时会在开头写 UTF-8 BOM，而 BOM 会让 cmd 把
> 第一行的 `@echo off` 当成乱码命令报错——写完要 `head -c 3` 确认前
> 三字节是 `@ec` 而不是 `357 273 277`。

### 已验证：容器会自动恢复

发行版停止后再唤醒，`docker` 服务 `active`，容器 **4 秒内**自动恢复
——`docker-compose.yml` 里的 `restart: unless-stopped` 生效。
所以保活只需要保证发行版活着，不需要额外管容器。

### 无效的修复尝试（不要重复踩）

| 方案 | 结果 |
|---|---|
| `.wslconfig` 里设 `vmIdleTimeout` | 无效，WSL 照样停止 |
| `conhost.exe --headless` | 命令根本没执行 |
| `schtasks.exe` 建计划任务 | 常被安全策略阻止 |

---

## 5. 端到端验证

```bash
# WSL 里订阅
sudo docker exec mosquitto mosquitto_sub -v -t 'iot/#'
```

设备侧烧录后应该看到：

```
iot/esp32s3_001/status    {"state":"online"}
iot/esp32s3_001/telemetry {"device_id":"esp32s3_001","ts":1789...,"seq":1,
                           "temperature_c":26,"humidity_rh":58,
                           "wifi":true,"rssi":-47,"clock":"unix"}
```

按文档 §18 还要测四类恢复：

| 测试 | 预期 |
|---|---|
| 断 WiFi | 串口 `console` 照常打印；恢复后自动重连并重发 `online` |
| 重启 broker | 自动重连，telemetry 继续 |
| 重启 ESP32 | 重新上线 |
| 断网期间 | telemetry 停发，`seq` 跳号如实体现丢了多少条 |

LWT 验证：给板子**直接断电**（不是软重启），broker 应代发
`iot/esp32s3_001/status {"state":"offline"}`。

---

## 6. 后端容器（FastAPI + SQLite）

**完整说明在 `../backend/README.md`**，这里只放"和 broker 有关的"部分。

```bash
cd /mnt/c/Users/mgnwo/Desktop/wrok_place/ESP32_project/ESP32_learn/backend
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps      # 预期 iot-backend  Up (healthy)
```

三个和 broker 直接相关的点：

**① `MQTT_HOST=mosquitto`，不是 `localhost`。**
"容器里的 localhost 是容器自己"是最常见的配置错误，症状是连接被拒绝，
而 broker 明明在跑。地址写在 `backend/docker-compose.yml` 的 `environment` 里。

**② 不写 `depends_on: mosquitto`，也写不出来。**
mosquitto 由本目录这个**另一个 compose 项目**管理，Compose 的 `depends_on`
只在项目内有效，跨项目硬写会因找不到服务名而直接报错。

而且从设计上说也不该依赖它：**后端必须容忍 broker 不在**。broker 重启、
WSL 重启、网络抖动都会断开连接，paho 自带指数退避重连，连上就继续收。
真要编排反而有害——那会让后端因为 broker 没起而不启动，于是连
`/api/health` 都看不到，排查时少了一个观测点。

**③ 数据落在宿主机，不在容器里。**

`backend/docker-compose.yml` 把 `/home/ubuntu/iot-data` bind mount 到
容器的 `/data`，所以库文件和"本机直跑 uvicorn"时是**同一个文件**。

⚠️ **绝不能挂 `/mnt/c`。** 那是 DrvFs（9p），SQLite 的 WAL 依赖 `-shm`
共享内存映射，9p 支持不完整，症状是"明明没有并发却报
`database is locked`"，而且**开发早期不出现**。
（`app/db/database.py` 启动时会校验 `journal_mode`，不是 `wal` 就直接抛异常，
所以配错了会立刻暴露，不会带着错误假设跑起来。）

⚠️ 容器和本机直跑**不要同时开**：两个进程会争同一个库文件、还会撞
MQTT `client_id`。

**验证整个链路**：

```bash
# 1. 容器内部
sudo docker exec iot-backend python -c \
  "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8005/api/health'))['mqtt'])"

# 2. 从 Windows（WSL2 的 localhost 转发）
curl http://localhost:8005/api/health
```

预期 `mqtt.host = "mosquitto"`、`mqtt.connected = true`、
`db.journal_mode = "wal"`、`data.stale = false`。

---

## 7. 撤销

```powershell
# 管理员 PowerShell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=1883
netsh advfirewall firewall delete rule name="MQTT Broker 1883"
```

```bash
# 后端（先停，再删网络）
cd ../ && sudo docker compose down

sudo systemctl disable --now wsl-fixed-ip.service
sudo rm /etc/systemd/system/wsl-fixed-ip.service
cd deploy && sudo docker compose down

# 共享网络（两个 compose 都 down 之后才删得掉）
sudo docker network rm iot-net
```

---

## 8. 目录归属与版本控制（已定案）

本目录是 **`backend/deploy/`**，和 `backend/` 在**同一个 git 仓库**里。

| 目录 | 仓库 |
|---|---|
| `My_ESP_learn/` | 固件仓库（remote：`zealous771/ESP32-S3N16R8-learn`） |
| `backend/`（含本目录 `deploy/`） | 服务端仓库，独立 `git init` |

### 为什么移进来（2026-09-29 定案）

原先 `deploy/` 在工作区根、**不在任何仓库里**。Phase 6 之前无所谓——
那时它只是个 mosquitto 容器，改坏了重写一遍也就几分钟。

Phase 6 之后开始有代价，具体在 2.1 节那张 `iot-net` 网络上：

> 它是 `backend/` 与 `deploy/` 之间的约定。而 `deploy/` 这一侧
> （网络声明、`mosquitto.conf`、本 README）**没有历史**——
> 改了没有记录，也回不去。

移进 `backend/deploy/` 之后，这个耦合从"跨仓库的隐式约定"变成
"同一仓库内的目录关系"：能一起提交、一起回溯，`git log -- backend/deploy/`
就能看到 broker 配置的完整变更史。

**代价**：`backend/` 的构建上下文里多了个 `deploy/`（已在 `.dockerignore`
里排除，不会进镜像）；以及 `docker compose` 的相对路径要多写一层
（`cd deploy` 而不是 `cd ../deploy`）。

### 设计文档 §35 的伞仓库

文档设想的是 `iot-ai-monitor/{firmware,backend,web,deploy}`。
将来真要建伞仓库时，把 `backend/deploy/` 挪回顶层 `deploy/` 即可——
compose 文件内部用的是相对路径（`./mosquitto/mosquitto.conf`），
跟着文件走，移动目录不用改内容。

`mosquitto.conf` 里目前**没有凭据**（开发阶段匿名）。
正式部署时按文档 §39 加 `password_file` 并把 `allow_anonymous` 关掉，
那时密码文件**不能**入库。

