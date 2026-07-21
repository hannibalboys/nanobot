# connector-gateway 规格增量

## ADDED Requirements

### Requirement: 连接器 WebSocket 接入端点
当配置 `connector.enabled` 为 true 时，网关 SHALL 在现有 `websockets.serve(process_request=...)` 分发链上暴露 `/connector/ws`（路径可由 `connector.path` 配置）WebSocket 端点；当 `connector.enabled` 为 false 时，该端点及所有 `/api/connector/*` 路由 SHALL 不存在（返回 404）。

#### Scenario: 开关关闭时端点不可达
- **WHEN** `connector.enabled` 为 false 且客户端请求 `/connector/ws`
- **THEN** 网关返回 404，且不注册任何 `connector_*` Agent 工具

#### Scenario: 合法设备令牌完成握手
- **WHEN** 连接器携带有效 `device_token` 发起 WS 握手
- **THEN** 握手成功（令牌以常量时间比较校验其 sha256 哈希）

#### Scenario: 非法令牌被拒绝
- **WHEN** 握手携带缺失、错误或已吊销的 `device_token`
- **THEN** 网关返回 401 并拒绝升级

### Requirement: 节点注册与生命周期
握手成功后，连接器发送的首帧 MUST 为 `register` 帧（含协议版本、节点名称、平台、共享根目录列表）。网关 SHALL 在收到合法 `register` 后将节点标记为在线并回复 `registered` 帧（含 `node_id` 与心跳间隔）；若 10 秒内未收到 `register`，网关 SHALL 关闭连接。

#### Scenario: 注册成功
- **WHEN** 连接器在握手后 10 秒内发送 `protocol` 版本受支持的 `register` 帧
- **THEN** 网关回复 `registered` 帧，节点出现在在线节点列表中

#### Scenario: 协议版本不支持
- **WHEN** `register` 帧的 `protocol` 版本高于服务端支持的版本
- **THEN** 网关回复 `error(code="protocol_unsupported")` 并关闭连接

#### Scenario: 连接断开即摘除节点
- **WHEN** 节点的 WS 连接因任何原因断开
- **THEN** 节点立即从在线列表移除，且该节点所有未完成 RPC 以 `ConnectorDisconnected` 失败

### Requirement: RPC 请求路由与超时
网关 SHALL 提供按 `node_id` 下发 `rpc_request`（方法：`fs.list` / `fs.search` / `fs.read` / `fs.fetch` / `fs.stat`）并等待对应 `rpc_response` 的能力；请求与响应通过唯一 `id` 关联。非传输类 RPC 等待超过 `connector.rpcTimeoutS`（默认 60 秒）SHALL 以 `rpc_timeout` 错误结束；`fs.fetch` 的整体时限 SHALL 由独立的 `connector.transferTimeoutS`（默认 600 秒）控制。`fs.fetch` 成功时以 `file_chunk` 帧流响应（不另发 `rpc_response`），失败或中途出错时 MUST 以 `rpc_response(ok=false)` 终止该 `id` 的流。

#### Scenario: RPC 正常往返
- **WHEN** 网关向在线节点下发 `fs.list` 请求
- **THEN** 连接器的 `rpc_response` 按 `id` 唤醒等待方并返回结果

#### Scenario: RPC 超时
- **WHEN** 节点在 `rpcTimeoutS` 内未响应
- **THEN** 调用方收到 `rpc_timeout` 错误，该 `id` 的后续迟到响应被丢弃

#### Scenario: 目标节点离线
- **WHEN** 向不在线的 `node_id` 发起 RPC
- **THEN** 调用方立即收到 `node_offline` 错误

### Requirement: 传输取消
当发起方不再等待（Agent 回合取消、调用超时）时，网关 SHALL 向连接器发送 `cancel` 帧（携带对应 `id`）终止进行中的传输，并丢弃该 `id` 的后续 `file_chunk`。

#### Scenario: 超时后取消传输
- **WHEN** `fs.fetch` 超过 `transferTimeoutS` 仍未完成
- **THEN** 网关发送 `cancel` 帧、清理临时文件，调用方收到 `rpc_timeout` 错误

### Requirement: 分块文件接收与原子落盘
网关 SHALL 接收 `file_chunk` 帧流（base64 编码，块大小由 `connector.chunkBytes` 控制），组装后校验 sha256，并以临时文件加重命名的方式原子写入 `<workspace>/connector/<node_id>/` 下的目标路径。累计字节超过 `connector.maxFileBytes` 的传输 SHALL 中止并返回 `too_large`；单节点并发传输数 SHALL 不超过 `connector.maxConcurrentTransfers`。

#### Scenario: 完整传输成功
- **WHEN** 全部分块接收完毕且 sha256 与尾帧声明一致
- **THEN** 文件出现在 `<workspace>/connector/<node_id>/` 对应路径，无 `.part` 残留

#### Scenario: 校验失败不落盘
- **WHEN** 组装后的 sha256 与声明不一致
- **THEN** 临时文件被删除，调用方收到传输失败错误

#### Scenario: 超过大小上限
- **WHEN** 累计接收字节超过 `maxFileBytes`
- **THEN** 传输中止、临时文件清理，调用方收到 `too_large`

### Requirement: 落盘路径防穿越（服务端侧）
网关 MUST 将连接器提供的客户端路径视为不可信输入：落盘相对路径 SHALL 经规范化（剥离盘符与根、替换非法字符、消解 `.`/`..`）后校验最终真实路径位于 `<workspace>/connector/<node_id>/` 之内，否则拒绝该传输。此防线独立于客户端白名单校验——即使连接器被攻破，也不能写出落盘目录。

#### Scenario: 恶意相对路径被拒绝
- **WHEN** 连接器（或被篡改的客户端）声明目标路径 `../../../etc/cron.d/evil`
- **THEN** 网关拒绝落盘，本次传输失败并记录审计日志

#### Scenario: Windows 路径安全转换
- **WHEN** 客户端路径为 `D:\PPT资料\报告.docx`
- **THEN** 落盘为 `<workspace>/connector/<node_id>/D/PPT资料/报告.docx` 等去盘符的安全相对路径

### Requirement: 落盘区磁盘配额
`<workspace>/connector/` 目录总占用 SHALL 受 `connector.fetchCacheMaxBytes`（默认 2GB）约束：新传输将导致超限时，网关 SHALL 先按最近最少使用（LRU）清理旧文件；单文件本身超过配额时返回 `too_large`。

#### Scenario: 超限触发 LRU 清理
- **WHEN** 新的 `fs.fetch` 将使落盘区总量超过 `fetchCacheMaxBytes`
- **THEN** 最久未访问的旧文件被清理，直至新文件可写入

### Requirement: 审计日志
网关 SHALL 对每次连接器文件访问记录审计日志，内容至少包含：时间、发起会话、`node_id`、方法、路径、字节数、结果。

#### Scenario: 拉取文件产生审计记录
- **WHEN** 任一 `fs.fetch` 完成（无论成败）
- **THEN** 审计日志新增一条包含上述字段的记录
