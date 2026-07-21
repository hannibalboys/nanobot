# Design: add-forest-theme

## Context

- 当前 webui 的配色体系：`globals.css` 在 `:root`（浅色）与 `.dark`（深色）下定义**裸 HSL 三元组** CSS 变量（如 `--background: 0 0% 100%`），Tailwind 配置以 `hsl(var(--x))` 消费并支持 alpha 组合（如 `hsl(var(--muted-foreground) / 0.26)`）；`darkMode: ["class"]`，由 `useTheme.ts` 在 `<html>` 上切 `.dark`，localStorage 键 `nanobot-webui.theme`。
- CerebraBot 的绿色主题：`webui/src/globals.css` 以**十六进制**定义 Material Design 3 风格 token（`surface-container-*`、`on-primary-container` 等），浅色 "E-Ink Terminal"、深色 "Deep Forest"，另有专用 Tailwind 颜色映射、字体（Space Grotesk 等）与 CRT 扫描线装饰。
- 两边变量命名与格式完全不同，无法直接拷贝，需要做 token 映射 + 格式转换。

## Goals / Non-Goals

**Goals:**

- 将 CerebraBot 绿色调色板移植为 nanobot WebUI 的第二套主题 **Forest（森语）**，浅色/深色变体齐全。
- 外观（light/dark）与主题（default/forest）正交，独立切换、独立持久化。
- 零依赖新增、零 Tailwind 配置改动、零后端改动。

**Non-Goals:**

- 不移植 CerebraBot 的字体、CRT 扫描线、terminal-pulse 动画等非配色装饰。
- 不做主题跟随系统、自定义主题编辑器、每主题独立圆角/密度。
- 不做后端持久化或跨设备同步。

## Decisions

### D1: 用 `data-theme="forest"` 属性应用主题，而非 class

`<html>` 上已有 `.dark` class 承担外观语义；主题用 `data-theme` 属性区分维度更清晰，CSS 选择器为 `[data-theme="forest"]`（浅色）与 `[data-theme="forest"].dark`（深色）。默认主题不设属性，`:root`/`.dark` 原有变量块一字不改，天然保证「默认主题零回归」。

*备选：`.theme-forest` class。可行但与 `.dark` 混在 classList 里语义混杂，且未来多主题时属性单值替换比 class 增删更不易出错。*

### D2: 保持 HSL 三元组变量体系，转换 CerebraBot 的 hex 值

Tailwind 配置消费 `hsl(var(--x))` 且多处依赖 `/ alpha` 组合，改成 hex 需要动 Tailwind 配置并破坏 alpha 用法。因此将 CerebraBot 的 hex 逐一转换为 HSL 三元组（机械转换，实现时脚本/工具辅助），只在 `globals.css` 追加两个变量块。

### D3: MD3 token → shadcn 语义变量映射表

CerebraBot 的 MD3 token 多于 nanobot 的 shadcn 变量，按语义就近映射（hex 为源值，实现时转 HSL）：

| nanobot 变量 | Forest 浅色（E-Ink） | Forest 深色（Deep Forest） |
|---|---|---|
| `--background` | surface `#e7ebe5` | surface `#091610` |
| `--foreground` | on-surface `#111e18` | on-surface `#d7e6dc` |
| `--card` / `--popover` | surface-container-lowest `#f2f5f0` | surface-container-low `#111e18` |
| `--card-foreground` / `--popover-foreground` | `#111e18` | `#d7e6dc` |
| `--primary` | primary `#005236` | primary `#c5ebd5` |
| `--primary-foreground` | on-primary `#ffffff` | on-primary `#143728` |
| `--secondary` / `--muted` | surface-container `#dde3dc` | surface-container `#15221c` |
| `--secondary-foreground` / `--accent-foreground` | `#111e18` | `#d7e6dc` |
| `--muted-foreground` | on-surface-variant `#375949` | on-surface-variant `#c1c8c2` |
| `--accent` | surface-container-high `#d3d9d2` | surface-container-high `#202d26` |
| `--destructive` | error `#ba1a1a` | error-container `#93000a` |
| `--destructive-foreground` | on-error `#ffffff` | on-error-container `#ffdad6` |
| `--border` / `--input` | outline-variant `#b6beb7` | outline-variant `#414844` |
| `--ring` | primary `#005236` | surface-tint `#a9cfba` |
| `--inline-token-highlight` | tertiary `#005f40` | tertiary-container `#4edea3` |
| `--sidebar` | cb-sidebar `#dde3dc` | cb-sidebar `#0d1a14` |
| `--sidebar-foreground` | `#111e18` | `#d7e6dc` |
| `--sidebar-accent` | `#d3d9d2` | `#202d26` |
| `--sidebar-accent-foreground` | `#111e18` | `#d7e6dc` |
| `--sidebar-border` | `#b6beb7` | `#414844` |

滚动条变量（`--scrollbar-thumb*`）由 `--muted-foreground` 派生，无需单独定义；两个变量块内同步设置 `color-scheme: light/dark`。实现阶段允许对个别 token 做小幅视觉微调，但基调 MUST 忠于源调色板。

### D4: 扩展 `useTheme.ts` 而非新建 hook

在现有 hook 内新增 `colorTheme: "default" | "forest"` 状态、`setColorTheme`，localStorage 键 `nanobot-webui.color-theme`；`applyTheme` 一并负责 `data-theme` 属性的设置/移除。外观与主题同源管理。

`ThemeProvider` / `useThemeValue` 的签名**保持不变**（仍只承载 `"light" | "dark"`）：现有消费方 `CodeBlock.tsx` 与 `DiffSyntaxHighlight.tsx` 仅用它选择代码高亮的明暗主题，与配色主题无关；`colorTheme` 由 `App.tsx` 经 props 传给 `SettingsView`，不进 context。注意 `useTheme` 返回值形状变化会波及 mock 该模块的测试（如 `app-layout.test.tsx`）。

*备选：独立 `useColorTheme` hook + 独立 provider。会在 `App.tsx` 增加一层嵌套与一套 context，对两个字段的小状态不值得。*

### D5: i18n 键的取舍

现有 `settings.rows.theme`（"Theme/主题"）语义恰好归新主题行使用：改写其 help 文案为主题切换说明，新增 `settings.rows.appearance` + `settings.help.appearance` 给浅色/深色行，新增 `settings.values.themeDefault` / `settings.values.themeForest`（en: "Default"/"Forest"，zh-CN: "默认"/"森语"）。10 个语言包（en、zh-CN、zh-TW、ja、ko、es、fr、pt-BR、vi、id）同步补齐，并更新 `webui/src/tests/i18n.test.tsx` 的必需键清单（该测试强制所有语言包含有清单内键）。

新主题行的切换控件直接复用 `SettingsView.tsx` 内既有的 `SegmentedControl` 组件（密度/活动详情等行同款），不再手写胶囊按钮。

### D6: 首帧无闪烁

`webui/index.html` 已有预挂载内联脚本：React 挂载前读取 `nanobot-webui.theme` 并给 `<html>` 加 `.dark`。同一脚本扩展为再读取 `nanobot-webui.color-theme`，值为 `forest` 时设置 `document.documentElement.dataset.theme = "forest"`。boot splash 的底色目前硬编码（`body { background: #ffffff }` / `html.dark body { background: #1a1a1a }`），为 Forest 追加 `html[data-theme="forest"]` 两条覆盖（`#e7ebe5` / `#091610`），避免启动瞬间白/灰底闪烁。

## Risks / Trade-offs

- [组件中存在硬编码颜色（品牌 logo、图表、代码高亮等）在 Forest 下不协调] → 实现后对主要视图（会话、设置、侧栏、Markdown 渲染）做一轮视觉走查，仅修正明显冲突处，不追求像素级统一。
- [`.dark .host-window-shell` 等原生宿主玻璃效果使用固定 rgba，Forest 深色下可能偏灰] → 走查时验证，必要时为 `[data-theme="forest"].dark` 追加覆盖。
- [MD3→shadcn 映射是有损的（MD3 token 更多）] → 以映射表为基线，接受与 CerebraBot 原版存在细微差异；用户核心诉求是"绿色基调"而非逐像素还原。
- [代码高亮配色不随主题变化：`CodeBlock` / `DiffSyntaxHighlight` 只按明暗选择高亮主题，Forest 下代码块仍是默认高亮调色板] → 接受；代码块底色（`--muted` 等容器色）已随主题变绿，语法着色保持通用调色板可读性更稳。
- [`webui/index.html` 的 `<meta name="theme-color">` 硬编码 `#fafaf9`/`#161618` 且跟随系统偏好而非应用主题] → 现状对 light/dark 也不精确，属既有限制，本变更不扩大处理；如需可在走查时顺带评估。
- [文本选区 `::selection` 使用 `bg-primary/15`] → 随 Forest 的 `--primary` 自动变绿，无需额外处理（记录以免走查误报）。

## Migration Plan

纯前端增量，无数据迁移。`bun run build` 后照常打入 wheel。回滚即还原代码；用户 localStorage 中残留的 `nanobot-webui.color-theme` 键在旧版本中无人读取，无副作用。

## Open Questions

- 无。主题命名（Forest/森语）如需调整仅涉及 i18n 文案，不影响结构。
