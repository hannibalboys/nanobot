# Design: merge-upstream-main

> 本提案是一次性上游同步（merge，非 rebase、非 cherry-pick）。本文记录合并策略、冲突处置原则与回滚方案。

## Context

- 合并基线：`cf1e801a`（2026-07-27，v0.3.0 后）。`dev` 领先基线 9 个提交（全部为 connector 系列工作），上游领先 387 个提交。
- 双方均改动的重叠文件 36 个，高密度冲突点：`nanobot/config/schema.py`、`nanobot/config/loader.py`、`nanobot/agent/loop.py`、`nanobot/agent/tools/context.py`、`nanobot/channels/manager.py`、`nanobot/channels/websocket/runtime.py`、`nanobot/cli/commands.py`、`webui/src/App.tsx`、`webui/index.html`、全部 i18n locale、`README.md` / `CONTRIBUTING.md` / `pyproject.toml` / `.gitignore`。
- fork 独有资产（不得回退）：`connector/` 子项目、`nanobot/connector/` 服务端、`nanobot/agent/tools/connector.py`、forest 主题、portable 配置引导、`openspec/` 全部内容。
- 环境约束：GitHub 访问需 `-c http.proxy=http://127.0.0.1:7897`。

## Goals / Non-Goals

**Goals:**

- `dev` 完整吸收 upstream/main 387 个提交，双方历史经一次 merge commit 汇合。
- fork 自有功能（connector 全链路、主题、配置引导）行为零回退，由既有测试套件证明。
- 冲突解决过程可追溯：每个重叠文件的取舍有据可依。

**Non-Goals:**

- 不评估/改造上游新功能本身（TUI、技能市场等原样接收）。
- 不接入上游新 CI/发布流程。
- 不在本次合并中修复上游既存 bug 或开发新功能。
- 不把合并结果立即推向用户发布；合并后观察期内的修复走独立提交。

## Decisions

1. **merge 而非 rebase**：fork 的 9 个提交已推送至 `origin/dev`，rebase 改写已推送历史会破坏协作者基线。产生一个 merge commit 保留双方历史。

2. **冲突取舍分层原则**：
   - **fork 独有文件**（`connector/`、`nanobot/connector/`、`openspec/` 等）：上游未触碰，天然无冲突。
   - **共享文件、上游演进 vs fork 适配**（如 `config/schema.py`）：以上游新版本为骨架，把 fork 的增量（connector 配置段等）重放到上游结构上；禁止整文件"ours"一刀切，避免吞掉上游 387 提交中的 fix。
   - **i18n locale 文件**：以上游为准，检查 fork 是否新增过 key（connector 相关文案），有则补回。
   - **`README.md` / `CONTRIBUTING.md` / `THIRD_PARTY_NOTICES.md`**：逐段比对，保留 fork 的 connector 文档段落，接收上游其余更新。
   - **`pyproject.toml`**：版本号、依赖列表以上游为准；fork 若有额外依赖（connector 服务端依赖）保留。

3. **TUI 子系统处置**：上游 TUI 是全新目录/子项目，预期零冲突直接引入；合并后仅验证其不影响既有 Python 测试与 webui 构建，不投入适配工作。

4. **验证门禁（合并完成的定义）**：
   - `ruff check nanobot/`
   - `pytest` 全量通过（重点关注 `tests/connector/`、`tests/config/`、`tests/channels/`、`tests/webui/`）
   - `connector/` 子项目自带测试通过
   - `cd webui && bun run build && bun run test`
   - 冒烟：`nanobot gateway` 能启动，connector 注册/心跳/工具调用链路手工验证一次

5. **回滚方案**：合并完成后暂不推送，验证通过后一次性推送；若验证失败且短期无法修复，`git merge --abort`（合并中）或 `git reset --hard <合并前 dev>`（合并后未推送）回退，提案状态标记 blocked 并记录原因。

## Risks / Trade-offs

- **接收 unreleased 代码的风险**：上游 387 提交未随 release 验证。缓解：全量测试 + gateway 冒烟；残留上游问题跟上源修复。
- **merge commit 一次性吞 387 提交，回归定位难**：缓解：验证失败后按子系统二分（先 Python 测试、再 webui、再 connector 链路）。
- **后续同步节奏**：建议此后每 2–4 周或每个上游 release 重复本流程，避免再次累积到数百提交。
