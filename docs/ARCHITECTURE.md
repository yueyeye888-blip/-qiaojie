# 架构、版本与已实现边界

## 数据路径

```text
外置 USB 蓝牙 10d7:b012（Actions / general adapter）
  → Mac 原生 VirtualHere USB Server
  → 127.0.0.1:7576（原始 USB 服务，不对网络开放）
  → Python UsbProxy（Mac Tailscale IPv4:7575，按租约授权）
  → Tailscale 网络（直连或中继取决于当时网络，不在备份中预设）
  → Windows VirtualHere Client → Windows USB/蓝牙驱动 → 浏览器/手机验证
```

本地管理网页 `127.0.0.1:8766`，云电脑代理与最小健康 API 为 `Mac Tailscale IPv4:8767`。独立 Windows 监测服务器只检查 Mac 健康并通知 Telegram，不是 USB 中转。阿里云 ECD API 负责目录、唤醒/启动与内置远程脚本。UTM、Ubuntu、独立 WireGuard 服务和固定网络出口不属于这条已验证路径。

## 历史验证环境与版本

| 项目 | 基线资料 | 迁移要求 |
| --- | --- | --- |
| Mac | Apple M4、16GB、macOS 15.3 / 24D2059（此前只读观察） | 不代表其他版本已实机验证 |
| Python | Apple Command Line Tools Python 3.9.6；业务代码仅用标准库 | 管理员组件固定检查 `/Library/Developer/CommandLineTools/usr/bin/python3`，不能随意换为用户可写解释器 |
| VirtualHere Mac Server | 4.8.8 | 从官方重新取得；安装器验证厂商签名与 Team ID；许可绑定硬件 |
| Tailscale Mac | 1.104.5（此前本机观察） | 新 Mac 需独立入网，确认地址与策略 |
| Windows | 两台试点历史为 Windows Server 2025 / build 26100，PowerShell 5.1 | 不能据此保证所有 Server 镜像都具备相同蓝牙支持，逐台验收 |
| Tailscale Windows 安装器 | 模板固定 1.104.5 amd64 MSI，检查签名 | URL 后续不可用时人工审核新版本并重新验收 |
| VirtualHere Windows Client | 历史试点 6.0.3；模板下载官方动态 URL | 动态 URL 不保证将来仍是同一版本；应核对签名与版本 |
| USB | VID/PID 10d7:b012，名称 general adapter | 零售型号、固件和蓝牙版本未确认；不能只看名称买同款 |

代码与第三方二进制不构成完全封闭、离线可重建的镜像。未归档第三方安装包、OS、驱动或云磁盘；获取不到兼容版本时可能需要适配。

官方来源：
- https://www.virtualhere.com/osx_server_software
- https://www.virtualhere.com/usb_client_software
- https://www.virtualhere.com/purchase
- https://pkgs.tailscale.com/stable/
- https://tailscale.com/download

## 安装位置

| 内容 | 正式位置 |
| --- | --- |
| 用户后台/身份/数据库 | `~/Library/Application Support/BluetoothBridgeController/`，其中 `config.json`、`runtime/` 是私密状态 |
| 用户登录服务 | `~/Library/LaunchAgents/com.lumen.bluetoothbridge.controller.plist` |
| 用户日志 | `~/Library/Logs/BluetoothBridge/controller.log` |
| USB 源服务 | `/Library/LaunchDaemons/com.virtualhere.vhusbd.plist` |
| 管理员复位服务 | `/Library/LaunchDaemons/com.lumen.bluetoothbridge.source-reset.plist` |
| 管理员保护目录 | `/Library/Application Support/BluetoothBridge/` |
| 源服务/复位日志 | `/var/log/vhusbd.log`、`/var/log/bluetooth-bridge-reset.log` |
| Windows 桥接 | `%ProgramData%\BluetoothBridge`；SYSTEM 计划任务 `Mac Bluetooth Bridge Agent`；VirtualHere 客户端服务 |
| 独立监测 | `%ProgramData%\BluetoothBridgeWatchdog`；SYSTEM 任务 `Mac Bluetooth Bridge Watchdog` |

## 已实现

- 一只 USB 一份全局租约；旧目标确认 released/absent 后才允许切换。
- 原始 USB 服务只监听回环，转发按实际来源地址、租约会话、代次、有效期和心跳判断。
- 网页每 10 秒续期；默认租约 900 秒。后台监控独立运行，**关闭网页不等于后台停止，但网页续期会停止**。
- 每台云电脑独立代理身份；首次接入、修复保留身份、逐台批量任务、丢失回执核对。
- 每次接管前的受限源服务复位、保护性的启动恢复、异常代次不自动抢占。
- Telegram 私聊告警、独立监测节点和最小健康 API。

## 尚未实现

多只 USB 并行、按设备授权与独立恢复、外部借用 API、撤销先到的外借持久记录、跨来源目标互斥、关闭网页后由后台持续续租。这些是后续讨论，不能作为本次已实现功能恢复。

当前全局 `acquire(agent_id)` 可发起目标切换，`release()` 释放全局租约，不应直接开放给外部平台。源服务复位为整个服务重启；扩展多适配器前必须处理恢复的影响范围。
