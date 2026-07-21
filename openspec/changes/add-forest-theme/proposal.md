# Proposal: add-forest-theme

## Why

当前 WebUI 仅有一套中性灰配色（shadcn neutral），只能在浅色/深色两种外观间切换。旧版 fork CerebraBot 中打磨过一套广受好评的绿色主题（浅色 "E-Ink Terminal" / 深色 "Deep Forest"），希望将其移植进当前版本，作为可切换的第二套配色主题 **Forest（森语）**，让用户在「设置 → 外观 → 界面」中自由选择。

## What Changes

- WebUI 新增配色主题 **Forest（森语）**：移植 CerebraBot 的绿色调色板（浅色 E-Ink 米绿纸感 + 深色 Deep Forest 墨绿），转换为 nanobot 现有的 HSL 三元组 CSS 变量体系，不改动 Tailwind 配置。
- 「设置 → 外观 → 界面」分区调整：
  - 现有「主题」行（浅色/深色胶囊切换）更名为「外观」（Appearance）。
  - 在其后新增「主题」（Theme）行：胶囊切换 `默认 | 森语`（Default | Forest）。
  - 外观与主题**正交**：Forest 主题同时提供浅色与深色变体，跟随外观开关切换。
- 主题选择持久化到浏览器 localStorage（与现有 light/dark 机制一致，不经后端）。
- 通过 `<html>` 上的 `data-theme="forest"` 属性应用主题，与现有 `.dark` class 机制叠加。
- 全部 10 个语言包（en、zh-CN、zh-TW、ja、ko、es、fr、pt-BR、vi、id）补充新文案。
- 首帧无闪烁：`webui/index.html` 既有的预挂载内联脚本（React 挂载前应用 `.dark`）同步扩展为读取主题键并设置 `data-theme`，boot splash 底色为 Forest 提供覆盖。
- 不做：主题跟随系统、自定义主题编辑器、后端持久化、CerebraBot 的字体/CRT 扫描线等非配色装饰效果。

## Capabilities

### New Capabilities

- `webui-theming`: WebUI 配色主题系统——主题选择 UI（外观设置内的主题切换）、Forest 主题的浅色/深色调色板、主题持久化与 DOM 应用机制。

### Modified Capabilities

（无 —— `openspec/specs/` 尚无 WebUI 外观相关规格建档；现有浅色/深色外观切换行为保持不变，仅 UI 文案由「主题」更名为「外观」。）

## Impact

- **修改代码**（全部位于 `webui/`，纯前端）：
  - `webui/src/globals.css`：新增 `[data-theme="forest"]` 与 `[data-theme="forest"].dark` 两个变量块。
  - `webui/src/hooks/useTheme.ts`：扩展出 `colorTheme` 状态（`default | forest`）、localStorage 持久化、`data-theme` 属性应用。
  - `webui/src/components/settings/SettingsView.tsx`：`AppearanceSettings` 新增主题切换行。
  - `webui/src/App.tsx`：接线新的 theme 状态到 SettingsView。
  - `webui/index.html`：预挂载内联脚本与 boot splash 底色。
  - `webui/src/i18n/locales/*/common.json`（10 个语言包）：新增/调整文案键；`webui/src/tests/i18n.test.tsx` 的必需键清单同步更新。
- **依赖**：零新增。
- **后端 / 打包**：无 Python 侧改动；`bun run build` 产物照常打入 wheel（`nanobot/web/dist`）。
- **兼容性**：localStorage 无主题键时默认 `default`，存量用户体验不变。
