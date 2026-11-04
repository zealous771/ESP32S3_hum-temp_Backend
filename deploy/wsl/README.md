# WSL2 侧的主机配置资产

`backend/` 里的代码是"跑在 WSL 里"的那一半：Python 后端（`../app/`）、
Vue 前端（`../web/`）、mosquitto broker（`../docker-compose.yml` + `../mosquitto/`）。
但光有这些还不够，WSL 主机本身还有三处配置决定链路能不能通。

本目录把那三处配置**以可直接安装的文件**形式固定下来，避免换机器时
只能翻文档手敲。完整原理与排障见 [`../README.md`](../README.md) 第 3、4 节。

| 文件 | 装到哪 | 解决什么 |
|---|---|---|
| `wsl.conf` | `/etc/wsl.conf` | 启用 systemd（`wsl-fixed-ip.service` 和 Docker 自启的前提） |
| `wsl-fixed-ip.service` | `/etc/systemd/system/` | 给 eth0 加固定次要 IP，让 portproxy 的目标不随 DHCP 变 |
| `WSL-KeepAlive.bat` | Windows 启动文件夹（**可选**） | 顶住 WSL 发行版，防止空闲后被自动停止 |

## 当前实际状态（2026-11-04 核对）

| 项 | 状态 |
|---|---|
| `systemd=true` | 已启用 |
| `wsl-fixed-ip.service` | enabled + active，`172.22.120.100/20` 为 eth0 次要地址 |
| `docker.service` | enabled + active |
| `iot-net` | 已创建，两个 compose 项目共用 |
| 数据目录 | `/home/ubuntu/iot-data/`（WSL 原生 ext4，WAL 生效） |
| **保活脚本** | **已停用**——启动文件夹里没有，本机留的是 `~/Desktop/WSL-KeepAlive.bat.disabled` |

所以**重启电脑后 WSL 不会自动起来**，后端和 broker 也不会恢复。
需要长时间采集时，把 `WSL-KeepAlive.bat` 复制进启动文件夹，或手动起 WSL 后
在两个 compose 目录各跑一次 `docker compose up -d`。

## 两个容易踩的坑

1. **`/20` 掩码跟着 WSL 的 NAT 网段走**。如果 Windows 更新或 `.wslconfig` 变更
   导致网段变了，`172.22.120.100` 可能落到网段外 —— 这时要同时改 unit 里的地址
   和 `setup-lan-access.ps1` 里的 `$wslIp` / portproxy 目标。
2. **只配 portproxy 不够**。Windows 防火墙默认**丢弃**不回 RST，设备端只看到
   TCP 超时，和"broker 根本没起"一模一样。`setup-lan-access.ps1` 里两条都做了，
   防火墙规则用 `remoteip=LocalSubnet` 自动跟随网段，换路由器不用改。
