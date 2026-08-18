# Proposal: merge-upstream-main

## Why

fork（hannibalboys/nanobot）的 `dev` 分支上次合并上游停留在 `cf1e801a`（2026-07-27，v0.3.0 之后）。此后上游 `HKUDS/nanobot:main` 前进了 **387 个提交**（230 fix、49 feat、36 refactor、30 test、6 perf、14 docs，截至 2026-08-18 的 `2bdb11ee`），其中 230 个修复直接关乎 connector 依赖的核心路径（config、agent loop、channels、websocket 网关）的稳定性。继续拖延只会让 36 个双方均改动的重叠文件冲突加剧，且上游正在快速演进的 TUI/WebUI 方向与本项目 connector GUI 工作交集会越来越多。

## What Changes

一次性将 `upstream/main`（387 个提交）合并入 `dev`，不做选择性摘取。上游主要新功能随合并引入：

- **终端 TUI（全新子系统）**：原生 TypeScript 终端 UI——会话导航、斜杠命令发现、交互式会话标题、模型遥测 footer、可点击运行时控制、跟进队列、跨工作区会话区分。
- **WebUI**：技能市场（SkillHub）、MCP 管理对话框、PWA 移动端安装、标签化 pane 工作台与拖拽分组、临时聊天模式、会话拖拽排序与 @引用、可信代理认证、集成 Vite 开发模式。
- **Provider**：DeepSeek Responses API 与 V4 Pro、Eden AI、OrcaRouter 网关、保留 Responses reasoning 状态与上下文压缩。
- **Agent 核心**：跨会话引用（session 互链）、可插拔 Agent Plugins、SDK 宿主集成扩展点、MCP 远程服务器浏览器 OAuth、Dream 记忆模型预设覆盖。
- **Channel**：Mattermost 线程/频道分组策略分离。
- **230 个 fix 与 36 个 refactor**：随合并全量接收。

合并冲突处理原则（详见 design.md）：

- `connector/` 子项目与 connector 相关服务端代码：以 fork 版本为准，仅吸收上游非冲突改进。
- 36 个重叠文件逐个三方比对，重点：`nanobot/config/schema.py`、`nanobot/config/loader.py`、`nanobot/agent/loop.py`、`nanobot/agent/tools/context.py`、`nanobot/channels/manager.py`、`nanobot/channels/websocket/runtime.py`、`nanobot/cli/commands.py`、`webui/src/App.tsx`、各 i18n locale 文件。
- fork 自有功能（connector 全套、forest 主题、portable 配置引导）行为不得因合并回退。

## Capabilities

### New Capabilities

无。本提案是上游同步，不定义 fork 自有新能力；上游引入的能力以其上游实现为准，不进入本仓库 openspec 规格。

### Modified Capabilities

- `connector-gateway` / `connector-client` / `connector-tools` / `connector-webui`：合并后须在引入上游 387 个提交的前提下保持既有行为不变（以既有测试与回归验证为准），并适配上游对 `config/schema.py`、`channels/manager.py`、`websocket/runtime.py` 等共享文件的演进。

## Dependencies

- 无前置 openspec 提案依赖。执行前要求 `dev` 工作区干净、`origin/dev` 已同步。
- 网络需经本机代理（`http://127.0.0.1:7897`）访问 GitHub。

## Impact

- **代码面**：387 个上游提交进入 `dev`；预期冲突集中在 36 个重叠文件（config、agent、channels、cli、webui 应用壳与 i18n）。
- **新增子系统**：上游 TUI 为全新 TypeScript 子项目，合并后需确认其构建/测试不进入本仓库既有 CI 关键路径（或按需接入）。
- **验证门禁**：合并后必须通过——`pytest` 全量（含 `tests/connector/`）、`connector/` 子项目测试、`cd webui && bun run build` 与 `bun run test`、`ruff check nanobot/`。
- **风险**：TUI 与大体量 WebUI 改动为 unreleased 代码，稳定性未经上游发布验证；接受此风险以换取不进一步落后。如合并后出现上游引入的回归，按"上游问题跟上源修复、fork 问题本地修"原则处理。
- **明确不做**：不 rebase 改写已推送历史（用 merge 保留双方提交）；不选择性 cherry-pick（避免后续合并地狱）；不在本次合并中开发 fork 新功能。
