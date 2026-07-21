# Tasks: add-nanobot-connector

## 1. 配置与协议基础

- [x] 1.1 在 `nanobot/config/schema.py` 新增 `ConnectorConfig`（enabled/path/pairingCodeTtlS/rpcTimeoutS/transferTimeoutS/maxFileBytes/maxInlineReadBytes/maxConcurrentTransfers/chunkBytes/fetchCacheMaxBytes/allowExec，camelCase 别名），并接入根配置
- [x] 1.2 创建 `nanobot/connector/protocol.py`：register/registered/heartbeat/rpc_request/rpc_response/file_chunk/cancel/revoked/error 帧的 pydantic 模型与 `PROTOCOL_VERSION = 1`
- [x] 1.3 protocol 单元测试：编解码往返、未知字段容忍、版本字段校验（`tests/connector/test_protocol.py`）

## 2. 设备配对与令牌（connector-pairing）

- [x] 2.1 实现 `nanobot/connector/devices.py` DeviceStore：配对码签发（8 位、TTL、一次性、记录归属用户）、令牌兑换（token_urlsafe(32)，仅存 sha256）、机器指纹去重（同指纹重配对替换旧记录并吊销旧令牌）、吊销标记、`devices.json` 原子持久化
- [x] 2.2 HTTP 路由：`POST /api/connector/pairing-codes`（WebUI 会话鉴权）、`POST /connector/pair`（配对码兑换）、`GET /api/connector/nodes`、`DELETE /api/connector/nodes/{id}`（管理路由按归属过滤）
- [x] 2.3 配对端点防暴力：来源 IP 失败 5 次锁定 15 分钟（429）、全局失败熔断（作废未兑换码 + 告警）、常量时间比较
- [x] 2.4 单元测试：配对码过期/复用拒绝、令牌哈希存储、重启后存活、吊销后握手 401、限速与熔断、指纹去重（`tests/connector/test_devices.py`）

## 3. 网关接入与 Hub（connector-gateway）

- [x] 3.1 实现 `nanobot/connector/hub.py` ConnectorHub：attach/detach、注册 10s 超时、心跳、在线节点表、`rpc()`（id 关联 + 超时 + 断连清理 pending future）、`cancel` 帧下发
- [x] 3.2 实现 `nanobot/connector/transfer.py`：file_chunk 组装、sha256 校验、`.part` 原子落盘到 `<workspace>/connector/<node_id>/`、落盘路径 sanitize（剥离盘符/根、消解 `..`、resolve 后强制在落盘区内）、maxFileBytes/transferTimeoutS 与并发限制、`fetchCacheMaxBytes` 配额 + LRU 清理
- [x] 3.3 在网关 `process_request` 分发链注册 `/connector/ws` WS 升级（device_token 常量时间校验），Hub 挂载到 `GatewayServices`，`enabled=false` 时全部路由 404
- [x] 3.4 审计日志：文件访问事件（时间/会话/node_id/method/路径/字节数/结果）
- [x] 3.5 集成测试：内存双工假连接器跑通 register→rpc→fetch；断连清理；校验失败不落盘；超限中止；恶意路径注入被拒；cancel 终止传输；LRU 清理（`tests/connector/test_hub_integration.py`）

## 4. Agent 工具（connector-tools）

- [x] 4.1 实现 `nanobot/agent/tools/connector.py` 五个工具（list_nodes/list_files/search_files/read_file/fetch_file），继承 `Tool`，经 ToolLoader 自动发现，受 `connector.enabled` 开关控制
- [x] 4.2 错误映射：node_offline/rpc_timeout/path_denied/too_large/not_found/非 UTF-8 内联读取 → 可操作的 `ToolResult.error` 文案
- [x] 4.3 设备可见性隔离：多用户部署时工具按当前会话归属过滤设备，跨用户访问返回等同 not_found 的错误
- [x] 4.4 工具测试：开关注册行为、参数校验、错误映射、归属过滤、fetch 返回路径可被既有 read_file 读取（`tests/agent/tools/test_connector.py`）

## 5. 连接器客户端（connector-client）

- [x] 5.1 搭建 `connector/` 子项目骨架（pyproject.toml，包 `nanobot_connector`，依赖 websockets/pydantic/typer）
- [x] 5.2 实现 `config.py`（~/.nanobot-connector/config.json）与 `cli.py`（pair/allow/remove/start/status），`allow` 拒绝文件系统根目录；`pair` 支持 `--fingerprint` 证书指纹固定与显式 `--insecure`（带警告）
- [x] 5.3 实现 `client.py`：出站 WSS（默认完整证书校验 / 指纹 pinning）、register 首帧（含机器指纹）、心跳、指数退避重连（1s→60s+抖动）、`revoked` 停止重连、`cancel` 帧终止传输
- [x] 5.4 实现 `files.py`：`resolve_within_roots` 白名单校验（.. 与符号链接逃逸拦截）、fs.list/search/read/stat 处理器（search 带结果数/时长/深度上限与 truncated 标记）、fs.fetch 分块上传（尾帧 sha256）
- [x] 5.5 本地审计日志（滚动 30 天）
- [x] 5.6 实现 `service.py` 开机自启：Windows 服务 / launchd / systemd
- [x] 5.7 客户端测试：白名单逃逸矩阵、重连退避、配对流程（含指纹校验）、分块传输与取消、搜索上限（`connector/tests/`）
- [x] 5.8 PyInstaller 打包 spec 与三平台 CI 构建流水线（含 Windows 签名占位）

## 6. WebUI（connector-webui）

- [x] 6.1 设备列表页：`GET /api/connector/nodes` 渲染名称/平台/在线状态/最后在线/共享目录摘要；`enabled=false` 时隐藏入口
- [x] 6.2 添加设备向导：下载 → 生成配对码（倒计时）→ 轮询等待上线
- [x] 6.3 设备详情与吊销（二次确认 → DELETE → 状态刷新）
- [x] 6.4 WebUI 组件测试（`bun run test`）

## 7. 端到端与收尾

- [x] 7.1 端到端验证："列节点 → 列文件 → 拉取文档 → 生成 PPT 并经 WebUI 交付" 全链路（自动化：`tests/connector/test_e2e_flow.py`；PPT 生成本身依赖 LLM，需人工在 WebUI 对话中验收）
- [x] 7.2 可靠性验证：断网/杀进程/网关重启后 30s 内恢复；吊销 10s 内断开（自动化：`test_disconnect_fails_pending`、`test_revoke_disconnects_online_node`、`test_fetch_cancel_on_timeout`；真实网络/三平台仍需灰度人工抽测）
- [x] 7.3 文档：用户手册（安装/配对/共享目录）、运维手册（开关/证书/监控/Runbook）
- [x] 7.4 `ruff check nanobot/ connector/` 零告警；新增代码单测覆盖率 ≥ 80%
- [ ] 7.5 安全评审与渗透测试（双向路径逃逸/配对码暴力猜测/令牌重放/越权访问他人节点/假冒服务器）通过后，`openspec archive add-nanobot-connector`
