# connector-client 规格增量

## ADDED Requirements

### Requirement: 工具注册表 CLI
连接器 SHALL 提供 `tool add` / `tool list` / `tool remove` CLI 子命令，管理本机 `tools.json` 中的工具定义（名称、可执行文件路径、参数模板、工作目录、超时、审批策略）。工具定义 MUST 只能在本机通过 CLI 增删改，MUST NOT 可由服务端远程写入。

#### Scenario: 本机登记工具
- **WHEN** 设备主人执行 `tool add` 登记一个可执行文件与参数模板
- **THEN** 该工具写入 `tools.json`，此后可被 `tools.list` 枚举

#### Scenario: 服务端无法远程写入工具
- **WHEN** 服务端通过协议尝试新增或修改工具定义
- **THEN** 连接器拒绝，`tools.json` 不被改动

### Requirement: 工具凭据本机配置
连接器 SHALL 提供在本机为工具配置凭据/环境变量实体的手段（CLI 或本机配置文件），凭据实体 MUST 只存本机、不经协议传输。执行时连接器 SHALL 按工具声明的引用注入子进程环境。

#### Scenario: 本机配置凭据
- **WHEN** 设备主人为某工具在本机配置其声明的环境变量实体
- **THEN** 该工具执行时凭据被注入子进程环境，协议与服务端不接触凭据实体

### Requirement: 执行器
连接器 SHALL 提供执行器：校验参数模板、以 `shell=False` 的 `argv` 数组启动子进程、按声明注入凭据环境变量、增量回传 stdout/stderr、在结束/取消/超时时终止进程树并回传结果帧。执行器 MUST 在启动前完成 `local` 审批（若工具声明）。

#### Scenario: 本机审批后执行
- **WHEN** 一个 `approval=local` 的工具被调用且设备主人在本机放行
- **THEN** 执行器启动进程并流式回传输出

#### Scenario: 终止进程树
- **WHEN** 收到取消或触发超时
- **THEN** 执行器终止该进程及其子孙进程，回传已取消/超时结果

### Requirement: 本机执行审计
连接器 SHALL 在本机记录每次执行（含被拒绝/超时）的滚动审计日志，字段与服务端一致（脱敏参数摘要、审批方式、退出码、耗时、结果）。

#### Scenario: 本机留痕
- **WHEN** 任一工具执行结束或被拒绝
- **THEN** 连接器本机审计日志新增一条记录
