# Design: add-connector-mcp-proxy

> 接续 `add-connector-local-tools`（v2 受控执行）。本文为 v2.5 本地 MCP 代理的规格化摘要与决策记录。

## Context

nanobot 已有成熟 MCP 栈：`MCPServerConfig`（stdio/sse/streamableHttp 三型）+ `nanobot/agent/tools/mcp.py`（连接、工具枚举、名称清洗为 `mcp_<server>_<tool>`、重连、把 MCP 工具封装为原生 `Tool`）。v2 连接器已建立协议 v2、request-id + 流式机制、per-device/per-tool 授权与审批、设备主人主权。本变更把二者接起来：连接器充当"本机 MCP server 的搬运工"，服务端把搬来的工具当作普通 MCP 工具注册。

约束延续前序：Python 3.11+ asyncio、不引入重量级依赖、用户电脑不可开公网端口、服务端不可信、复用优先。

## Goals / Non-Goals

**Goals:**

- 设备主人在本机登记的 MCP server，其工具能出现在服务端 `ToolRegistry` 并被 Agent 正常调用。
- 最大化复用现有 `mcp.py` 封装：只替换底层 transport 为"经连接器通道"，工具封装/名称/重连逻辑不重写。
- 桥接工具沿用 v2 授权/审批/审计，不新开安全通道。
- 默认关闭（`allowMcpProxy: false`），对 v1/v2 用户零影响。

**Non-Goals:**

- 服务端自动发现或安装用户 MCP server（仍由设备主人本机登记）。
- 把服务端侧 MCP server 反向暴露给连接器。
- MCP 的 resources/prompts 全量代理（v2.5 先做 tools，resources/prompts 视反馈再定）。
- 桌面 GUI 控制（v3）。

## Decisions

1. **连接器做 MCP client，服务端做"透明注册"**
   连接器在本机以标准 MCP client 连本地 server（stdio 子进程 / 本地 http），把 `list_tools` 结果上报、把 `call_tool` 请求转发。服务端不直接连任何 MCP server，只通过连接器通道收发 MCP 报文——用户电脑无需开端口。

2. **发现-调用（已采用），而非透明 transport 隧道**
   > 实现调整：原设想"把 MCP JSON-RPC 报文封进帧走连接器通道、服务端复用 `mcp.py` 的 `ClientSession` transport"。实际采用**连接器做完整 MCP client + 服务端结构化转发**：连接器在本机跑 `ClientSession`（MCP 往返留在本机、低延迟），只把工具清单与调用结果跨 WAN 传；服务端经 `connector_list_mcp_tools`/`connector_call_mcp_tool` 暴露，与其余 `connector_*` 工具的发现-调用模式一致，天然适配设备动态上下线。授权键用 `mcp:<server>:<tool>`（不进 provider 词表，规避名称长度问题）。沿用 `MCPServerConfig` 形态、`enabledTools` 白名单与 schema 约定。

3. **桥接工具沿用 v2 授权与审批**
   桥接进来的每个 MCP 工具在授权/审批模型里等同一个"本地工具"：per-tool 授权、`auto`/`webui`/`local` 审批、设备主人主权全部适用。不为 MCP 单开一套安全模型。

4. **MCP server 生命周期归连接器本机管理**
   stdio server 由连接器 spawn/kill；http server 由设备主人自行运行、连接器仅连接。设备离线即注销其全部桥接工具；重连后重新枚举注册。服务端不控制 server 进程。

5. **能力协商 + 三重开关**
   `capabilities` 增加 `mcp`；工具注册受 `enabled` + `allowExec` + `allowMcpProxy` 三重控制。不声明 `mcp` 能力或未开 `allowMcpProxy` 的节点不参与桥接。

## Risks / Trade-offs

- [MCP server 自身权限过大] → 桥接工具沿用 v2 审批（决策 3）；登记文档提示设备主人评估每个 server 能访问的本机资源；高危 server 建议 `local` 审批。
- [经连接器通道的 MCP 延迟/断连] → 复用 v2 request-id + 超时；设备下线注销工具（决策 4），Agent 侧得到"工具暂不可用"的明确反馈。
- [大量桥接工具灌爆 prompt 词表] → 沿用 `MCPServerConfig.enabled_tools` 白名单只注册选定工具；WebUI 展示并可裁剪。
- [连接器需内置 MCP client 依赖（体积）] → 客户端新增 `mcp` SDK 依赖，惰性导入；session 工厂可注入，测试无需真实 SDK/子进程。
- [stdio server 子进程泄漏] → 复用 v2 执行器的进程树终止；设备离线时连接器清理所有 spawn 的 server。

## Migration Plan

1. 服务端随常规版本发布，`allowMcpProxy` 默认 false —— 零行为变化。
2. v2.5 连接器发版；设备主人 `mcp add` 登记本机 server；服务端开启三重开关并授权后工具方可见。
3. 试点用一个只读 MCP server（如本地文件索引）验证 list→注册→授权→调用→设备下线注销全链路。
4. 回滚：关闭 `allowMcpProxy`，桥接工具注销，v2 执行与 v1 文件能力不受影响。
5. 通过安全评审后 `openspec archive add-connector-mcp-proxy`。

## Open Questions

- 是否代理 MCP resources/prompts，还是仅 tools？→ v2.5 仅 tools，其余看反馈。
- ~~桥接工具命名前缀过长触发 provider 名称长度限制？~~ → 已解决：采用发现-调用，桥接工具不进 provider 词表，授权键 `mcp:<server>:<tool>` 无长度限制。
- 多设备桥接同名 server 的冲突消解策略？→ 以 device 段区分，UI 展示设备归属。
