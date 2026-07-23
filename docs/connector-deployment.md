# 连接器与配置的生产部署

本指南说明如何安全部署、升级和迁移 nanobot 服务端与连接器。目标是让新服务器能从
版本控制中的无密钥档案稳定初始化，而不复制设备身份、授权或本机登录态。

## 四类文件，四种处理方式

| 类型 | 示例位置 | 是否提交 Git | 是否可迁移 |
| --- | --- | --- | --- |
| 部署档案 | `deployment/profiles/production.json` | 可以，必须审阅 | 可以 |
| 服务端真实配置 | `~/.nanobot/config.json` | 不可以 | 用档案重新生成 |
| 服务端运行状态 | 工作区的 `connector/devices.json`、`grants.json`、审计 | 不可以 | 新服务器默认不迁移 |
| 连接器本机状态 | `~/.nanobot-connector/` | 不可以 | 工具定义可重建；身份与凭据不可复制 |

部署档案只能保存非敏感设置，例如模型名称、端口、限额和连接器开关。API Key 应写为
`${PROVIDER_API_KEY}` 这类引用，由系统服务、CI/CD 或密钥服务注入。设备 token、配对码、
授权、审计、浏览器 Cookie 和 QQ 登录态都不能进入档案。

## 从已有服务器生成部署档案

在现有服务器执行：

```powershell
nanobot config export-profile --output .\deployment\profiles\production.json
```

导出结果会移除已知敏感字段；仍须人工审阅后才可提交。特别检查工作区、端口、渠道开关、
模型选择和 `connector` 的高风险开关。生产档案应默认保持：

```json
{
  "connector": {
    "enabled": false,
    "allowExec": false,
    "allowMcpProxy": false,
    "allowDesktopControl": false
  }
}
```

确有业务需要时，再通过经过评审的档案分别开启能力，并在设备端完成本机授权。

## 新服务器部署

1. 安装项目与依赖，准备部署档案和服务环境变量。没有团队档案时，`nanobot config init` 会使用安装包自带的保守模板；有经过审阅的档案时，执行 `nanobot config init --template <档案>`。
2. 初始化不会覆盖已有配置；需要重建时必须显式使用 `--force`，并会生成受限权限备份。
3. 执行 `nanobot config validate --strict`。它会检查 JSON、Schema、环境变量和内联敏感值，但不会显示秘密值。
4. 执行 `nanobot config doctor --strict`，处理目录权限、端口和高风险能力提示。
5. 启动 `nanobot gateway`。
6. 在 WebUI 为每台连接器生成一次性配对码。每台设备执行 `nanobot-connector pair`；如果该设备此前连过另一台服务器，必须加 `--replace-server`。
7. 最小权限重新授予执行、MCP 与桌面控制能力。验证稳定后，在旧服务器撤销对应设备。

不要复制旧服务器的 `devices.json`、`grants.json` 或设备 token。复制这些文件会把旧信任关系带入新环境，无法安全轮换或排障。

## 同机升级与回滚

同一服务器升级版本时：

```powershell
nanobot config refresh
nanobot config validate --strict
nanobot gateway restart
```

刷新会做版本化迁移、补齐默认字段并备份旧配置；它不会热更新已运行的 gateway。若需要
回滚，停止 gateway、恢复同目录的 `.bak` 配置、再启动旧版本。工作区不变时，设备配对与
授权运行状态保留。

## 连接器本机初始化与工具

每台远程电脑都独立执行：

```powershell
nanobot-connector init
nanobot-connector tool import-template windows-browser-launch
nanobot-connector doctor --strict
```

档案导入会检查可执行文件存在性，失败时不会改写 `tools.json`。同名工具默认拒绝覆盖；
需要有意识地使用 `--on-conflict skip` 或 `--on-conflict replace`。工具档案不执行程序，
也不携带凭据值。

浏览器、QQ 等常驻 GUI 程序应配置为 `completion=launch`，因此调用只确认启动成功；它
不会自动完成搜索或业务操作。需要结构化返回结果时，应为目标业务注册受参数约束的本机
自动化脚本；需要人工登录、验证码或图形界面操作时，使用桌面控制会话。

连接器凭据存储在操作系统凭据库。历史版本留下的 `secrets.json` 需要在本机显式迁移：

```powershell
nanobot-connector tool secret migrate-legacy
# 验证无误后，才执行：
nanobot-connector tool secret migrate-legacy --delete-after-success
```

生产环境禁止 `--insecure`。自签证书场景使用 `--fingerprint`；客户端会实际比较服务器
证书 SHA-256 指纹，指纹不匹配时不会修改现有连接器身份。
