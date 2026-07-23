# Tasks: add-portable-configuration-bootstrap

## 0. 前置约束与基线

- [ ] 0.1 确认 `add-connector-local-tools`、`add-connector-mcp-proxy`、`add-connector-desktop-control` 的实现状态和未归档变更不会与本提案冲突。
- [ ] 0.2 确认现有 `onboard --refresh`、自定义 `--config` 多实例路径、环境变量插值和连接器配对的回归基线。
- [ ] 0.3 定义模板敏感字段策略：`deviceToken`、`nodeId`、配对码、授权记录、审计、SecretStore 值和 API Key 不得进入模板或示例。
- [ ] 0.4 完成配置与连接器身份迁移的威胁建模：模板投毒、路径穿越、符号链接、并发写入、备份泄露、重配对降级、证书中间人和不安全凭据回退。

## 1. 服务端配置版本与生命周期

- [ ] 1.1 在 `nanobot/config/schema.py` 为根配置增加 camelCase 序列化的 `schemaVersion`，定义当前版本常量与旧无版本配置的兼容值。
- [ ] 1.2 在 `nanobot/config/loader.py` 将 `_migrate_config` 重构为按版本顺序、幂等执行的迁移链；保留现有字段迁移行为。
- [ ] 1.3 新增 `nanobot/config/bootstrap.py`：模板读取、敏感内容拒绝、初始化、刷新、原子备份/写入、只读校验、来源追踪与诊断数据收集。
- [ ] 1.4 定义并实现来源优先级：Schema 默认值 < 内置模板 < 显式部署档案 < 既有配置保留值 < 单次 CLI 覆盖；原始 JSON 在 Pydantic 校验前解析 `${变量}`，解析值绝不回写。
- [ ] 1.5 实现 `validate` 的普通与 `--strict` 两级校验：严格模式拒绝缺失的运行必需变量和已知敏感字段的内联秘密；全部错误必须只显示字段路径，不显示秘密值。
- [ ] 1.6 实现 `doctor` 的工作区、配置路径/DACL 或 POSIX 权限、端口、环境变量、连接器开关、活动网关与运行状态检查；它不得改写任何文件，端口检查必须标注为建议性结果。
- [ ] 1.7 为所有写操作实现目标级排他锁、临时文件 `fsync`、原子替换、受限权限备份和无秘密变更记录；支持 `--dry-run` 与机器可读 `--json` 输出。

## 2. 服务端 CLI、模板与文档

- [ ] 2.1 新增 `nanobot/cli/config_commands.py`，实现 `nanobot config init/refresh/validate/doctor` 的 Typer 命令、退出码和人类可读输出。
- [ ] 2.2 在 `nanobot/cli/commands.py` 注册命令组；让 `onboard` 与 `onboard --refresh` 复用共享初始化/刷新服务，保持现有参数兼容。
- [ ] 2.3 新增 `nanobot/config/templates/config.example.json`，覆盖主配置与保守的 `connector` 默认值，且只引用环境变量、不含真实秘密；定义 `deployment/profiles/` 项目档案与 `.env.example` 的版本控制规则。
- [ ] 2.4 实现 `nanobot config export-profile`：脱敏导出、去除运行状态/机器私有路径、生成“需审阅”标记；为自定义 provider 和发现到的 channel plugin 保留有效非敏感配置与默认值合并。
- [ ] 2.5 更新 `pyproject.toml`，保证模板在 wheel、源码安装和打包运行时均可发现。
- [ ] 2.6 更新 `.gitignore` 与中文文档，区分可提交模板、不可提交真实配置、服务器运行状态和连接器本机状态；明确刷新后需要重启网关。

## 3. 连接器本机初始化与工具档案

- [ ] 3.1 在 `connector/nanobot_connector/config.py` 添加本机配置版本、旧版本迁移和“未配对安全默认值”；不改变现有已配对配置的行为，并对敏感配置文件实施平台权限检查。
- [ ] 3.2 新增 `connector/nanobot_connector/bootstrap.py`：本机目录初始化、档案发现、静态校验、可执行文件预检、诊断和结果模型。
- [ ] 3.3 在 `connector/nanobot_connector/tools.py` 实现档案导入/导出与 `fail/skip/replace` 冲突策略；失败前不写 `tools.json`，替换时不得修改 SecretStore，导出时拒绝内联凭据和机器专属路径。
- [ ] 3.4 新增 Windows、Linux 基础档案及可选浏览器自动化档案；对仅启动 GUI 的工具明确标记为 `completion=launch`，不得宣称可返回自动化结果；档案不得含 Shell hook、任意代码或自动下载行为。
- [ ] 3.5 新增 `connector/nanobot_connector/credentials.py`，将 SecretStore 接入操作系统凭据库；实现历史 `secrets.json` 的本机显式迁移、成功验证和可确认删除，严格模式下拒绝不安全回退。
- [ ] 3.6 在 `connector/nanobot_connector/pairing.py` 实现候选配置式重配对、失败不变、不同服务器的显式 `--replace-server` 确认、地址规范化与真实证书指纹比对；`--insecure` 在严格诊断中失败。
- [ ] 3.7 在 `connector/nanobot_connector/cli.py` 添加 `init`、`tool import-template`、`tool export-template`、`doctor` 和重配对确认；保留 `pair`、`allow`、`tool add` 等现有命令。
- [ ] 3.8 在 GUI 首次启动和重配对流程中展示档案预检、旧/新服务器、TLS 状态与明确确认；不自动配对、共享目录或开启高风险能力。
- [ ] 3.9 更新连接器打包配置，确保模板与凭据后端依赖进入 CLI、GUI 和 PyInstaller 发布物。

## 4. 迁移与安全行为验证

- [ ] 4.1 服务端初始化测试：首次创建、已有文件拒绝、显式覆盖、模板合并、敏感字段拒绝、原子失败回滚。
- [ ] 4.2 服务端迁移测试：无版本旧配置、逐版本升级、重复刷新幂等、已有值保留、环境变量不落盘、`validate --strict` 失败路径。
- [ ] 4.3 服务端安全写入测试：并发锁、写入中断/替换失败、备份权限、模板路径穿越/符号链接、脱敏输出和 `--dry-run` 不写入。
- [ ] 4.4 服务端诊断测试：配置错误、缺少环境变量、内联秘密、不可写工作区、端口冲突、活动网关重启提示和连接器高风险开关提示。
- [ ] 4.5 连接器测试：未配对初始化、档案导入成功、缺可执行文件拒绝、同名冲突、替换不影响凭据、旧本机配置迁移、档案导出脱敏。
- [ ] 4.6 重配对与凭据测试：失败时旧配置不变、不同服务器需确认、指纹不匹配拒绝、严格模式拒绝 `--insecure`、凭据库迁移和无安全后端失败。
- [ ] 4.7 迁移流程测试：同服务器升级保留设备运行状态；新服务器初始化不创建或导入设备/授权状态；重新配对后生成新身份。
- [ ] 4.8 GUI/CLI 一致性测试：同一档案在 GUI 与 CLI 中具有相同预检和冲突语义；重配对确认和 TLS 报告一致。

## 5. 收尾

- [ ] 5.1 编写中文部署 Runbook：首次部署、从当前实例导出/审阅部署档案、同机升级、换服务器、增加设备、重新配对、撤销旧服务器设备、回滚与密钥轮换。
- [ ] 5.2 运行 `ruff check nanobot/ connector/`、相关 pytest、连接器测试和 WebUI 测试；新增核心逻辑覆盖率不低于项目约定。
- [ ] 5.3 安全评审：检查模板泄密、覆盖保护、环境变量日志脱敏、设备 token 不可迁移、真实 TLS 指纹校验、凭据库/权限、工具档案不执行代码及默认最小权限。
