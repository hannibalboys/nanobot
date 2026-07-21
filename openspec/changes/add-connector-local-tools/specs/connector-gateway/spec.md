# connector-gateway 规格增量

## ADDED Requirements

### Requirement: 工具枚举与调用路由
`ConnectorHub` SHALL 提供 `list_tools(node_id, owner_id)` 与 `call_tool(node_id, tool, args, owner_id)`，将请求路由到目标在线节点并沿用 v1 的归属校验。`call_tool` SHALL 复用 request-id + 队列机制转发 `exec_output` 流并以 `exec_result` 终止；断连时 MUST 以可读错误失败进行中的执行。

#### Scenario: 路由到目标节点并转发输出
- **WHEN** 服务端对一台在线设备发起工具调用
- **THEN** Hub 将请求路由到该节点，转发其增量输出，并在结束时返回最终结果

#### Scenario: 断连中止进行中执行
- **WHEN** 执行进行中目标节点断线
- **THEN** Hub 以设备离线的可读错误终止该次执行

### Requirement: 执行资源上限
网关 SHALL 施加每节点并发执行上限 `maxConcurrentExecs`、单次超时 `execTimeoutS` 与输出累计上限 `maxExecOutputBytes`。超并发 SHALL 返回可操作错误，超时 SHALL 触发取消，超限输出 SHALL 被截断。

#### Scenario: 超并发被拒并可重试
- **WHEN** 某节点进行中的执行数已达 `maxConcurrentExecs` 又收到新调用
- **THEN** 返回"并发已满、稍后重试"的可读错误，不启动新执行

### Requirement: 执行限流
网关 SHALL 对执行施加 per-session 与 per-device 速率限制（`execRatePerMinute`）。命中限流 SHALL 返回 `exec_limit` 的可重试错误并记入指标，MUST NOT 启动新执行。

#### Scenario: 超速率被限流
- **WHEN** 某会话或某设备的执行速率超过 `execRatePerMinute`
- **THEN** 返回 `exec_limit` 可重试错误，不启动新执行

### Requirement: 执行可观测性指标
网关 SHALL 暴露执行指标：执行次数、时长分布、失败率、审批拒绝率、限流命中数（按设备/会话维度）。指标 SHALL 可供监控与告警消费。

#### Scenario: 指标可被采集
- **WHEN** 发生若干次执行与审批拒绝
- **THEN** 对应的次数、时长、失败率、审批拒绝率与限流命中指标被记录并可读取

### Requirement: 工具、授权与跨人访问管理路由
网关 SHALL 新增 `/api/connector/*` 路由用于枚举某设备的可用工具、查询/授予/收回 per-tool 授权、处理跨人访问请求/邀请、查看设备活跃使用者、以及响应待处理的 WebUI 审批。这些路由 SHALL 按会话归属过滤，非归属主人 MUST NOT 修改授权或跨人授予。

#### Scenario: 非主人无法修改授权
- **WHEN** 非设备归属主人的会话尝试授予或收回该设备工具授权
- **THEN** 路由返回拒绝，授权状态不变

#### Scenario: 设备主人处理跨人访问请求
- **WHEN** 设备主人通过管理路由接受某操作者的访问请求并授予工具
- **THEN** 该操作者获得对应工具的调用授权，设备主人可在活跃使用者视图看到其使用
