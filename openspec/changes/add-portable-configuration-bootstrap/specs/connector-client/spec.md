# connector-client 规格增量

## ADDED Requirements

### Requirement: 连接器本机安全初始化

连接器 SHALL 提供 `nanobot-connector init`，创建版本化的本机安全默认配置和空的本机注册表。初始化 MUST NOT 自动配对、生成或复用设备 token、共享目录、开启远程执行/MCP/桌面控制，或导入凭据。

#### Scenario: 新设备初始化

- **WHEN** 未配对的电脑执行 `nanobot-connector init`
- **THEN** 系统创建本机配置目录，配置处于未配对状态，工具注册表为空，且高风险能力仍关闭

### Requirement: 本机工具档案显式导入

连接器 SHALL 提供 `nanobot-connector tool import-template`，导入随连接器发布的非敏感工具档案。系统 MUST 在写入前校验档案、平台、可执行文件和参数模板；导入不得执行目标程序或写入凭据。同名工具的默认冲突策略 MUST 为 `fail`。

#### Scenario: 可用档案导入

- **WHEN** 用户显式导入与当前平台匹配、且所有前置检查通过的工具档案
- **THEN** 系统将档案中的工具定义原子写入本机 `tools.json`，但不启动任何工具也不修改 SecretStore

#### Scenario: 缺失可执行文件

- **WHEN** 工具档案声明的可执行文件在当前机器不存在
- **THEN** 导入失败并输出缺失项，现有 `tools.json` 保持不变

#### Scenario: 同名工具保护

- **WHEN** 档案中的工具名称已存在且用户未显式选择冲突策略
- **THEN** 系统拒绝导入该工具，既有定义和凭据均不被修改

### Requirement: 连接器迁移必须重新配对

连接器配置 SHALL 将设备 token、node ID、证书绑定和本机凭据视为设备私有状态。连接器 MUST NOT 提供从服务端模板、旧服务器运行数据或工具档案导入这些状态的能力。换新服务器后，设备 SHALL 使用新服务器生成的一次性配对码重新配对。

#### Scenario: 迁移到新服务器

- **WHEN** 已有连接器设备改用新服务器
- **THEN** 用户保留本机工具定义与凭据，但必须执行新的 `pair` 流程以取得新的设备身份；旧服务器 token 不被自动接受

### Requirement: 重配对必须事务化并验证 TLS 身份

连接器 SHALL 在内存中构造候选配置并完成服务器地址规范化、TLS 校验和配对码兑换后，才原子替换本机服务器地址、节点 ID、设备 token 与证书绑定。配对失败 MUST 保留原有已配对配置不变。目标服务器与已配对服务器不同时，CLI SHALL 要求显式确认；非交互调用 MUST 提供 `--replace-server`。提供证书指纹时，连接器 MUST 实际比对服务端证书哈希；`--insecure` 不得被标记为固定证书或生产安全。

#### Scenario: 重配对失败保留旧连接

- **WHEN** 已配对连接器尝试改连新服务器，但网络、TLS 或配对码校验失败
- **THEN** 本机仍保留旧服务器地址、旧节点 ID、旧 token 与旧证书绑定，且可继续连接旧服务器

#### Scenario: 指纹不匹配

- **WHEN** 用户提供的服务器证书指纹与实际服务端证书不一致
- **THEN** 连接器拒绝配对且不写入候选配置

### Requirement: 工具凭据不得随档案迁移

连接器 SHALL 将工具凭据存入操作系统凭据库。工具档案导入、导出、日志、诊断、配对和协议传输 MUST NOT 包含凭据值。历史 `secrets.json` 的迁移必须仅在本机显式发起；严格诊断在没有可用安全凭据后端或文件权限不合规时 MUST 失败。

#### Scenario: 档案导出不泄露凭据

- **WHEN** 用户导出包含 `secrets` 引用的本机工具档案
- **THEN** 导出只保留凭据标识符，不包含实际值，且输出文件不包含历史 `secrets.json` 内容

#### Scenario: 严格诊断发现不安全后端

- **WHEN** 本机不存在可用操作系统凭据库，或遗留凭据文件权限不合规，用户执行 `nanobot-connector doctor --strict`
- **THEN** 命令以非零退出码失败，并给出本机修复建议而不显示任何凭据值

### Requirement: 本机诊断

连接器 SHALL 提供 `nanobot-connector doctor`，检查本机配置版本、配对状态、共享目录、工具档案依赖、系统权限与能力开关。诊断 MUST NOT 修改本机配置、工具注册表或凭据。

#### Scenario: 诊断发现未配对

- **WHEN** 未配对设备执行 `nanobot-connector doctor`
- **THEN** 系统报告设备尚未配对，并给出生成服务端配对码和执行 `pair` 的下一步建议
