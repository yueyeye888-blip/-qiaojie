# 备份范围与私密材料

## 公开仓库

包含当前应用源码、Windows 模板、静态页面、测试、原部署/验收记录（地址和试点 ID 已脱敏）、无秘密配置示例、恢复说明、校验器。未包含此前失败的 UTM/Ubuntu 试验目录 `bluetooth_bridge/`，因为它不是现有方案的运行依赖。

`backup/source-provenance.json` 记录项目文件与正式用户安装目录的 SHA256 对比，以及公开副本是否脱敏。测试、示例配置、Git 忽略文件原本不在用户安装目录，不应把这些 missing 标记解释为运行文件丢失。受限 root 副本未提权读取。

## 本地私密快照

`tools/private_backup.py` 单独执行，输出权限 0600 的 tar.gz，父目录权限 0700。**归档未加密**，不得上传公开仓库、发聊天或放在公开网盘。应另外保存到受保护的离线/加密存储；仅留在这台 Mac 上不具备硬盘损坏后的灾备能力。

默认记录：
- 正式用户安装目录的代码、配置与可读历史文件；跳过锁、socket、SQLite WAL/SHM 原始文件。
- 使用 SQLite online backup API 创建一致的 `runtime/bridge.sqlite3`，做 integrity_check；不是简单复制活动 DB 文件。
- 管理身份与接入秘密按原始字节保存在私密归档，不输出内容。
- 可读取的用户/系统 launchd 文件、源端隔离标记、当前签名厂商应用包，以及管理员文件的缺项记录。
- 不读取钥匙串，不读取 Tailscale 状态私钥目录，不登录 Windows 或云平台，不请求 sudo。

业务数据库是某一时刻的一致快照，配置/其他文件不是跨进程全局原子快照；工具检查复制期间配置与身份文件摘要是否变化。运行中的租约和任务状态可能在采集后继续变化。恢复不能直接把旧租约视为仍有效。

## 本次不能从 GitHub 恢复的材料

| 材料 | 恢复方式 |
| --- | --- |
| 管理令牌、enrollment_secret、agents 身份哈希与原数据库 | 同机恢复用私密归档，或重新部署/登记；不能凭哈希恢复 Windows 明文身份 |
| 阿里云 AccessKey/Secret | 原 Mac 钥匙串保持不动；换 Mac 从密码管理器取回或重新签发 |
| Telegram Token、本人私聊 Chat ID | 原钥匙串保持；换机重新填写/配对并发送测试 |
| Tailscale OAuth Client ID/Secret | 原钥匙串保持；换机重新配置；原节点状态不复制 |
| VirtualHere License、root ini/manifest 与管理员历史备份 | 本次普通用户不可读的列入缺项；同机保留原件；完整系统恢复依靠另外的加密系统备份或管理员私下保存 |
| Windows 的 agent.json、Tailscale 状态、VirtualHere 客户端配置 | 本次未访问远端，不包含；原机保留，换机重新登记 |
| 监测服务器 DPAPI 加密凭据 | 仅原 Windows 安装可用；换服务器签发新的单次授权并重装监测 |

钥匙串定位（仅服务名称，不是密钥）：账户 `bluetooth-bridge-controller`；`com.lumen.bluetoothbridge.aliyun.credentials`、`com.lumen.bluetoothbridge.telegram.credentials`、`com.lumen.bluetoothbridge.tailscale.client-id`、`com.lumen.bluetoothbridge.tailscale.client-secret`。

本备份不是完整磁盘/虚拟机镜像，不包含手机通行密钥或网站登录状态。不能宣称“一次 clone 自动恢复全部凭据与硬件”。
