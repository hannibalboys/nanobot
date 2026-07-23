# nanobot 启动指南

## 核心概念

- `gateway` = 后端服务（Agent 大脑 + 连接器 + API），**必须启动**
- `webui` = 只打开浏览器，**依附于 gateway**，不能单独启动

**正确流程**：先启动 `gateway`（保持运行），再运行 `webui` 打开浏览器。

---

## 快速启动（开发环境）

### 1. 安装依赖

```powershell
uv pip install -e ".[dev]"
```

### 2. 启动后端服务（保持窗口开着）

```powershell
# 前台模式（能看到实时日志，Ctrl+C 停止）
uv run nanobot gateway

# 或后台模式（关掉终端也继续跑）
uv run nanobot gateway --background
```

### 3. 打开 WebUI 浏览器（新开一个终端）

```powershell
uv run nanobot webui
```

> 这个命令只是打开浏览器，并附着到已运行的 gateway。

---

## 常用命令速查

| 场景 | 命令 | 说明 |
|------|------|------|
| **启动服务** | `uv run nanobot gateway` | 前台运行，能看到日志 |
| **后台启动** | `uv run nanobot gateway --background` | 关掉终端继续跑 |
| **打开浏览器** | `uv run nanobot webui` | 只是开浏览器，依附 gateway |
| **重启服务** | `uv run nanobot gateway restart` | 改完代码必须执行 |
| **查看状态** | `uv run nanobot gateway status` | 看是否在运行 |
| **查看日志** | `uv run nanobot gateway logs` | 后台模式的日志 |
| **停止服务** | `uv run nanobot gateway stop` | 停止后台进程 |
| **CLI 测试** | `uv run nanobot agent -m "你好"` | 单条消息测试 |
| **交互 CLI** | `uv run nanobot agent` | 持续对话模式 |

---

## 重要：改代码后必须重启

**`webui` 不会重启服务！** 它只是开浏览器。改完 Python 代码后：

```powershell
# 方式1：如果前台运行，Ctrl+C 停掉，再重新启动
uv run nanobot gateway

# 方式2：如果后台运行
uv run nanobot gateway restart
```

---

## 前端开发（可选）

如果要改 WebUI 界面：

```powershell
cd webui
bun install
bun run dev    # 开发服务器，代理到 8765
```

生产构建：

```powershell
cd webui
bun run build  # 输出到 ../nanobot/web/dist
```

---

## 生产部署

### 直接运行

```powershell
# 安装
uv tool install nanobot-ai

# 初始化配置
nanobot onboard

# 启动
nanobot gateway --background
```

### 可迁移配置与连接器（推荐）

不要把 `C:\Users\<用户名>\.nanobot` 或 `C:\Users\<用户名>\.nanobot-connector`
直接复制到另一台服务器。它们可能包含 API Key、设备 token、授权和审计状态。

服务端首次部署时，使用仓库中经过审阅的无密钥档案创建真实配置：

```powershell
# 仅生成配置与工作区，不会创建已配对设备或授权记录
uv run nanobot config init

# 如果团队已将无密钥部署档案纳入版本控制，则显式使用它
uv run nanobot config init --config C:\ProgramData\nanobot\config.json --template .\deployment\profiles\production.json

# 在服务环境或密钥服务中注入 ${...} 引用的变量后校验
uv run nanobot config validate --strict
uv run nanobot config doctor --strict
uv run nanobot gateway
```

升级同一台服务器时：

```powershell
uv run nanobot config refresh
uv run nanobot config validate --strict
uv run nanobot gateway restart
```

`refresh` 只迁移已知旧字段并补齐新默认值；它会创建备份，但不会让已经运行的
gateway 热更新。切换到一台新服务器时，所有连接器均必须重新配对并重新授予高风险权限。

远程电脑首次安装连接器时：

```powershell
nanobot-connector init
nanobot-connector tool import-template windows-browser-launch
nanobot-connector doctor --strict

# 在 WebUI 生成一次性配对码后执行；切换旧服务器时加 --replace-server
nanobot-connector pair --server wss://新服务器:8765 --code 配对码
nanobot-connector allow "C:\Users\Xu\Documents"
nanobot-connector start
```

工具档案只导入经过声明和预检的工具定义，不会执行程序，也不会导入凭据。浏览器启动
档案只负责启动浏览器；要访问远程登录态并完成业务，应注册带结构化参数与结果的本机
自动化工具，或使用受控桌面会话。工具凭据默认存入操作系统凭据库；历史明文
`secrets.json` 只能在本机通过 `nanobot-connector tool secret migrate-legacy` 显式迁移。

### 注册为系统服务（Linux/macOS）

```bash
nanobot gateway install-service --manager systemd   # Linux
nanobot gateway install-service --manager launchd   # macOS
```

---

## 常见问题

### Q: 为什么 `uv run nanobot webui` 没反应？
A: 它只是打开浏览器。必须先启动 `gateway`。

### Q: 改了代码为什么不生效？
A: 必须重启 `gateway` 服务：`uv run nanobot gateway restart`

### Q: 端口被占用怎么办？
A: 杀掉残留进程：
```powershell
# Windows
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'nanobot' } | Stop-Process -Force

# 然后重新启动
uv run nanobot gateway
```

### Q: 能看到实时日志吗？
A: 用前台模式启动：`uv run nanobot gateway`（不要用 `--background`）
