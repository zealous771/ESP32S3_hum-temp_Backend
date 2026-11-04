@echo off
rem ============================================================
rem  WSL2 发行版保活（可选，默认不启用）
rem
rem  为什么需要：WSL2 发行版在没有 wsl.exe 客户端连接时会完全停止，
rem  里面跑的 systemd 服务和 Docker 容器随之全部被杀。
rem  实测：静默约 95 秒后 Ubuntu-24.04 变成 Stopped，MQTT broker 一起没了。
rem  表现很迷惑 —— localhost 偶尔能通（那只是恰好有 wsl 命令在执行、
rem  把发行版唤醒了），命令结束约 8 秒后又全断。
rem
rem  本脚本提供一个常驻的 wsl.exe 客户端连接（sleep infinity），
rem  让发行版一直活着。代价是任务栏多一个最小化的控制台窗口，
rem  而且**不能关**——关了 broker 就停。
rem
rem  启用：把本文件复制进
rem    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
rem  （不需要管理员权限）
rem  停用：把该文件移出启动文件夹即可。
rem  注意：只改名留在原地（例如 .disabled）是**不行的**，
rem        Windows 会对不认识扩展名的文件弹「你要如何打开这个文件？」。
rem
rem  容器侧的 restart: unless-stopped 只能救 Docker 守护进程重启，
rem  救不了发行版整体停止 —— 那时 dockerd 自己都没了。
rem
rem  已试过但无效的方案（不要重复踩）：
rem    - .wslconfig 里设 vmIdleTimeout
rem    - conhost.exe --headless
rem    - schtasks 计划任务 / VBS 隐藏窗口
rem ============================================================

start "" /min "C:\Windows\System32\wsl.exe" -d Ubuntu-24.04 -u root -- sleep infinity
exit
