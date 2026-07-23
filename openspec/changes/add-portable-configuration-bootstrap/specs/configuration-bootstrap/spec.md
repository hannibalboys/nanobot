# configuration-bootstrap 规格增量

## ADDED Requirements

### Requirement: 服务端声明配置版本化

系统 SHALL 在服务端根配置序列化时写入 `schemaVersion`。加载旧的无版本配置时，系统 SHALL 将其识别为兼容的历史版本，按顺序执行幂等迁移后再做 Schema 校验。迁移 MUST 保留有效的既有配置值，MUST NOT 将运行状态、设备身份或环境变量中解析出的秘密写入声明配置。

#### Scenario: 升级无版本旧配置

- **WHEN** 用户对不含 `schemaVersion` 的有效旧配置执行 `nanobot config refresh`
- **THEN** 系统迁移已知旧字段、补齐当前默认字段、写入当前 `schemaVersion`，并保留用户已有的有效设置

#### Scenario: 重复刷新幂等

- **WHEN** 用户连续两次对同一当前版本配置执行 `nanobot config refresh`
- **THEN** 第二次执行不改变任何有效配置语义

### Requirement: 部署档案、来源优先级与安全导出

系统 SHALL 区分内置通用模板、由项目维护的部署档案和目标机器的真实声明配置。系统 MUST 按以下优先级计算有效配置：Schema 默认值、内置通用模板、显式部署档案、已有配置保留值、单次 CLI 覆盖。`${变量}` SHALL 在原始 JSON 完成环境变量解析后再进入 Schema 校验，解析后的值 MUST NOT 写回文件。系统 SHALL 提供脱敏的 `config export-profile`，且导出不得包含秘密、设备/会话运行状态或机器专属路径。

#### Scenario: 从现有环境导出可审阅档案

- **WHEN** 运维人员对现有服务端配置执行 `nanobot config export-profile --output <路径>`
- **THEN** 系统生成标记为需审阅的非敏感部署档案，保留可迁移的配置意图，并移除秘密、运行状态和机器专属路径

#### Scenario: 数字环境变量在校验前解析

- **WHEN** 部署档案将网关端口声明为 `${NANOBOT_GATEWAY_PORT}` 且环境变量值为有效数字
- **THEN** 系统在 Schema 校验前将其解析为数字，并且真实数字值不被写回档案或真实配置

### Requirement: 安全配置初始化

系统 SHALL 提供 `nanobot config init`，从 Schema 默认值和可选无密钥模板创建服务端声明配置。目标配置已存在时，命令 MUST 拒绝覆盖，除非用户显式指定 `--force`。初始化 MUST NOT 创建、导入或恢复连接器设备、授权、审计或 token 运行状态。

#### Scenario: 新服务器首次初始化

- **WHEN** 新服务器在空目标路径执行 `nanobot config init --template <无密钥模板>`
- **THEN** 系统创建带当前版本号的声明配置与所需工作区，并且设备登记与授权运行状态保持为空

#### Scenario: 已有配置受保护

- **WHEN** 目标配置文件已存在且用户未提供 `--force`
- **THEN** 命令失败并且不修改原文件

### Requirement: 校验与部署诊断

系统 SHALL 提供只读的 `nanobot config validate` 和 `nanobot config doctor`。`validate` SHALL 校验 JSON、Schema、版本、模板限制和环境变量引用；`--strict` SHALL 对运行必需的环境变量报错。`doctor` SHALL 检查部署前提并给出建议，MUST NOT 修改配置或运行状态。

#### Scenario: 严格校验发现缺失密钥

- **WHEN** 配置引用 `${PROVIDER_API_KEY}` 且环境变量未设置，用户执行 `nanobot config validate --strict`
- **THEN** 命令以非零退出码失败，并指出缺失变量和引用位置

#### Scenario: 诊断不改写文件

- **WHEN** 用户执行 `nanobot config doctor`
- **THEN** 系统报告配置、工作区、端口和连接器检查结果，且不修改任何配置或运行状态文件

### Requirement: 生产安全写入与运行生效语义

所有会写服务端声明配置的命令 SHALL 针对目标配置取得排他锁，并使用受限权限备份、`fsync` 临时文件和原子替换。命令 SHALL 支持 `--dry-run` 和机器可读的脱敏结果。严格校验 MUST 拒绝已知敏感字段中的内联秘密与权限无法保障的配置文件。配置刷新 SHALL 明确告知网关需要重启才会使用新配置，MUST NOT 宣称热更新已经生效。

#### Scenario: 写入失败保留旧配置

- **WHEN** 刷新配置时备份、写入或原子替换任一步失败
- **THEN** 原声明配置保持可用，命令以非零退出码失败，并且输出不包含秘密内容

#### Scenario: 网关运行时刷新

- **WHEN** 网关正在运行，用户成功执行 `nanobot config refresh`
- **THEN** 命令报告配置已落盘但尚未生效，并给出重启网关的下一步命令

### Requirement: 模板与运行状态隔离

系统发布的配置模板 MUST 不包含 API Key、设备 token、配对码、设备 ID、授权记录、审计数据、SecretStore 值或用户机器私有目录。模板加载器 SHALL 拒绝包含禁止字段或秘密值的模板。

#### Scenario: 含设备 token 的模板被拒绝

- **WHEN** 用户尝试使用包含 `deviceToken` 的模板执行初始化
- **THEN** 初始化失败，目标配置和设备运行状态均不被创建或修改
