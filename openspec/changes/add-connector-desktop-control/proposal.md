# Proposal: add-connector-desktop-control

## Why

v2/v2.5 让 Agent 能在用户电脑上调用**已登记的程序与 MCP server**，但仍无法操作没有命令行/MCP 接口的图形软件——用户最初举的"打开 QQ 并登录"就属此类。要覆盖任意 GUI 软件，需要 OpenClaw / Claude computer-use 式的"看屏幕 + 动键鼠"闭环：连接器捕获屏幕回传，多模态 LLM 在服务端决策，指令经连接器注入键鼠。这是能力最强、风险也最高的一档，独立成 v3，在 v2 授权/审批/主权/审计地基上加"实时会话式控制"这一层。

## What Changes

- **协议扩展（v2 之上加法）**：新增 `desktop.capture`（请求截屏/屏幕流帧）、`desktop.input`（键鼠事件注入）、`desktop.session.start` / `desktop.session.end`（受控桌面会话生命周期）方法与图像/事件帧。`capabilities` 增加 `desktop`。
- **客户端桌面代理**：连接器新增桌面控制模块——屏幕捕获（全屏/指定窗口、可配帧率与分辨率上限）、键鼠事件注入（跨平台后端）、屏幕坐标与显示器信息上报。所有能力受本机开关与显式会话授权控制。
- **服务端桌面会话编排**：新增受控桌面会话——把屏幕帧喂给多模态 LLM、把模型输出的动作（点击/输入/滚动/快捷键）翻译为 `desktop.input` 事件、维护会话超时与资源上限，全程流式呈现给 WebUI。
- **强制人类在环（human-in-the-loop）**：桌面控制默认要求设备主人显式开启一次"受控会话"（限时），会话内可随时一键接管/终止；敏感动作（输入疑似密码、点击"确认支付/删除"类控件）触发额外确认。默认策略比 v2 更严：不提供 `auto`。
- **Agent/工具侧**：新增 `connector_desktop_session`（在授权设备上开启/驱动一次受控桌面会话）；桌面会话是有状态的多步交互，作为长任务型工具接入。
- **WebUI**：设备控制中心新增"桌面控制"标签——实时画面预览、会话状态、接管/终止按钮、敏感动作确认、逐动作审计回放。
- **隐私红线**：屏幕内容是高度敏感数据。捕获仅在活动受控会话内进行、会话结束即停；帧默认不落盘（仅在显式开启录制时留存并标注，录制按保留期到期自动删除、设备主人可随时手动删除）；捕获期间连接器本机显示醒目指示。跨人（操作者非设备主人）会话须设备主人本机在场同意并留证。
- **开关**：受 `connector.enabled` + 新增 `connector.allowDesktopControl` 控制，默认 false；与 `allowExec` 相互独立（可只开文件+执行而不开桌面）。
- **明确不做**：无人值守的全自动 GUI 操作（始终要求会话授权与可接管）、后台静默截屏、绕过操作系统权限提示（macOS 屏幕录制/辅助功能授权仍需用户在系统层授予）。

## Capabilities

### New Capabilities

- `connector-desktop`: 受控桌面控制——协议 `desktop.*` 方法与图像/事件帧、客户端屏幕捕获与键鼠注入、服务端多模态会话编排、人类在环授权/接管/敏感动作确认、跨人在场同意、录制留存/删除策略、逐动作审计与隐私保护。

### Modified Capabilities

- `connector-tools`: 新增 `connector_desktop_session` 长任务型工具，受 `connector.allowDesktopControl` 开关控制，沿用归属隔离。
- `connector-gateway`: Hub 新增桌面会话路由、屏幕帧转发与键鼠事件下发、会话资源上限与超时。
- `connector-client`: 新增桌面捕获/注入模块、本机会话授权闸门与捕获指示、系统级权限引导。
- `connector-webui`: 控制中心新增"桌面控制"标签（实时预览、接管/终止、敏感动作确认、审计回放）。

## Dependencies

- **依赖 `add-connector-local-tools`（v2）先行**：复用协议 v2、`capabilities` 协商、request-id + 流式机制、per-device 授权、操作者与设备主人分离、设备定向、审计地基。桌面审批策略在 v2 基础上收紧（取消 `auto`）。须在 v2 归档后实施。
- **与 `add-connector-mcp-proxy`（v2.5）无强依赖**：可在 v2 之后独立实施；但 Agent 侧"同一任务优先选最低风险路径"（能用命令/MCP 就不用桌面控制）的策略建议在三者共存后统一设计。

## Impact

- **新增代码**：`nanobot/connector/desktop.py`（会话编排/多模态动作翻译）、`nanobot/agent/tools/connector.py`（`connector_desktop_session`）、连接器子项目 `nanobot_connector/desktop/`（capture/input/backends）、WebUI 桌面控制标签。
- **修改代码**：`nanobot/connector/protocol.py`（`desktop.*` 帧/方法）、`hub.py`（桌面会话路由/帧转发）、`nanobot/config/schema.py`（`allowDesktopControl` 与帧率/分辨率/会话最长时长/空闲超时/录制保留期上限）、devices/授权（桌面会话授权 + 跨人在场同意留证）。
- **依赖**：客户端新增跨平台屏幕捕获与输入注入依赖（如 mss / pynput 或平台原生 API）；服务端依赖多模态 LLM provider（nanobot 已支持）。
- **安全/隐私面**：最高等级。屏幕内容 + 键鼠控制等同完全掌控该电脑。防线：强制会话授权 + 限时 + 可接管终止、无 `auto`、敏感动作二次确认、帧默认不落盘、捕获指示、逐动作审计、系统级权限不被绕过。必须专项隐私影响评估 + 渗透测试门禁方可 GA。
- **运维**：新增桌面会话审计与指标（会话数/时长/接管次数/敏感动作拦截）；文档需明确隐私边界与合规（多用户/受控他人电脑场景的知情同意）。
