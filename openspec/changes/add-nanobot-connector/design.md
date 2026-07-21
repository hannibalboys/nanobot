# Design: add-nanobot-connector

> 详细技术方案见 `docs/nanobot-connector/02-技术设计.md`（NBC-TD-001），本文为规格化摘要与决策记录。

## Context

nanobot 服务端部署（例：内网 `192.168.90.100`）时，`AgentLoop`/`AgentRunner` 的文件与执行工具只作用于服务器文件系统。用户本地资料无法被 Agent 触达。现有基础设施可复用：

- 网关由 `WebSocketChannel`（`nanobot/channels/websocket.py`）基于 `websockets.serve(process_request=...)` 同端口分发 HTTP 与 WS，`GatewayServices` 承载 tokens/media/workspaces 等服务。
- 工具经 `ToolLoader`（pkgutil 扫描 + entry points）自动发现，继承 `Tool` 基类，注册进 `ToolRegistry`。
- 配置为 pydantic Schema（`nanobot/config/schema.py`，camelCase 别名），加载自 `~/.nanobot/config.json`。
- `nanobot/pairing/` 已有"配对码审批"模式可参照。

约束：Python 3.11+ 全 asyncio；不引入 gRPC/消息队列等新重量级依赖；用户电脑无公网 IP、不可开监听端口。

## Goals / Non-Goals

**Goals:**

- 用户电脑安装轻量连接器后，Agent 可按需列取/搜索/读取/拉取**目录白名单内**的本地文件到服务器工作区。
- 设备生命周期可管理：一次性配对码接入、令牌吊销即时生效、WebUI 可视化。
- 默认关闭（`connector.enabled: false`），对存量部署零影响。
- 三平台（Windows/macOS/Linux）单文件安装包与开机自启。

**Non-Goals:**

- 本地文件写入/删除（v1 只读）。
- 远程命令执行（`allowExec` 仅预留配置位，v1 恒 false 且客户端不实现）。
- 实时双向同步、断点续传、P2P/中继直连、移动端（列入 v2 候选）。

## Decisions

1. **反向连接（连接器出站 WSS）而非服务端主动连接**
   备选：服务端拨号到用户电脑、VPN。用户电脑无固定 IP/不可开端口，出站长连接免网络改造，与 CMDB 采集器、Zabbix active agent、VS Code Tunnel 同模式。

2. **复用网关端口与 `process_request` 分发链，不新开监听端口**
   备选：独立端口/独立进程。同端口新增 `/connector/ws` 路由与 `/api/connector/*` 管理路由，少一套端口、证书与部署文档；`ConnectorHub` 挂 `GatewayServices` 随网关启停。

3. **JSON 文本帧 + base64 分块（256KB）走同一条 WS，控制面与数据面不分离**
   备选：二进制帧、独立 HTTP 上传、gRPC。与现有 WebUI 通道编码习惯一致、调试成本最低；base64 +33% 开销在内网场景可接受，帧大小受既有 `max_message_bytes` 约束。协议帧带 `protocol: 1` 版本号，注册时协商，为 v2（续传/watch/exec）留扩展位。

4. **按需拉取落盘到 `<workspace>/connector/<node_id>/`，复用现有文件工具后处理**
   备选：流式直读不落盘、实时同步。落盘后 Agent 用既有 `read_file`/`exec` 完成 PPT 生成，链路最短；`.part` 临时文件 + sha256 校验 + rename 原子落盘，与 `memory.py` 原子写风格一致。

5. **配对码 → 设备令牌两段式认证**
   备选：静态共享 token、mTLS。一次性配对码（TTL 10min，WebUI 生成）兑换长期设备令牌；服务端只存 `sha256(token)`，握手用 `hmac.compare_digest` 常量时间比较（与 `_authorize_websocket_handshake` 同法）；吊销＝标记失效＋断开在线连接。mTLS 对个人用户部署成本过高。

6. **白名单在客户端强制执行，服务端请求视为不可信**
   `Path.resolve()` 消解 `..`/符号链接后必须落在用户显式 `allow` 的根目录内；禁止共享文件系统根。这是数据不出用户授权范围的最后防线，即使服务端被攻破也不能越权读取。

7. **客户端用 Python + websockets + typer + PyInstaller，不换语言**
   备选：Go/Rust 重写体积更小。复用团队技术栈与代码风格，v1 交付速度优先；体积/内存目标（≤80MB 常驻）可达。

8. **工具经 pkgutil 自动发现 + 配置开关注册**
   与 `FileToolsConfig` 开关模式一致：`connector.enabled=false` 时 `connector_*` 工具完全不出现在 `ToolRegistry`，不占用 prompt 词表。

9. **双向路径防线：客户端白名单 + 服务端落盘 sanitize**
   白名单防"服务端越权读客户端"；落盘 sanitize（剥离盘符、消解 `..`、校验最终路径在 `<workspace>/connector/<node_id>/` 内）防"被攻破的连接器写出服务器落盘区"。两侧互不信任，各自独立执行。

10. **设备归属绑定 + 按会话隔离**
   设备记录携带归属用户（生成配对码的 WebUI 会话身份）与机器指纹。多用户部署时工具与管理路由按归属过滤（跨用户访问返回 `not_found`，不泄露存在性）；同指纹重新配对替换旧记录并吊销旧令牌。

11. **自签证书用指纹固定（pinning），不降级校验**
   内网部署普遍使用自签证书。`pair --fingerprint <sha256>` 固定证书指纹，此后每次连接校验；默认仍走完整信任链校验，`--insecure` 仅显式且带警告。备选 mTLS 对个人用户成本过高（同决策 5）。

12. **传输独立超时 + 显式取消帧**
   `fs.fetch` 不复用 `rpcTimeoutS`（60s 对大文件不现实），由 `transferTimeoutS`（默认 600s）控制；发起方不再等待时下发 `cancel` 帧终止流。fetch 成功以 `file_chunk` 流响应、失败以 `rpc_response(ok=false)` 终止，消除协议歧义。

## Risks / Trade-offs

- [大文件占用网关内存/带宽] → 单文件上限 `maxFileBytes`（默认 200MB）、每节点并发传输上限、分块背压；超限返回 `too_large` 引导用户拆分。
- [用户误共享敏感目录] → 禁止根目录、CLI 添加目录需显式命令、WebUI 设备详情展示共享范围、双端审计日志。
- [base64 编码开销] → 接受 +33%（决策 3）；若灰度实测吞吐不足，v2 升级为二进制帧（协议版本位已预留）。
- [三平台打包与签名（杀软误报）] → Sprint 1 启动打包 PoC，Windows 代码签名证书提前采购（项目计划风险 R1）。
- [连接断开时 pending RPC 悬挂] → Hub 在 detach 时以 `ConnectorDisconnected` 失败所有 pending future，工具层映射为可读的 `ToolResult.error`。
- [企业代理/防火墙拦截出站 WS] → 客户端支持代理配置；文档说明需放行网关端口；失败时 CLI `status` 给出可诊断信息。
- [`/connector/pair` 无鉴权端点被暴力猜码] → 来源 IP 失败 5 次锁定 15 分钟 + 全局失败熔断（作废全部未兑换码并告警）+ 常量时间比较。
- [落盘区无限增长吃满磁盘] → `fetchCacheMaxBytes`（默认 2GB）+ LRU 清理。
- [恶意连接器路径注入落盘逃逸] → 服务端落盘 sanitize 独立防线（决策 9），渗透测试覆盖。

## Migration Plan

1. 服务端随常规版本发布，`connector.enabled` 默认 false —— 零行为变化。
2. 试点实例开启开关 + 部署 WSS 证书，验证配对与拉取链路。
3. 客户端独立发版（GitHub Release 三平台资产），WebUI 下载页指向资产。
4. 回滚：关闭 `connector.enabled`（端点消失、存量连接断开、工具不再注册）；客户端无需处理，重连自然失败并提示。
5. GA 后 `openspec archive add-nanobot-connector` 归档规格。

## Open Questions

- 配对码由管理员统一生成还是允许普通登录用户自助生成？（设备访问隔离已由决策 10 覆盖，此处仅剩"谁能发码"的策略）→ S2 设计评审定。
- WebUI 下载页直链 GitHub Release 还是支持管理员配置内网镜像地址？→ 倾向支持镜像配置，S2 确认。
- `fs.search` 是否需要内容级搜索（v1 仅文件名模糊匹配，带结果数/时长/深度上限）？→ 灰度反馈决定是否进 v2。
- 设备令牌是否需要有效期与轮换机制（v1 为长期令牌 + 可吊销）？→ 安全评审时定，协议已预留重新配对路径。
