# Design: add-portable-configuration-bootstrap

## 背景与边界

配置按所有权分为三层，三层不得混淆：

| 层级 | 典型位置 | 可迁移性 | 内容 |
| --- | --- | --- | --- |
| 服务端声明配置 | `~/.nanobot/config.json` 或 `--config` 指定文件 | 可由无密钥模板重建 | 模型、渠道、网关、连接器策略和非敏感运行参数 |
| 服务端运行状态 | 工作区下的 `connector/` 等目录 | 同机升级保留；换服务器默认不迁移 | 设备登记、授权、审计、缓存、会话等 |
| 连接器本机状态 | `~/.nanobot-connector/` | 工具档案可导入；身份不可复制 | 服务端地址、设备 token、共享目录、本机工具、凭据和本机审计 |

“可迁移”不等于复制用户目录。可迁移的是无秘密的配置意图和工具定义模板；设备身份与授权是一次配对建立的信任关系，必须在目标环境重新建立。

另有一类受版本控制的**部署档案**：它属于项目/运维团队，而不是 nanobot 内置默认值。部署档案保存环境的非敏感意图，例如模型选择、网关端口、渠道开关、连接器限额和允许能力；它由 `config init --template` 显式选择。内置通用模板只用于起步，不应被误当作生产环境的完整档案。

## 目标与非目标

### 目标

- 新服务器可在不编辑 JSON 的情况下生成可校验的服务端配置。
- 旧配置可安全升级，新增字段能补齐，已有有效值不被覆盖。
- 新设备可初始化本机目录、按需导入工具档案，并在导入前得到依赖检查结果。
- 部署、同机升级、换服务器和新增设备的行为可预测、可审计、可回滚。
- 模板、环境变量、运行状态和密钥的职责清晰。

### 非目标

- 通过模板分发 QQ 登录态、浏览器 Cookie、MCP 凭据或任何可用于冒充设备的资料。
- 支持把旧服务器的设备信任状态一键迁移到新服务器。
- 替换现有交互式 `onboard --wizard`；它继续负责首次模型/渠道引导。
- 用配置档案决定聊天会话默认选择哪一台设备。

## 关键决策

### 1. 保留 JSON 为结构化配置源，环境变量只注入秘密或部署差异

JSON 适合表达嵌套配置、限额、开关和工具参数模式；环境变量适合 API Key、令牌和部署系统注入的少量差异。模板可使用 `${NAME}` 引用，但 `init` 和 `refresh` 不会把解析出的环境变量值回写进 JSON。解析发生在原始 JSON 到 Pydantic Schema 校验之前，以便数字、布尔值和嵌套字段也能安全使用环境变量引用。

`validate` 分两级：默认模式验证配置形状和引用语法；`--strict` 同时要求所有被引用且运行必需的环境变量已设置。这样镜像构建阶段可验证模板，部署阶段可验证密钥注入。

有效配置来源固定为：**Schema 默认值 < 内置通用模板 < 显式选择的部署档案 < 已有声明配置的保留值 < 单次 CLI 覆盖**。环境变量仅解析 `${NAME}` 占位符；对于现有 `BaseSettings` 的兼容环境输入，命令必须显示其来源，且不得让来源优先级变得隐式。`config validate --strict` 对已知敏感字段中的明文值发出失败；普通校验仅警告以兼容现有部署。

### 2. 部署档案是显式、可审阅的输入

项目仓库可提交 `deployment/profiles/<环境>.json` 和 `.env.example`，但不得提交实际 `.env`、密钥、运行状态或设备目录。`nanobot config export-profile` 从已存在配置导出一个标记为“需审阅”的档案：它保留非敏感结构和 `${NAME}` 形式的引用，移除已知秘密、设备/会话运行状态和机器专属路径；对无法安全泛化的路径给出占位符与诊断，而非悄悄复制。

连接器工具也提供等价的档案导出，但只导出工具定义的可移植字段。绝对可执行文件路径必须转换为“平台候选路径/发现规则”，否则导出失败并要求操作者建立专用档案。档案不是可执行代码；导入时不允许任意 Python、Shell hook 或网络下载。

### 3. 版本号只管理声明配置，不管理运行状态

根配置写入 `schemaVersion`，连接器本机配置和工具注册表各自有版本。迁移函数接收原始 JSON、按版本逐步升级，再交给 Pydantic 校验。版本迁移必须幂等，并以原子写入落盘。

`devices.json`、`grants.json`、审计与 token 不使用此迁移机制：它们属于安全状态，而非用户可携带模板。

### 4. 新命令收敛生命周期，旧命令保持兼容

新增命令的语义如下：

```text
nanobot config init [--config PATH] [--template PATH] [--force]
nanobot config refresh [--config PATH] [--dry-run]
nanobot config validate [--config PATH] [--strict]
nanobot config doctor [--config PATH] [--strict]
nanobot config export-profile [--config PATH] --output PATH

nanobot-connector init [--profile NAME] [--home PATH]
nanobot-connector tool import-template NAME [--on-conflict fail|skip|replace]
nanobot-connector tool export-template NAME --output PATH
nanobot-connector doctor [--strict]
```

`onboard` 使用同一套初始化/刷新服务；其现有交互模式、`--wizard` 与 `--refresh` 保持向后兼容。`init` 不启动网关，`doctor` 不写入配置，所有覆盖操作都要求显式确认或 `--force`。

每个命令均支持机器可读的 `--json` 输出和稳定退出码。写操作以配置目标为粒度获取排他锁，预检、备份、`fsync` 临时文件、原子替换依次完成；失败时保留旧文件。备份文件与真实配置具有同等受限权限，命令输出、诊断和变更记录只保存字段路径、版本、哈希与操作结果，严禁打印或记录秘密值。Windows 必须设置/验证当前用户 DACL；POSIX 必须使用 0600。无法保证权限时，严格模式必须失败。

### 5. 工具档案是显式导入、默认拒绝覆盖

工具档案只描述 `ToolDef` 的非敏感部分，例如名称、说明、参数模式、审批方式、完成方式与可执行文件探测规则。档案不能内嵌 `secrets` 的实际值；凭据仍通过本机 SecretStore 单独配置。

导入首先静态校验档案，再检查可执行文件、参数模板和平台兼容性。检查失败时不改变 `tools.json`。同名工具默认 `fail`，用户可显式选择 `skip` 或 `replace`；`replace` 只替换工具定义，不碰本机密钥。

`windows-browser-automation` 是“可选的自动化脚本档案”，不应把单纯启动 Chrome 的 GUI 命令包装成可完成检索任务的工具。模板必须暴露结构化查询参数与结构化结果契约，或明确标记为仅启动程序。

### 6. 连接器重配对是一次身份轮换事务

当前连接器的 `pair_device` 在 HTTP 请求成功前就改写 `server`、证书相关字段，失败后会留下不一致状态；该行为必须修复。重配对先在内存中构造候选配置、完成地址规范化、TLS 系统校验或真实证书指纹校验、配对码兑换，最后一次性写入 `server`、`nodeId`、`deviceToken`、证书绑定与名称。任何失败都必须保留原配置字节语义不变。

当目标服务器规范化地址不同于已配对服务器时，CLI 必须要求交互式确认，自动化场景必须显式提供 `--replace-server`；GUI 必须展示旧/新服务器和“会使旧本机身份失效”的提示。`--insecure` 只能作为显式临时开发选项，严格诊断和生产文档必须拒绝或标记为不合规。提供证书指纹时必须实际比对服务端证书哈希，不能仅关闭 TLS 验证。

工具 SecretStore 必须以操作系统凭据库作为生产后端。历史 `secrets.json` 可在设备本机显式导入凭据库，校验成功后由用户确认删除；不得经模板、导出、日志、配对或远程协议传输。没有可用安全凭据后端时，严格模式失败，普通模式只能报告受限状态而不能静默当作生产安全。

### 7. 迁移流程按环境区分

#### 同一服务器升级

1. 停止网关并备份声明配置与运行数据。
2. 执行 `nanobot config refresh`。
3. 执行 `validate --strict` 和 `doctor`。
4. 重启网关；刷新不会热加载到已运行进程。

配置迁移保留已有值；工作区路径不变时，设备 token、配对和授权状态保留。

#### 迁移到新服务器

1. 安装新版本与依赖，注入新服务器的生产密钥。
2. 使用受版本控制的无密钥模板执行 `nanobot config init`。
3. 执行 `validate --strict` 和 `doctor` 后启动网关。
4. 在 WebUI 为每台既有连接器生成新的单次配对码。
5. 在每台电脑保留本机工具与凭据，运行 `nanobot-connector pair --replace-server` 指向新服务器并重新配对；旧服务器设备记录在完成切换后由管理员撤销。
6. 根据最小权限原则重新授予执行、MCP 或桌面控制权限，并逐台验证。

新服务器不会接纳旧服务器的 `devices.json`、`grants.json`、审计或 `deviceToken`。若使用相同域名，客户端仍必须完成重新配对，而不是仅修改 DNS。

#### 新增连接器设备

1. 安装连接器并运行 `nanobot-connector init`。
2. 显式选择适合平台的工具档案；执行 `doctor`，处理缺失程序或权限。
3. 在 WebUI 生成配对码，在本机执行 `pair`。
4. 显式添加共享目录；执行、MCP、桌面能力均保持关闭，直到服务端与本机两端分别开启。

## 文件落点

### 修改

- `nanobot/config/schema.py`：主配置版本字段与别名。
- `nanobot/config/loader.py`：版本迁移、原子保存、模板合并、校验基础能力。
- `nanobot/cli/commands.py`：注册配置命令组，并让 `onboard` 委托共享服务。
- `connector/nanobot_connector/config.py`：本机配置版本、安全初始化、权限检查与原子保存。
- `connector/nanobot_connector/pairing.py`：事务化重配对、地址规范化和真实证书指纹校验。
- `connector/nanobot_connector/tools.py`：工具档案载入、校验、预检、冲突处理和安全凭据后端适配。
- `connector/nanobot_connector/cli.py`：连接器初始化、档案导入/导出、重配对确认和诊断命令。
- `connector/nanobot_connector/gui.py`：首次启动档案选择和依赖诊断。
- `pyproject.toml`：将模板作为包数据发布。
- `.gitignore`、`README.md`、`START.md`：忽略规则和中文部署文档。

### 新增

- `nanobot/config/bootstrap.py`：无 UI 的服务端配置生命周期服务。
- `nanobot/cli/config_commands.py`：Typer 命令适配层。
- `nanobot/config/templates/config.example.json`：服务端无密钥模板。
- `connector/nanobot_connector/bootstrap.py`：连接器本机初始化、档案发现与诊断服务。
- `connector/nanobot_connector/credentials.py`：操作系统凭据库与本机历史凭据迁移适配层。
- `connector/nanobot_connector/templates/`：按平台发布的工具档案。
- 对应的 `tests/config/`、`tests/cli/`、`connector/tests/` 测试文件。

## 风险与缓解

- **刷新误覆盖配置**：默认仅补齐和迁移；覆盖必须显式 `--force`，写入前创建备份并采用临时文件原子替换。
- **并发命令或断电造成配置损坏**：目标级锁、`fsync`、原子替换、备份和启动前校验；任何写入失败保留旧文件。
- **模板携带秘密**：模板加载器拒绝 token、设备身份与凭据值；CI 检查模板敏感键和值模式。
- **内联秘密与备份泄露**：严格模式拒绝已知敏感字段的明文值；配置、备份和日志执行权限限制与脱敏，导出不携带秘密。
- **工具模板在不同机器不可用**：导入先预检，不通过不写入；诊断输出具体缺失项。
- **把“迁移服务器”误解为“迁移信任”**：命令和文档固定展示重新配对步骤，服务器端禁止导入旧设备 token。
- **重配对失败破坏现网连接**：候选配置在内存中完成网络与 TLS 验证后才原子切换；失败保留旧身份。
- **配置修改后误以为已生效**：命令输出明确标注是否需重启，部署 Runbook 将刷新与重启作为两个步骤。
- **环境变量在服务管理器中缺失**：`validate --strict` 与 `doctor` 在启动前报错，文档给出服务环境注入方式。

## 回滚

配置迁移在写入前生成带时间戳的备份；回滚可停止网关、恢复备份、重启旧版本。关闭连接器开关或不导入工具档案不会影响既有文件工具和聊天渠道。新服务器迁移失败时可继续运行旧服务器；尚未重新配对的设备不会连接到新服务器。
