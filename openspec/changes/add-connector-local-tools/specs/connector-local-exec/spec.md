# connector-local-exec 规格增量

## ADDED Requirements

### Requirement: 协议 v2 执行方法与能力协商
系统 SHALL 将 `PROTOCOL_VERSION` 提升为 2，新增 RPC 方法 `tools.list`、`tools.call`、`tools.cancel`，并在 register 帧的节点信息中携带 `capabilities` 能力集（如 `["fs", "exec"]`）。服务端 SHALL 按客户端声明的能力交集决定可用方法；不声明 `exec` 能力的连接器 MUST NOT 被路由任何执行请求。

#### Scenario: v1 客户端连 v2 服务端降级
- **WHEN** 一个 protocol=1 且不声明 `exec` 能力的连接器接入 v2 服务端
- **THEN** 该节点的文件能力照常可用，且对其调用执行工具时返回"该设备不支持执行"的可读错误

#### Scenario: 能力协商决定方法可用性
- **WHEN** 连接器 register 时声明 `capabilities` 含 `exec`
- **THEN** 服务端允许向该节点路由 `tools.list` / `tools.call` / `tools.cancel`

### Requirement: 客户端声明式工具注册表
连接器 SHALL 从本机 `tools.json` 读取设备主人登记的工具定义，每个定义至少包含 `name`、可执行文件路径、参数模板、工作目录、超时与审批策略。连接器响应 `tools.list` 时 SHALL 只返回已登记工具；服务端 MUST NOT 通过协议新增、修改或调用未登记的工具。

#### Scenario: 枚举已登记工具
- **WHEN** 服务端对声明 `exec` 能力的节点发起 `tools.list`
- **THEN** 连接器返回其 `tools.json` 中登记的工具名称、描述与参数模式

#### Scenario: 调用未登记工具被拒绝
- **WHEN** 服务端 `tools.call` 引用一个未在 `tools.json` 登记的工具名
- **THEN** 连接器拒绝执行并返回工具不存在的错误，不启动任何进程

### Requirement: 参数模板校验与无 shell 执行
连接器 SHALL 依据工具的参数模板校验每个入参（类型、必填、枚举/正则约束），并将其渲染为 `argv` 数组后以非 shell 方式（`shell=False`）启动子进程。未在模板中声明的参数 MUST 被拒绝；路径类参数 MUST 强制落在工具声明的允许目录内。

#### Scenario: 非法参数被拒绝
- **WHEN** `tools.call` 传入不符合模板约束或未声明的参数
- **THEN** 连接器拒绝执行并返回参数校验失败的错误，不启动进程

#### Scenario: 参数不经 shell 解释
- **WHEN** 参数值包含 shell 元字符（如 `; rm -rf`）
- **THEN** 该值作为单一 `argv` 元素原样传递，不被 shell 解释执行

### Requirement: 流式执行输出与结果帧
连接器执行工具时 SHALL 以 `exec_output` 帧增量回传 stdout/stderr（标注 `stream` 与 `seq`），并以 `exec_result` 帧终止（退出码、耗时、是否超时、是否因超限截断）。服务端 SHALL 将输出流转发给发起方。

#### Scenario: 实时输出与终止结果
- **WHEN** 一个已授权工具被成功调用并运行至结束
- **THEN** 发起方先收到增量 stdout/stderr，再收到含退出码与耗时的最终结果

### Requirement: 执行取消与资源上限
系统 SHALL 支持通过 `tools.cancel`（或既有 `cancel` 帧）终止进行中的执行，连接器 MUST 终止子进程树。服务端 SHALL 施加每节点并发上限 `maxConcurrentExecs`、单次超时 `execTimeoutS`、输出累计上限 `maxExecOutputBytes`；超时 SHALL 触发取消，超限输出 SHALL 被截断并标记 `truncated`。

#### Scenario: 取消终止子进程树
- **WHEN** 发起方对进行中的执行发出取消
- **THEN** 连接器终止该进程及其子孙进程，并回传标记为已取消的结果

#### Scenario: 超时自动取消
- **WHEN** 一次执行超过 `execTimeoutS` 仍未结束
- **THEN** 服务端下发取消、连接器终止进程，结果标记为超时

#### Scenario: 输出超限截断
- **WHEN** 一次执行的累计输出超过 `maxExecOutputBytes`
- **THEN** 输出被截断，结果帧标记 `truncated`，执行不再继续灌流

### Requirement: 工具凭据只存本机
工具定义 SHALL 只声明所需凭据/环境变量的**引用**（环境变量名或本机凭据条目 id），凭据实体 MUST 只存于设备本机并由连接器在执行时注入子进程环境。凭据实体 MUST NOT 出现在协议帧、服务端状态或审计日志中；服务端与其他操作者 MUST NOT 能读取凭据实体。

#### Scenario: 凭据本机注入不外泄
- **WHEN** 一个声明了环境变量引用的工具被调用
- **THEN** 连接器把本机配置的凭据实体注入子进程环境，协议与服务端仅见引用名，审计仅见脱敏摘要

#### Scenario: 缺失凭据的可读失败
- **WHEN** 工具声明的凭据引用在本机未配置
- **THEN** 连接器拒绝执行并返回"缺少所需凭据、需设备主人在本机配置"的错误，不启动进程

### Requirement: 稳定执行错误码
协议、网关与工具三层 SHALL 共享一套稳定执行错误码：`exec_unsupported`、`tool_not_found`、`invalid_args`、`exec_denied`、`approval_denied`、`approval_timeout`、`exec_limit`、`exec_timeout`、`exec_cancelled`。同一失败语义 MUST 使用同一错误码。

#### Scenario: 错误码一致
- **WHEN** 因未授权被拒绝的执行分别在网关与工具层被观察
- **THEN** 两层均以 `exec_denied` 表示该失败

### Requirement: 执行审计
系统 SHALL 在服务端与连接器本机各记录一条执行审计：时间、发起会话/操作者、node_id、工具名、参数摘要（敏感值脱敏）、审批方式与审批人、退出码、耗时、字节数、结果。审计 MUST NOT 可通过协议关闭。

#### Scenario: 每次执行留痕
- **WHEN** 任一工具执行完成（成功、失败、取消或拒绝）
- **THEN** 服务端与连接器本机各写入一条包含上述字段的审计记录
