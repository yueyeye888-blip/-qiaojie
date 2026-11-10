# 换服务器与重建部署

以下是将来人工执行的流程，本次制作备份没有执行安装、停机或 USB 切换。先读恢复手册，确认要保留身份还是重新登记。

## 1. 保留 Mac，只换目标云电脑

1. 保留 Mac 正式安装、源服务、钥匙串和 Tailscale，不运行首次配置程序。
2. 在原管理台结束旧目标并等待释放确认；尚未确认时不把同一 USB 交给新目标。
3. 新电脑需要受支持的 Windows x64、管理员安装权限、能运行阿里云远程命令、能访问官方软件源及 Mac Tailscale 通道。当前代码面向阿里云 ECD，不是任意云厂商的一键安装器。
4. 通过管理台刷新目录，先单台“首次接入”。后台创建每台独立身份并执行固定模板；不要复制另一台 `agent.json` 或 Tailscale 状态。
5. 已经登记的原机选择“更新/修复代理”，不能通过删除身份绕过安装保护。
6. 等待正确版本心跳，再接管、释放并做手机实际验证；通过后再逐批扩大，默认每批 3 台。

非阿里云 Windows 主机可人工调用 `windows/bootstrap.ps1`，但 `ControllerBase`、一次性入网与登记授权、ComputerName、AgentSha256 必须由当前控制器/授权流程生成；不能复用历史命令，也不要把带参数的命令提交到仓库。本备份没有为任意云厂商实现完整目录/批量管理适配。

## 2. 独立监测节点

1. 新 Windows 节点先用独立身份加入同一允许访问的 Tailscale 网络，确认可长期在线。
2. Mac 为该节点实际 Tailscale IPv4 签发新的十分钟、单次 enrollment；过期或地址不符就重新签发，不关闭检查。
3. 取得当前 `/agent/watchdog/install.ps1` 并验证 SHA256，以管理员 PowerShell 运行；必须显式传 `-MacAddress <当前 Mac Tailscale IPv4>`。公开副本的默认地址只是示例。
4. 安装器设置无人值守、SYSTEM 计划任务并用本机 DPAPI 保护通知凭据。不要把旧服务器的密文复制过来。
5. 手机确认启动通知。经批准的维护窗口再验证失联/恢复；切换完毕后停用旧监测，防止重复告警。

## 3. 新 Mac 源端：人工重建，不是 Linux 服务器迁移

物理 USB 蓝牙仍需插在提供它的实体主机上。普通云服务器没有这只物理 USB；替换云服务器不能替代 Mac USB 源。新 Mac 的 Tailscale 地址、用户 UID、解释器路径、许可证和各客户端的控制器地址都要重新核对。

### 3.1 前置准备

- 保留旧 Mac 和旧客户端；新旧不能同时以相同身份提供同一恢复环境。
- 安装 Apple Command Line Tools，使固定的管理员解释器路径存在，确认 macOS/芯片与厂商软件兼容。
- 从官方安装 Tailscale 并加入合适网络；入站只允许所需节点访问 7575/8767，不开放原始源端口 7576 或本地管理口 8766。不要改变默认出口路由来解决桥接问题。
- 安装兼容的 VirtualHere Mac Server，确认签名和授权。硬件更换涉及许可证，不把原许可当成可随意迁移。
- 确认外置适配器确实是 10d7:b012；当前 root 组件固定校验该型号，换其他型号需要代码适配和验收。
- clone 本仓库并 checkout 保存的基线 SHA，执行 `python3 tools/verify_backup.py`。

### 3.2 创建本地配置

复制 `bluetooth_bridge_controller/config.example.json` 为同目录 `config.json`，权限 0600；填写新 Mac 实际 Tailscale IPv4 与 Hub。保留回环 UI、`runtime_dir=runtime`、`bulk_validation_hold=true`。**不要把旧环境的 bulk_ready 或历史验收当作新环境验证。**

原 `setup_local.py`/`install_source_isolation.py` 是对“已有、已许可 VirtualHere launch daemon”的升级器，不是空白 Mac 的完整 USB 服务安装器。空白 Mac 若不存在服务定义/许可证，会失败；不能靠反复双击修复。

### 3.3 重建基础 USB 源服务（管理员人工审核）

仓库 `templates/` 提供三个明确标为 example 的模板。它们没有许可证，不能直接使用：

1. 创建 root 拥有且不可被普通用户写的 `/Library/Application Support/BluetoothBridge` 和 `backups`（backups 0700）。
2. 将 `virtualhere.ini.example` 中 License 占位符换成该硬件合法许可证，只写入本机 root 可读的 `virtualhere.ini`，权限 0600。仅共享 10d7/b012、监听 127.0.0.1:7576、禁用广播和反向客户端。
3. 校验官方 app 签名，将审核后的 plist 写入 `/Library/LaunchDaemons/com.virtualhere.vhusbd.plist`，root:wheel、0644。此阶段使用原 `/Applications/VirtualHereServerUniversal.app` 路径；将来由已有受限组件安装器复制并固定完整签名包。
4. 在明确的安装维护窗口加载该服务，验证它只监听回环 7576，并核对实际外置设备。不要仅凭文件存在认定隔离完成。
5. 验证后创建 root 拥有、0644 的 `source-isolation.json`，使用模板实际字段，并填写真实安装时间。
6. 在本地 `config.json` 中启用 `usb_proxy_enabled=true`（后台端口 7576、代理 7575），保持 `source_reset_enabled=false`、批量暂停。USB 原始服务已隔离，不能再套用预期旧端口的初始化升级流程。

所有管理员安装动作都应在新 Mac 人工执行；本仓库不包含绕过本机管理员授权的凭据。

### 3.4 安装用户后台与受限组件

在**新 Mac、已完成上述基础服务**后，从 `bluetooth_bridge_controller/` 运行：

```sh
python3 install_login_service.py --apply
```

它把用户代码复制到 `~/Library/Application Support/BluetoothBridgeController`，创建当前用户登录服务。确认登录域存在、8766/8767/7575 正确绑定，没有第二个后台。`launch.py` 打开本地控制台。

然后使用正式目录中的 `enable_source_reset.py`（普通用户启动，本机 sudo 授权管理员安装器），或对应 `安装自动复位组件.command`。该步骤要求源服务、root 目录/ini、许可证、厂商签名、固定解释器和空闲状态都符合预期；生成新 UID、摘要和路径相关 manifest，不要复制旧 manifest。它会重启源服务，必须安排维护窗口。

### 3.5 外部服务与目标迁移

- 用 `setup_tailscale_oauth.py` 在新 Mac 隐藏输入 OAuth 凭据。Tailscale 策略需预先定义 `tag:wuying` 的拥有者，OAuth 只给对应标签的 Auth Keys 写权限；没有标签不能直接完成自动入网。策略应按组织实际网络审核，不照搬全网放行。
- 在本地管理台连接设置保存受限 RAM 和 Telegram；权限模板是 `ram-policy.json`。RAM `RunCommand` 具有远程执行能力，不是云厂商级的单台限制。
- 重新登记新目标；旧 Windows 的 `poll_url`、Hub 与身份仍指向原控制器，不能假设新地址自动生效。
- 若必须保留原代理身份，使用私密状态迁移并明确更新原客户端配置/网络策略；现有 `repair.ps1` 会检查控制器地址，不能用普通修复自动迁移到另一个控制器。
- 独立监测指向新 Mac，重新签发绑定该监测节点的新 enrollment。

## 4. 上线前验收

按 `VALIDATION.md` 先单台，再第二台切换；重新验证未授权拒绝、释放、到期与告警。批量保护不会因 clone 仓库而解除；只有确认当前环境真实通过后，才按当前管理流程解除人工 hold。不要为了出现绿色状态直接改数据库或伪造 ready。

原临时切换验收工具有固定 `PILOTS` 映射；公开备份已换为示例 ID。在新环境必须审核实际 agent ID/desktop ID 后修改**工具副本**，不删除身份校验、不直接在旧生产环境执行。
