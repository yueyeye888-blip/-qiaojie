# 验证与故障排查

## 只验证备份，不触碰生产

从仓库根目录执行 `python3 tools/verify_backup.py`，它只验证归档清单。Python 测试在临时目录/回环临时端口创建假控制器与身份，外部服务经 mock，不访问正式 runtime、不操作 USB。

```sh
cd bluetooth_bridge_controller
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

前端测试需要 Node、Playwright 和本机 Chrome。不是业务运行依赖；可在临时目录安装测试依赖，再通过 `PLAYWRIGHT_MODULE` 指向该模块运行 `tests/ui_smoke.cjs`。测试拦截并本地响应所有请求，使用 101 台假设备，不应向真实控制台发送写请求。输出目录默认 `/private/tmp/bluetooth-bridge-ui`。当前源测试选择 Chrome channel，新机器没有 Chrome 时需安装或人工调整测试工具。

## 将来部署/恢复验收（会操作设备，另安排维护窗口）

1. 检查只有一个用户后台；UI 只回环；代理 API/转发只在正确 Tailscale 地址；原始 USB 只回环 7576。
2. 核对实际外置 USB 数量为 1、VID/PID、签名与当前源服务。Mac 内置蓝牙不属于共享适配器。
3. 核对 source isolation、受限 helper、云权限与通知配置；先单台。
4. 云电脑 fresh 心跳为 released/absent 后发起；等待 authorizing → attached/ready → active。ready 不证明手机验证成功。
5. 手机放在物理适配器附近，人工完成网站通行密钥验证；浏览器、手机、网站支持与本人确认是另一层条件。
6. 结束后等待 released/absent 和 idle；第二台再接管，原目标不能同时继续访问。
7. 测试取消准备、租约到期、代理离线、控制器重启与未授权连接拒绝；这些属于破坏性验收，不能仅为了备份执行。
8. Telegram 手机实际收到测试；独立节点验收需要受控失联/恢复，不能把 Mac 后台的发送成功当作手机送达。
9. 新环境重新确认批量保护与上限，再扩大接入。

## 故障分层

| 现象 | 优先检查 |
| --- | --- |
| 安装 MSI 1603 | 日志中真正失败条件；历史镜像曾禁用 iphlpsvc，模板仅启用必要依赖，不全局放开服务 |
| 后台没有心跳 | Windows 计划任务、agent 日志、Tailscale 与 8767；旧 released 记录不是实时在线 |
| Hub 可见但不能用 | 当前租约、来源身份、实际 USB 地址、客户端是否仍在自动使用；不手工绕过门禁 |
| 同名适配器多个 | 当前实现拒绝不确定设备，不随机挑第一个 |
| 释放一直等待 | 原目标是否在线并卸载 USB；源端 socket 关闭不等于 Windows 完成释放 |
| 接管失败后状态异常 | 先结束并读错误，必要时维护窗口重启固定源服务；不能在其他活跃 USB 上扩大复位范围 |
| 网站验证失败 | 检查手机/网站提示，区分驱动就绪与实际验证 |
| 告警重复或缺失 | 独立节点是否重复、DPAPI 是否属于当前机器、私聊是否已 start、网络是否可达 |

历史实测与本次自动复核分开：原 `TEST_REPORT.md` 记载先前硬件/手机验收；`backup/VALIDATION_RESULTS.md` 只记录本次备份期间实际执行的检查。
