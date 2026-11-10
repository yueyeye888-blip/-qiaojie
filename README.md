# Mac USB 蓝牙桥接：可恢复基线备份

备份基线：**2026-11-10（Asia/Singapore）**。本仓库保存当时已实现的单适配器方案，供换 Windows 云电脑、重建 Mac 源端及重大修改前回滚使用。不是多适配器版，也没有实现外部临时借用接口或关闭网页后无限续期。

## 从哪里开始

| 需求 | 阅读顺序 |
| --- | --- |
| 了解当前方案与依赖 | [架构与版本](docs/ARCHITECTURE.md) → [备份范围](docs/BACKUP_SCOPE.md) |
| 换一台 Windows 云电脑 | [迁移部署](docs/DEPLOYMENT.md) 中“保留 Mac，只换目标” |
| 换监测服务器 | [迁移部署](docs/DEPLOYMENT.md) 中“独立监测节点” |
| 换 Mac、重新搭建 | [迁移部署](docs/DEPLOYMENT.md) 中“新 Mac 源端” |
| 大改前保留现状、之后回滚 | [恢复手册](docs/RESTORE.md) → [验证清单](docs/VALIDATION.md) |
| 出现故障 | [原操作手册](bluetooth_bridge_controller/OPERATIONS.md) |

## 实际包含

- `bluetooth_bridge_controller/`：Python 控制器、租约转发、云任务、告警、Tailscale OAuth、受限源服务复位组件、Windows 安装/修复/诊断/监测脚本、完整前端、自动测试及原说明。
- `templates/`：无凭据的源服务配置、launchd 定义及隔离标记示例，供人工重建审核，不会自行安装。
- `tools/verify_backup.py`：逐文件 SHA256 和可执行权限检查，默认不导入业务代码、不访问服务。
- `tools/private_backup.py`：另存**本地私密**配置、身份、在线 SQLite 一致性快照和可读取的系统组件；不读取钥匙串、不调用云 API、不重启任何服务。
- `backup/`：文件校验清单、源码来源比对、脱敏说明和本次验证结果。

## 完整恢复需要两部分

**公开代码备份 + 私下保存的状态/凭据**。本仓库是公开仓库，不包含 `config.json`、数据库、管理令牌、接入密钥、许可证、云账号密钥、Telegram Token、Tailscale 节点私钥、Windows DPAPI 密文或真实目标目录。

同机回滚尽量保留当前运行身份；换机需要重新配置钥匙串凭据、Tailscale 身份和与硬件绑定的许可。没有私密状态时，可以按文档重新部署并登记云电脑，但不能恢复原身份。Windows 监测节点的 DPAPI 密文不能作为可跨机直接使用的凭据备份。

## 先验证，后安装

```sh
python3 tools/verify_backup.py
cd bluetooth_bridge_controller
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

以上不会执行真实切换验收。不要为“检查备份”双击 `首次配置管理后台.command`、`安装自动复位组件.command` 或 `自动复位切换验收.command`；它们会修改服务或操作 USB。

本仓库不携带第三方商业软件二进制或授权，也未授予第三方软件的再分发权。依赖来源与版本见架构文档。公开副本将少量固定运维地址/试点 ID 替换为示例值，核心控制逻辑保持原样；具体差异见 `backup/REDACTIONS.md` 和来源清单。

保存本次提交的完整 SHA。后续开发使用新分支，不移动基线分支；可下载该 SHA 的源码归档再运行校验，避免把未来的 `main` 当成本次备份。
