# Tasks: add-connector-desktop-control

## 0. 前置依赖

- [ ] 0.1 确认 `add-connector-local-tools`（v2）已归档，其协议 v2、授权、操作者-主人分离、审计地基可用；桌面审批在其上收紧（取消 `auto`）

## 1. 协议与配置

- [x] 1.1 `nanobot/connector/protocol.py`：新增 `desktop.session.start`/`desktop.session.end`/`desktop.capture`/`desktop.input` 方法、`desktop` 能力、`DESKTOP_ACTIONS` 白名单、桌面错误码（desktop_unsupported/session_inactive/session_ended/out_of_bounds/no_permission/sensitive_unconfirmed）
- [x] 1.2 `nanobot/config/schema.py`：`allow_desktop_control` + `desktopMaxFps`/`desktopMaxDimension`/`desktopSessionMaxS`/`desktopIdleTimeoutS`/`desktopRecordingRetentionDays`（camelCase）
- [x] 1.3 协议/配置单元测试：方法集、能力协商、动作白名单、错误码、配置默认与别名

## 2. 客户端桌面模块（connector-client / connector-desktop）

- [x] 2.1/2.2 `nanobot_connector/desktop.py`：可注入捕获/注入后端（真实后端 mss/pynput 惰性导入）、分辨率上限与帧率节流、显示器尺寸
- [x] 2.3 会话生命周期：本机授权闸门（fail-closed，守护进程经 arm 时间窗 `arm desktop --for` 提供本机同意）、捕获指示 hook（守护进程打印醒目指示）、会话外禁止捕获/注入、会话 id 校验；断连即结束（client `_serve` finally）
- [x] 2.4 系统级权限：`available()` 缺权限时 `no_permission` 优雅拒绝，不绕过；`desktop enable/disable` CLI 本机开关
- [x] 2.5 客户端测试：会话外拒绝、授权 fail-closed、越界坐标拒绝、未知动作、无权限降级、client dispatch（`connector/tests/test_desktop.py`，13 例）

## 3. 服务端桌面会话编排（connector-gateway / connector-desktop）

- [x] 3.1 `ConnectorHub.desktop_rpc`：桌面方法路由 + `desktop` 能力校验（复用 request-id/超时/断连清理）
- [x] 3.2 `nanobot/connector/desktop.py` `DesktopSessionManager`：会话生命周期、动作类型白名单校验（坐标屏内校验在客户端）
- [x] 3.3 敏感动作识别（`is_sensitive_action`：支付/确认/删除/授权/密码启发式 + 显式标记）+ webui 二次确认（复用 broker，TTL 默认拒）；**无 `auto`**
- [x] 3.4 会话资源上限：最长时长 + 空闲超时（越界即结束）；帧率上限在客户端
- [x] 3.5 逐动作审计（`desktop-audit.log`：动作/参数摘要/是否敏感/是否确认/截图引用/结果）；帧默认不落盘
- [x] 3.6 跨人授权（`desktop:control` grant，未授权隐藏）；录制留存（`record=True` 才落盘 + `cleanup_recordings` 到期清理 + 手动删除）
- [x] 3.7 集成测试：授权→捕获→注入→接管→终止；敏感拦截/确认后放行；空闲超时；跨人授权；不支持设备；帧不落盘/录制落盘/到期清理（`tests/connector/test_desktop.py`，12 例）

> 设计偏差：computer-use 闭环由 **Agent 多模态循环驱动**（截图作为工具结果回模型 → 下一动作），而非服务端另起 LLM 循环——复用 Agent 已有多模态能力，避免重复集成。

## 4. Agent 工具（connector-tools）

- [x] 4.1 `connector_desktop_session`/`connector_desktop_act`/`connector_desktop_end`，受 `enabled`+`allowDesktopControl` 双开关注册（独立于 allowExec）
- [x] 4.2 错误映射：不支持桌面/未授权/会话结束（超时/接管/终止）/越界/无系统权限/敏感未确认
- [x] 4.3 工具测试：双开关注册、首屏/下一屏返回、错误映射、无 manager 降级（`tests/agent/tools/test_connector.py`）

## 5. WebUI 桌面控制（connector-webui）

- [x] 5.1 设备控制中心"本地工具"标签新增"桌面控制会话"区（发起者/目标/时长/录制标记 + 接管/终止），经 `/api/connector/desktop-sessions` 404 探测显隐
- [x] 5.2 敏感动作确认复用 v2 `webui` 审批横幅（`ConnectorApprovalsBanner`）
- [x] 5.3 逐动作审计回放（`/api/connector/desktop-audit` + WebUI 最近动作列表，敏感/确认标记）+ 录制列表与手动删除（`/api/connector/desktop-recordings` + `desktop-recording-delete`，归属校验 + 防穿越 + 删除审计）。**实时画面流式预览**仍为后续增量（截图当前经 Agent 多模态循环消费；浏览器端 live 流需 WS 多路复用，独立追加）
- [x] 5.4 i18n +4 键覆盖 10 语言包；WebUI 组件测试（桌面会话展示 + 接管）

## 6. 收尾

- [x] 6.1 端到端：授权（设备同意）→捕获→注入→接管(HTTP)→审计；设备拒绝同意；敏感确认回环（`tests/connector/test_desktop_e2e.py`，3 例）
- [x] 6.2 隐私：帧默认不落盘（测试断言）、捕获指示 hook、结束/断连即停、录制到期清理；跨人在场同意（设备端 fail-closed）
- [x] 6.3 文档：用户手册"受控桌面控制"、运维手册"allowDesktopControl 治理/隐私/存储/Runbook"
- [x] 6.4 `ruff check nanobot/ connector/` 零告警；新增代码单测覆盖率 ≥ 80%
- [ ] 6.5 隐私影响评估 + 渗透测试（会话外注入/绕过授权/绕过敏感确认/坐标越界/帧泄露/系统权限绕过）通过后 `openspec archive add-connector-desktop-control` —— **外部门禁，待评审 + v2 先归档**
