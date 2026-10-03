# 让局域网里的 ESP32 能访问跑在 WSL2 里的 MQTT broker
#
# ★ 必须以管理员身份运行 PowerShell，否则 netsh 会报
#   "The requested operation requires elevation (Run as administrator)."
#
# 用法：右键 PowerShell → 以管理员身份运行，然后执行
#   cd C:\Users\mgnwo\Desktop\wrok_place\ESP32_project\ESP32_learn\backend\deploy
#   .\setup-lan-access.ps1
#
# 为什么需要这一步：Docker 的 -p 1883:1883 绑的是 **WSL 的 eth0**，
# 局域网设备根本看不到 WSL 的内部地址（172.22.x.x 网段）。
# 所以要在 Windows 侧再做一次端口转发，把物理网卡上的 1883
# 转到 WSL 的 1883 上。
#
# 本机现状（已探测）：
#   - WSL 的 eth0 上已经有固定辅助地址 172.22.120.100（由
#     /etc/systemd/system/wsl-fixed-ip.service 维护，已 enabled+active），
#     所以这里直接用它，不必再配。
#   - 已有的 portproxy 规则是 8000-8003 + 1883，本脚本只重建 1883 那条。

$ErrorActionPreference = 'Continue'

# WSL 的固定辅助地址。改这个值要同时改 WSL 里的 wsl-fixed-ip.service。
$wslIp = '172.22.120.100'

# ★ 防火墙来源用 netsh 内置的 LocalSubnet，不写死具体网段。
#
#   2026-10-03 换路由器：网段从 192.168.31.0/24 变成 192.168.1.0/24，
#   而这条规则写死的是旧网段 —— 结果是**设备连上了 WiFi 却一条数据都
#   发不出去**，且没有任何错误提示（包被防火墙静默丢弃，设备侧只看到
#   "连接超时"，看不到"被拒绝"）。
#
#   LocalSubnet 会自动匹配当前的活动子网，换网络不用改脚本。
#   代价：WSL（172.22.x.x）和 VMware（192.168.x.x）的虚拟网段也算
#   "本地子网"，1883 对它们也开放 —— 家用环境可接受。
#
#   想收窄成单一网段就把它改成实际网段，但要**按实际子网掩码算**，
#   不要凭 IP 猜：掩码是 /23 时网段横跨两个 C 段，按 /24 写会让同网段
#   的设备被静默丢弃。
$lanCidr = 'LocalSubnet'

$port = 1883

Write-Host "== 1/3 端口转发：0.0.0.0:$port -> ${wslIp}:$port" -ForegroundColor Cyan
# 先删后加，让脚本可以重复执行（幂等）
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$port 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$port connectaddress=$wslIp connectport=$port
if ($LASTEXITCODE -ne 0) {
    Write-Host "端口转发添加失败（是否以管理员身份运行？）" -ForegroundColor Red
    exit 1
}

Write-Host "== 2/3 防火墙放行 TCP $port，来源限 $lanCidr" -ForegroundColor Cyan
netsh advfirewall firewall delete rule name="MQTT Broker $port" 2>$null | Out-Null
netsh advfirewall firewall add rule name="MQTT Broker $port" dir=in action=allow `
    protocol=TCP localport=$port remoteip=$lanCidr profile=any
if ($LASTEXITCODE -ne 0) {
    Write-Host "防火墙规则添加失败" -ForegroundColor Red
    exit 1
}

Write-Host "== 3/3 当前规则" -ForegroundColor Cyan
netsh interface portproxy show v4tov4
Write-Host ""
netsh advfirewall firewall show rule name="MQTT Broker $port" | Select-String -Pattern 'Rule Name|Enabled|LocalPort|RemoteIP'

# 取默认网关所在网卡的 IP，用来给出可直接复制的验证命令
$lanIp = (Get-NetIPConfiguration |
          Where-Object { $_.IPv4DefaultGateway -ne $null } |
          Select-Object -First 1).IPv4Address.IPAddress

Write-Host ""
Write-Host "完成。验证方法：" -ForegroundColor Green
Write-Host "  1) 在**另一台**局域网设备上测（手机装个 MQTT 客户端最方便），"
if ($lanIp) {
    Write-Host "     连 $lanIp : $port"
}
Write-Host "  2) 本机看端口监听在哪个地址："
Write-Host "     netstat -ano | findstr :$port    # 期望 0.0.0.0:$port 而不是 127.0.0.1:$port"
Write-Host ""
Write-Host "★ 别忘了同步改**设备侧**的 broker 地址（固件 CONFIG_MQTT_BROKER_URI）" -ForegroundColor Yellow
if ($lanIp) {
    Write-Host "   本机当前局域网 IP = $lanIp  →  mqtt://${lanIp}:$port" -ForegroundColor Yellow
}
Write-Host "   改完必须重新编译烧录才生效。" -ForegroundColor Yellow
