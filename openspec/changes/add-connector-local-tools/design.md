# Design: add-connector-local-tools

> 接续 `add-nanobot-connector`（v1 只读文件连接器）。本文为 v2 受控执行的规格化摘要与决策记录。

## Context

v1 交付的连接器（`nanobot/connector/`：protocol/hub/devices/transfer + Agent `connector_*` 工具 + `connector/` 客户端子项目）建立了反向 WSS 长连接、配对/令牌、按会话归属隔离与只读文件通道。已有可复用基础：

- 协议层 `protocol.py` 用 `type` 判别帧、`extra="allow"` 向前兼容、`PROTOCOL_VERSION` 注册协商——v1 注释即已声明"为 v2（watch/resumable fetch/exec）留扩展位"。
- `ConnectorHub`（进程级单例）已实现 `rpc()`（id 关联 + 超时 + 断连清理 pending future）、`fetch_file()`（流式 `file_chunk` 组装 + `cancel` 帧）——执行路由与流式输出可沿用同一套 request-id + queue 机制。
- `DeviceStore` 已有归属用户（owner_id）、机器指纹、令牌吊销、原子持久化——授权模型在其上扩展。
- v1 `ConnectorConfig` 已含 `allow_exec: bool = False`（注释："v2 reservation; v1 never honors it"）。

约束：延续 v1——Python 3.11+ 全 asyncio、不引入新重量级依赖、用户电脑无公网 IP/不可开监听端口、服务端请求一律视为不可信。

## Goals / Non-Goals

**Goals:**

- 设备主人在本机**声明式登记**可被远程调用的工具后，Agent 能枚举并调用它们，实时获取 stdout/stderr 与退出码。
- 命令绝不由服务端构造：服务端只能按 `name` + 结构化参数调用已登记工具，参数经模板/白名单校验。
- 每次执行受"授权（谁能调）+ 审批（是否需人工放行）"双层控制，设备主人可随时收回授权、终止执行。
- 资源可控：并发数、单次超时、输出字节上限；执行链路双端审计。
- 默认关闭（`connector.allowExec: false`），对存量部署与 v1 用户零影响。

**Non-Goals:**

- 服务端下发任意 shell 命令 / 交互式远程终端（这等同远程后门，永不提供）。
- 桌面 GUI 控制：截屏、键鼠注入、窗口操作（v3 `add-connector-desktop-control`）。
- 本地 MCP server 代理（v2.5 `add-connector-mcp-proxy`）。
- 长驻后台进程/服务托管、进程间流水线编排（本变更只做"启动 → 运行至结束/取消 → 收集输出"的一次性执行）。
- 文件写入/删除（仍在 v1 只读边界内；如需产物落盘，工具自行写本地，再经 `connector_fetch_file` 拉取）。

## Decisions

1. **声明式工具注册表，而非命令直通**
   备选：服务端下发任意 `argv`/shell 字符串。后者等于把每台设备变成远程 shell，一旦服务端被攻破即全网沦陷，与"设备主人主权"根本冲突。改为设备主人在本机 `tools.json` 显式登记工具（`name` + `exec` 路径 + 参数模板），服务端只能引用 `name` 并传结构化 args。这是 v2 全部安全性的基石。

2. **参数模板 + 白名单校验，杜绝注入**
   工具定义用参数模板声明每个入参（类型、是否必填、枚举/正则约束、是否可多值）。客户端渲染为 `argv` 列表（**从不经过 shell**，直接 `subprocess` exec 数组形式），未声明的参数一律拒绝。禁止 `shell=True`、禁止参数里出现路径穿越到未授权目录。

3. **复用 request-id + queue 的流式机制承载执行输出**
   `tools.call` 复用 `fetch_file` 已验证的模式：一个 `rpc_id` 关联一次执行，客户端以 `exec_output` 帧增量回传 stdout/stderr（带 `stream` 与 `seq`），以 `exec_result` 帧终止（退出码/耗时/是否被截断/是否超时）；发起方取消时下发既有 `cancel` 帧，客户端 kill 子进程树。执行独立超时 `execTimeoutS`，不复用 `rpcTimeoutS`。

4. **协议 v2 加法式升级 + 注册协商降级**
   `PROTOCOL_VERSION` 1→2。`NodeInfo` 新增 `capabilities`（如 `["fs", "exec"]`）声明本连接器支持的能力集。服务端按交集决定可用方法：v1 客户端（protocol=1，无 exec 能力）连 v2 服务端时，`connector_call_tool` 对该节点返回"设备不支持执行"；v2 客户端连 v1 服务端时执行方法不可用。`extra="allow"` 保证新字段不破坏旧解析。

5. **双层控制：授权（authorization）与审批（approval）正交**
   - **授权**（静态、粗粒度）：`DeviceStore` 记录"哪些工具允许被哪些主体调用"。默认仅设备归属主人的会话可调用其设备工具；多用户共享设备需主人显式授予（per-tool 授予/吊销）。未授权调用返回等同 `not_found`（不泄露工具存在性）。
   - **审批**（动态、每次执行）：每个工具带 `approval` 策略——`auto`（授权内直接执行）、`webui`（服务端弹审批卡片，须 WebUI 用户放行）、`local`（连接器本机托盘/CLI 弹确认，须设备主人在本机放行）。`local` 优先级最高，是设备主人对高危工具的最后闸门。

6. **设备主人主权（device owner sovereignty）**
   设备工具的定义权、授权权、审批权、终止权全部归设备本机所有者。服务端/其他用户只能"请求执行"，不能新增工具、不能提升自己的授权、不能绕过 `local` 审批。收回授权与 kill 正在执行的进程即时生效。这是多用户/跨他人电脑场景的信任前提。

7. **资源上限与背压**
   `maxConcurrentExecs`（每节点并发执行上限）、`execTimeoutS`（单次超时，默认 300s）、`maxExecOutputBytes`（输出累计上限，默认 1MB，超限截断并置 `truncated`）。超并发返回可读错误引导重试；超时下发 `cancel` 并 kill。防止一次失控执行拖垮节点或灌爆网关内存。

8. **审计为强制项，不可关闭**
   执行事件（发起会话/用户、node_id、工具名、参数摘要（敏感值脱敏）、审批方式与审批人、退出码、耗时、字节数、结果）在服务端与连接器本机各记一份滚动日志。这是事后追责与安全评审的依据，且 `local` 审批弹窗内容即取自同一结构。

9. **设备定向：别名 + 会话默认绑定，禁止"任选一台"**
   多设备场景下 Agent 必须明确目标。设备记录支持设备主人设置可读别名；`connector_call_tool` 的 `node_id` 为必填，`connector_list_nodes` 返回别名供选择；会话可绑定默认设备（用户设定）。未指定且无默认时工具返回"请先指定设备"的可读错误，绝不隐式任选——"在错误的电脑上执行"是不可接受的故障模式。

10. **操作者与设备主人分离，跨人调用需显式同意**
   区分两个角色：**操作者**（与 nanobot server 对话、发起工具调用的人）与**设备主人**（连接器所在电脑的所有者）。二者相同即"自用"（v1 归属隔离已覆盖）；不同即"跨人使用"，是本项目核心多用户场景。跨人访问须经设备主人在 WebUI/本机**接受访问请求或主动邀请**授予（可细到 per-tool、可限时），并对"当前谁在用我的设备、用了哪些工具"实时可见、可随时收回。凭据、审批、终止权始终留在设备主人侧（决策 6 主权原则的多用户延伸）。

11. **工具凭据只存设备本机，协议永不传输凭据实体**
   真实工具常需 token/密钥/敏感路径。`tools.json` 只声明凭据的**引用**（环境变量名/本机凭据条目 id），实体由设备主人在本机配置，执行时由连接器注入子进程环境；协议帧、服务端、审计日志中一律只出现引用名与脱敏摘要，绝不出现明文凭据。既保证工具可用，又不把凭据暴露给服务端或其他操作者。

12. **审批 TTL 到期默认拒绝；执行限流防滥用**
   `webui`/`local` 审批设 `approvalTtlS`（默认 120s），无人响应即按拒绝处理并记审计——默认安全而非默认放行。另设 per-session 与 per-device 执行速率限制（`execRatePerMinute`），命中返回可重试错误并记指标，防止被攻破的服务端或失控 Agent 高频触发。

13. **稳定错误码贯穿协议/网关/工具三层**
   沿用 v1 错误码风格，新增 `exec_denied`（未授权）、`approval_denied`、`approval_timeout`、`tool_not_found`、`invalid_args`、`exec_unsupported`（设备无 exec 能力）、`exec_limit`（超并发/限流）、`exec_timeout`、`exec_cancelled`。三层共享同一套码，工具层据此映射为面向 LLM 的可操作文案（决策见 connector-tools 规格），消除歧义。

## Config Additions

在 `ConnectorConfig` 新增（camelCase 别名，保守默认）：

- `maxConcurrentExecs`（每节点并发执行上限，默认 2）
- `execTimeoutS`（单次执行超时，默认 300）
- `maxExecOutputBytes`（输出累计上限，默认 1MB，超限截断）
- `approvalTtlS`（审批等待超时，默认 120，到期默认拒绝）
- `execRatePerMinute`（per-session/per-device 执行速率上限，默认 30）

## Risks / Trade-offs

- [远程执行是最高危能力] → 命令不由服务端构造（决策 1）、参数模板白名单（决策 2）、双层授权审批（决策 5）、设备主人主权（决策 6）四重防线叠加；`allowExec` 默认 false，需专项渗透测试门禁方可 GA。
- [参数模板校验绕过导致注入] → 一律 exec 数组、禁 `shell=True`、参数值经类型/枚举/正则校验、路径参数强制落在工具声明的允许目录内；渗透测试覆盖注入矩阵。
- [被攻破的服务端反复触发高危工具] → `local` 审批策略（决策 5）把最终放行权留在设备本机；授权可即时收回；审计告警异常调用频率。
- [长时间/大输出执行拖垮节点或网关] → 资源上限与背压（决策 7）。
- [子进程未被彻底终止（僵尸/子孙进程）] → kill 进程组/进程树（Windows `CREATE_NEW_PROCESS_GROUP` + taskkill /T；POSIX `setsid` + killpg）；`cancel` 与超时都走同一终止路径。
- [审批疲劳导致用户无脑放行] → 审批卡片展示工具名+参数摘要+发起上下文；高危工具建议默认 `local`；`auto` 仅适用于设备主人自用且低危的工具。
- [v1/v2 混部的能力误判] → 注册协商 + `capabilities` 交集（决策 4），工具层对不支持执行的节点返回明确文案而非静默失败。
- [在错误的设备上执行] → `node_id` 必填 + 别名 + 会话默认绑定，禁止隐式任选（决策 9）；审批卡片展示目标设备别名供二次核对。
- [跨人使用被滥用/未经同意控制他人电脑] → 操作者与设备主人分离 + 显式同意授予 + 实时可见 + 随时收回（决策 10）；设备主人始终握有审批与终止权。
- [工具凭据经服务端泄露] → 凭据只存本机、协议只传引用、审计脱敏（决策 11）；渗透测试覆盖"凭据是否出现在任何服务端可见面"。
- [审批疲劳/无人响应阻塞] → `approvalTtlS` 到期默认拒绝（决策 12），不无限期挂起，不默认放行。
- [失控 Agent/被攻破服务端高频执行] → per-session/per-device 限流（决策 12）+ 审计告警异常频率。

## Migration Plan

1. 协议与服务端随常规版本发布，`connector.allowExec` 默认 false —— 行为与 v1 完全一致，执行方法不注册。
2. v2 连接器客户端独立发版；用户升级后用 `tool add` 登记工具，但只有服务端也开启 `allowExec` 且完成授权后才可被调用。
3. 试点实例开启 `allowExec`，用一个低危工具（如"打开记事本"/"运行只读诊断脚本"）验证 list→授权→审批→执行→取消→审计全链路。
4. 回滚：关闭 `connector.allowExec`（执行工具不再注册、`tools.call` 路由拒绝），文件能力与 v1 不受影响；客户端 `tools.json` 保留但不被调用。
5. 安全评审 + 渗透测试通过后 GA，再 `openspec archive add-connector-local-tools`。

## Open Questions

- 授权粒度：per-user 还是可细到 per-session/per-conversation？→ 倾向 per-user + 可选会话级临时授权，S2 设计评审定。
- `auto` 策略是否应仅在"设备主人本人的会话调用自己设备"时才允许？→ 倾向是（跨用户调用强制至少 `webui`），安全评审确认。
- 审批超时（无人响应）默认拒绝还是排队？→ 倾向默认拒绝 + 可配置 TTL。
- 工具产物如何回传：依赖工具自写本地 + `connector_fetch_file`，还是执行结果里直接带产物路径？→ v2 先用前者（复用 v1 通道），观察反馈。
- 是否需要"工具组/预设"批量授权？→ 灰度反馈决定，暂不进 v2。
