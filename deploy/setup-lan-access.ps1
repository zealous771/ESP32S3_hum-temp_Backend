# 让局域网里的 ESP32 能访问跑在 WSL2 里的 MQTT broker
#
# ★ 必须以管理员身份运行 PowerShell，否则 netsh 会报
#   "The requested operation requires elevation (Run as administrator)."
#
# 用法：右键 PowerShell → 以管理员身份运行，然后执行
#   cd C:\Users\mgnwo\Desktop\wrok_place\ESP32_project\ESP32_learn\deploy
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
#   - 已有的 portproxy 规则是 8000-8003，本脚本只增加 1883。

$ErrorActionPreference = 'Continue'

# WSL 的固定辅助地址。改这个值要同时改 WSL 里的 wsl-fixed-ip.service。
$wslIp = '172.22.120.100'

# 局域网网段。必须**读实际子网掩码**，不要凭 IP 猜：
# 比如 IP 是 10.158.241.128 而掩码是 255.255.254.0（/23）时，
# 网段横跨两个 C 段，按 /24 写会让同网段内的设备被防火墙静默丢弃。
$lanCidr = '192.168.31.0/24'

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

Write-Host ""
Write-Host "完成。验证方法：" -ForegroundColor Green
Write-Host "  Test-NetConnection 192.168.31.128 -Port 1883    # 期望 TcpTestSucceeded : True"
Write-Host "  netstat -ano | findstr :1883                    # 期望看到 0.0.0.0:1883 而不是 127.0.0.1:1883"
Write-Host ""
Write-Host "调试建议：先确认全链路能通，再把 remoteip 收窄到实际网段。" -ForegroundColor Yellow
