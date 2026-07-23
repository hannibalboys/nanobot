"""Desktop UI for the nanobot connector.

Double-clicking the packaged exe opens this window. It is aimed at non-technical
device owners: everything the CLI can do (pairing, shared folders, tool registry,
MCP bridge, desktop control, arm windows) is reachable from tabs — no shell needed.

Registry changes (folders / tools / MCP servers / desktop toggle) are picked up by
the daemon only at connect time, so the GUI restarts the connection automatically
after such edits. Arm windows are re-read by the daemon on every check, so arming
from the 授权窗口 tab takes effect immediately.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from nanobot_connector.arm import CATEGORIES, ArmStore
from nanobot_connector.bootstrap import (
    ConnectorBootstrapError,
    import_template,
    initialize_connector,
    list_templates,
)
from nanobot_connector.client import build_daemon_client
from nanobot_connector.config import ConnectorClientConfig, config_path
from nanobot_connector.credentials import CredentialStoreError
from nanobot_connector.files import is_filesystem_root
from nanobot_connector.logbuf import clear as clear_log
from nanobot_connector.logbuf import log_event
from nanobot_connector.logbuf import snapshot as log_snapshot
from nanobot_connector.mcp_bridge import McpRegistry, McpServerDef
from nanobot_connector.pairing import (
    PairingError,
    normalize_pairing_code,
    normalize_server_url,
    pair_device,
)
from nanobot_connector.tools import SecretStore, ToolDef, ToolRegistry

_PAIR_CMD_RE = re.compile(
    r"--server\s+(?P<server>\S+)\s+--code\s+(?P<code>\S+)",
    re.IGNORECASE,
)
# WebUI settings shows gateway.port (often 18790) but Connector lives on websocket.port (8765).
_LEGACY_GATEWAY_PORT = ":18790"
_CONNECTOR_PORT = ":8765"

_APPROVALS = ("local", "webui", "auto")
_ARM_DURATIONS = (("15 分钟", 15 * 60), ("30 分钟", 30 * 60), ("1 小时", 3600), ("2 小时", 7200))
_ARM_LABELS = {"exec": "执行工具", "mcp": "MCP 调用", "desktop": "桌面控制"}


def _normalize_server_url(server: str) -> str:
    server = server.strip()
    if _LEGACY_GATEWAY_PORT in server:
        return server.replace(_LEGACY_GATEWAY_PORT, _CONNECTOR_PORT, 1)
    return server


def _server_needs_port_hint(server: str) -> bool:
    return _LEGACY_GATEWAY_PORT in server.strip()


def _default_share_dir() -> Path:
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def _parse_paste(text: str) -> tuple[str, str] | None:
    match = _PAIR_CMD_RE.search(text.strip())
    if not match:
        return None
    return match.group("server"), normalize_pairing_code(match.group("code"))


def _fmt_remaining(seconds: int) -> str:
    return f"{seconds // 60} 分 {seconds % 60} 秒"


class ConnectorGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._first_run = not config_path().exists()
        self.cfg = initialize_connector() if self._first_run else ConnectorClientConfig.load()
        self._client_thread: threading.Thread | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_task: asyncio.Task | None = None
        self._running = False

        root.title("nanobot 连接器")
        root.geometry("640x520")
        root.minsize(600, 460)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        header = ttk.Frame(root, padding=(16, 12, 16, 0))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="连接网关，把本机能力共享给 nanobot Agent。改动设置会自动重新连接生效。",
            wraplength=580,
        ).pack(anchor=tk.W)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 4))

        self._build_connect_tab(notebook)
        self._build_folders_tab(notebook)
        self._build_tools_tab(notebook)
        self._build_mcp_tab(notebook)
        self._build_desktop_tab(notebook)
        self._build_arm_tab(notebook)
        self._build_log_tab(notebook)

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, wraplength=600, padding=(16, 4, 16, 10)).pack(
            anchor=tk.W,
        )
        self._refresh_status()
        self._tick()
        if self._first_run:
            root.after(100, self._offer_first_run_template)

    # ------------------------------------------------------------------
    # 连接 tab
    # ------------------------------------------------------------------

    def _build_connect_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 连接 ")

        form = ttk.Frame(tab)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="网关地址").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.server_var = tk.StringVar(
            value=_normalize_server_url(self.cfg.server or "ws://127.0.0.1:8765")
        )
        ttk.Entry(form, textvariable=self.server_var).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 0)
        )
        self.port_hint_var = tk.StringVar()
        self.port_hint = ttk.Label(
            form, textvariable=self.port_hint_var, foreground="#b45309", wraplength=420
        )
        self.port_hint.grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))
        self.server_var.trace_add("write", lambda *_: self._refresh_port_hint())
        self._refresh_port_hint()

        ttk.Label(form, text="配对码").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.code_var = tk.StringVar()
        code_entry = ttk.Entry(form, textvariable=self.code_var, font=("Segoe UI", 14))
        code_entry.grid(row=2, column=1, sticky=tk.EW, padx=(8, 0))

        ttk.Label(form, text="共享文件夹").grid(row=3, column=0, sticky=tk.W, pady=4)
        folder_row = ttk.Frame(form)
        folder_row.grid(row=3, column=1, sticky=tk.EW, padx=(8, 0))
        folder_row.columnconfigure(0, weight=1)
        default_root = self.cfg.roots[0] if self.cfg.roots else str(_default_share_dir())
        self.folder_var = tk.StringVar(value=default_root)
        ttk.Entry(folder_row, textvariable=self.folder_var).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(folder_row, text="浏览…", command=self._browse_folder).grid(
            row=0, column=1, padx=(6, 0)
        )

        paste_row = ttk.Frame(tab)
        paste_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(paste_row, text="或粘贴向导命令：").pack(side=tk.LEFT)
        self.paste_var = tk.StringVar()
        ttk.Entry(paste_row, textvariable=self.paste_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )
        ttk.Button(paste_row, text="填入", command=self._apply_paste).pack(side=tk.LEFT, padx=(6, 0))

        btn_row = ttk.Frame(tab)
        btn_row.pack(pady=(16, 8))
        self.connect_btn = ttk.Button(btn_row, text="连接", command=self._connect)
        self.connect_btn.pack(side=tk.LEFT, padx=4)
        self.reconnect_btn = ttk.Button(
            btn_row, text="重新连接", command=self._reconnect, state=tk.DISABLED
        )
        self.reconnect_btn.pack(side=tk.LEFT, padx=4)

        self.pair_info_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.pair_info_var, wraplength=560).pack(anchor=tk.W)
        if self.cfg.device_token and self.cfg.server:
            self.pair_info_var.set(
                f"已配对设备 {self.cfg.node_id or ''}，填写新配对码可重新配对连接。"
            )

    def _refresh_port_hint(self) -> None:
        if _server_needs_port_hint(self.server_var.get()):
            self.port_hint_var.set("Connector 使用 WebSocket 端口 8765，不是设置页里的 18790。")
        else:
            self.port_hint_var.set("")

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if chosen:
            self.folder_var.set(chosen)

    def _apply_paste(self) -> None:
        parsed = _parse_paste(self.paste_var.get())
        if not parsed:
            messagebox.showwarning(
                "无法识别",
                "请粘贴向导中的完整命令，例如：\n"
                "nanobot-connector pair --server ws://host:18790 --code AB12CD34",
            )
            return
        server, code = parsed
        self.server_var.set(_normalize_server_url(server))
        self.code_var.set(code)

    def _connect(self) -> None:
        folder = Path(self.folder_var.get().strip()).expanduser()
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("文件夹无效", "请选择一个存在的文件夹。")
            return
        if is_filesystem_root(folder.resolve()):
            messagebox.showerror("文件夹无效", "不能共享整个磁盘根目录，请选择具体文件夹。")
            return

        self.connect_btn.config(state=tk.DISABLED)
        self.root.update_idletasks()

        try:
            target_server = _normalize_server_url(self.server_var.get())
            replacing_server = bool(
                self.cfg.server
                and normalize_server_url(self.cfg.server) != normalize_server_url(target_server)
            )
            if replacing_server and not messagebox.askyesno(
                "确认切换服务器",
                "这会替换本机当前的设备身份。旧服务器不会自动撤销此设备，"
                "请在切换验证完成后由管理员手动撤销。是否继续？",
            ):
                return
            self.cfg = pair_device(
                self.cfg,
                server=target_server,
                code=self.code_var.get(),
                replace_server=replacing_server,
            )
            entry = str(folder.resolve())
            if entry not in self.cfg.roots:
                self.cfg.roots = [entry, *self.cfg.roots]
                self.cfg.save()
            self._start_client()
            self._refresh_folders()
            self.pair_info_var.set(f"已配对设备 {self.cfg.node_id or ''}。")
            messagebox.showinfo("连接成功", f"已配对并连接。\n共享：{entry}\n请保持此窗口打开。")
        except PairingError as exc:
            messagebox.showerror("连接失败", str(exc))
        except Exception as exc:  # noqa: BLE001 - surface to desktop user
            messagebox.showerror("连接失败", str(exc))
        finally:
            self.connect_btn.config(state=tk.NORMAL)
            self._refresh_status()

    def _reconnect(self) -> None:
        self._stop_client()
        self._start_client()
        self._refresh_status()

    # ------------------------------------------------------------------
    # 共享文件夹 tab
    # ------------------------------------------------------------------

    def _build_folders_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 共享文件夹 ")

        ttk.Label(
            tab,
            text="Agent 只能读取这些文件夹里的文件（只读）。改动后自动重新连接生效。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(0, 8))

        list_frame = ttk.Frame(tab)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.folder_list = tk.Listbox(list_frame, height=8, activestyle="dotbox")
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.folder_list.yview)
        self.folder_list.config(yscrollcommand=scroll.set)
        self.folder_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)

        btn_row = ttk.Frame(tab)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_row, text="添加文件夹…", command=self._add_folder).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="移除选中", command=self._remove_folder).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self._refresh_folders()

    def _refresh_folders(self) -> None:
        self.cfg = ConnectorClientConfig.load()
        self.folder_list.delete(0, tk.END)
        for entry in self.cfg.roots:
            self.folder_list.insert(tk.END, entry)
        if not self.cfg.roots:
            self.folder_list.insert(tk.END, "（尚未共享任何文件夹）")

    def _add_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(Path.home()))
        if not chosen:
            return
        path = Path(chosen).resolve()
        if is_filesystem_root(path):
            messagebox.showerror("文件夹无效", "不能共享整个磁盘根目录，请选择具体文件夹。")
            return
        entry = str(path)
        if entry in self.cfg.roots:
            return
        self.cfg.roots.append(entry)
        self.cfg.save()
        self._refresh_folders()
        self._apply_and_reconnect()

    def _remove_folder(self) -> None:
        selection = self.folder_list.curselection()
        if not selection or not self.cfg.roots:
            return
        entry = self.folder_list.get(selection[0])
        if entry not in self.cfg.roots:
            return
        self.cfg.roots.remove(entry)
        self.cfg.save()
        self._refresh_folders()
        self._apply_and_reconnect()

    # ------------------------------------------------------------------
    # 本机工具 tab
    # ------------------------------------------------------------------

    def _build_tools_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 本机工具 ")

        ttk.Label(
            tab,
            text="登记允许 Agent 调用的本机程序。Agent 只能按名称调用，无法拼任意命令。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(0, 8))

        columns = ("name", "approval", "completion", "exec")
        self.tool_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        self.tool_tree.heading("name", text="名称")
        self.tool_tree.heading("approval", text="审批方式")
        self.tool_tree.heading("completion", text="调用完成方式")
        self.tool_tree.heading("exec", text="程序")
        self.tool_tree.column("name", width=120)
        self.tool_tree.column("approval", width=80, anchor=tk.CENTER)
        self.tool_tree.column("completion", width=100, anchor=tk.CENTER)
        self.tool_tree.column("exec", width=240)
        self.tool_tree.pack(fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(tab)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_row, text="添加工具…", command=self._add_tool).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="从文件导入…", command=self._import_tool).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="导入内置档案…", command=self._import_tool_template).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="删除选中", command=self._remove_tool).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="切换启动模式", command=self._toggle_tool_completion).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="密钥管理…", command=self._manage_secrets).pack(side=tk.RIGHT)
        self._refresh_tools()

    def _refresh_tools(self) -> None:
        self.tool_tree.delete(*self.tool_tree.get_children())
        for tool in ToolRegistry.load().list():
            completion = "启动后返回" if tool.completion == "launch" else "等待结束"
            self.tool_tree.insert(
                "", tk.END, values=(tool.name, tool.approval, completion, tool.exec)
            )

    def _add_tool(self) -> None:
        dialog = _ToolDialog(self.root)
        tool = dialog.result
        if tool is None:
            return
        registry = ToolRegistry.load()
        registry.add(tool)
        registry.save()
        self._refresh_tools()
        self._apply_and_reconnect()

    def _import_tool(self) -> None:
        import json

        file = filedialog.askopenfilename(
            title="选择工具定义 JSON 文件",
            filetypes=[("JSON", "*.json")],
        )
        if not file:
            return
        try:
            payload = json.loads(Path(file).read_text(encoding="utf-8"))
            tool = ToolDef.model_validate(payload)
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", f"工具定义无效：{exc}")
            return
        registry = ToolRegistry.load()
        registry.add(tool)
        registry.save()
        self._refresh_tools()
        self._apply_and_reconnect()

    def _import_tool_template(self) -> None:
        templates = list_templates()
        if not templates:
            messagebox.showinfo("没有内置档案", "当前连接器未包含可导入的工具档案。")
            return
        name = simpledialog.askstring(
            "导入内置档案",
            "请输入要导入的档案名称：\n" + "\n".join(f"- {item}" for item in templates),
            parent=self.root,
        )
        if not name:
            return
        try:
            imported = import_template(name.strip())
        except ConnectorBootstrapError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        self._refresh_tools()
        self._apply_and_reconnect()
        messagebox.showinfo(
            "导入完成",
            "已导入：" + ("、".join(imported) if imported else "无（同名工具被跳过）"),
            parent=self.root,
        )

    def _offer_first_run_template(self) -> None:
        if not messagebox.askyesno(
            "连接器已初始化",
            "本机尚未配对，也没有共享目录或已启用的高风险能力。\n\n"
            "现在是否导入一个经过预检的内置工具档案？",
            parent=self.root,
        ):
            return
        self._import_tool_template()

    def _remove_tool(self) -> None:
        selection = self.tool_tree.selection()
        if not selection:
            return
        name = str(self.tool_tree.item(selection[0], "values")[0])
        if not messagebox.askyesno("删除工具", f"确定删除工具「{name}」？"):
            return
        registry = ToolRegistry.load()
        if registry.remove(name):
            registry.save()
        self._refresh_tools()
        self._apply_and_reconnect()

    def _toggle_tool_completion(self) -> None:
        selection = self.tool_tree.selection()
        if not selection:
            messagebox.showinfo("未选择工具", "请先选择一个工具。")
            return
        name = str(self.tool_tree.item(selection[0], "values")[0])
        registry = ToolRegistry.load()
        try:
            tool = registry.get(name)
        except Exception as exc:  # noqa: BLE001 - registry may change concurrently
            messagebox.showerror("更新失败", str(exc))
            return
        completion = "wait" if tool.completion == "launch" else "launch"
        registry.add(tool.model_copy(update={"completion": completion}))
        registry.save()
        self._refresh_tools()
        self._apply_and_reconnect()

    def _manage_secrets(self) -> None:
        _SecretsDialog(self.root)

    # ------------------------------------------------------------------
    # MCP 服务 tab
    # ------------------------------------------------------------------

    def _build_mcp_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" MCP 服务 ")

        ttk.Label(
            tab,
            text="桥接本机正在运行的 MCP server，让 Agent 经本机调用它们的工具。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(0, 8))

        columns = ("name", "transport", "target", "approval")
        self.mcp_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        self.mcp_tree.heading("name", text="名称")
        self.mcp_tree.heading("transport", text="方式")
        self.mcp_tree.heading("target", text="命令 / 地址")
        self.mcp_tree.heading("approval", text="审批方式")
        self.mcp_tree.column("name", width=120)
        self.mcp_tree.column("transport", width=110, anchor=tk.CENTER)
        self.mcp_tree.column("target", width=230)
        self.mcp_tree.column("approval", width=80, anchor=tk.CENTER)
        self.mcp_tree.pack(fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(tab)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_row, text="添加服务…", command=self._add_mcp).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="从文件导入…", command=self._import_mcp).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="删除选中", command=self._remove_mcp).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self._refresh_mcp()

    def _refresh_mcp(self) -> None:
        self.mcp_tree.delete(*self.mcp_tree.get_children())
        for server in McpRegistry.load().list():
            self.mcp_tree.insert(
                "",
                tk.END,
                values=(server.name, server.transport(), server.command or server.url,
                        server.approval),
            )

    def _add_mcp(self) -> None:
        dialog = _McpDialog(self.root)
        server = dialog.result
        if server is None:
            return
        registry = McpRegistry.load()
        registry.add(server)
        registry.save()
        self._refresh_mcp()
        self._apply_and_reconnect()

    def _import_mcp(self) -> None:
        import json

        file = filedialog.askopenfilename(
            title="选择 MCP server 定义 JSON 文件",
            filetypes=[("JSON", "*.json")],
        )
        if not file:
            return
        try:
            payload = json.loads(Path(file).read_text(encoding="utf-8"))
            server = McpServerDef.model_validate(payload)
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", f"MCP server 定义无效：{exc}")
            return
        registry = McpRegistry.load()
        registry.add(server)
        registry.save()
        self._refresh_mcp()
        self._apply_and_reconnect()

    def _remove_mcp(self) -> None:
        selection = self.mcp_tree.selection()
        if not selection:
            return
        name = str(self.mcp_tree.item(selection[0], "values")[0])
        if not messagebox.askyesno("删除 MCP 服务", f"确定删除「{name}」？"):
            return
        registry = McpRegistry.load()
        if registry.remove(name):
            registry.save()
        self._refresh_mcp()
        self._apply_and_reconnect()

    # ------------------------------------------------------------------
    # 桌面控制 tab
    # ------------------------------------------------------------------

    def _build_desktop_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 桌面控制 ")

        ttk.Label(
            tab,
            text="允许 Agent 截屏并操作本机键鼠（最高风险能力）。开启后每次会话仍需在"
            "「授权窗口」页签授权，且服务端审批通过才会执行。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(0, 12))

        self.desktop_state_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.desktop_state_var, font=("Segoe UI", 11, "bold")).pack(
            anchor=tk.W
        )

        btn_row = ttk.Frame(tab)
        btn_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btn_row, text="启用桌面控制", command=lambda: self._set_desktop(True)).pack(
            side=tk.LEFT
        )
        ttk.Button(btn_row, text="停用桌面控制", command=lambda: self._set_desktop(False)).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        ttk.Label(
            tab,
            text="注意：首次使用需在系统设置中授予本程序「屏幕录制 / 辅助功能」权限。",
            wraplength=560,
            foreground="#b45309",
        ).pack(anchor=tk.W, pady=(16, 0))
        self._refresh_desktop()

    def _refresh_desktop(self) -> None:
        self.cfg = ConnectorClientConfig.load()
        state = "已启用" if self.cfg.desktop_enabled else "已停用（默认）"
        self.desktop_state_var.set(f"当前状态：{state}")

    def _set_desktop(self, enabled: bool) -> None:
        self.cfg.desktop_enabled = enabled
        self.cfg.save()
        self._refresh_desktop()
        self._apply_and_reconnect()

    # ------------------------------------------------------------------
    # 授权窗口 tab
    # ------------------------------------------------------------------

    def _build_arm_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 授权窗口 ")

        ttk.Label(
            tab,
            text="本机同意：在限定时间内允许服务端执行对应操作。到期自动失效，可随时撤销。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(0, 12))

        self.arm_status_vars: dict[str, tk.StringVar] = {}
        self.arm_duration_vars: dict[str, tk.StringVar] = {}
        for row, category in enumerate(CATEGORIES):
            line = ttk.Frame(tab)
            line.pack(fill=tk.X, pady=4)
            ttk.Label(line, text=_ARM_LABELS.get(category, category), width=10).pack(side=tk.LEFT)

            status_var = tk.StringVar(value="未授权")
            self.arm_status_vars[category] = status_var
            ttk.Label(line, textvariable=status_var, width=18).pack(side=tk.LEFT, padx=(8, 0))

            duration_var = tk.StringVar(value=_ARM_DURATIONS[1][0])
            self.arm_duration_vars[category] = duration_var
            ttk.Combobox(
                line,
                textvariable=duration_var,
                values=[label for label, _ in _ARM_DURATIONS],
                width=8,
                state="readonly",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                line, text="授权", command=lambda c=category: self._arm(c)
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                line, text="撤销", command=lambda c=category: self._disarm(c)
            ).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(tab).pack(fill=tk.X, pady=12)
        ttk.Button(tab, text="全部撤销", command=lambda: self._disarm("all")).pack(anchor=tk.W)

    # ------------------------------------------------------------------
    # 运行日志 tab
    # ------------------------------------------------------------------

    def _build_log_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=16)
        notebook.add(tab, text=" 运行日志 ")

        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 8))
        self.conn_state_var = tk.StringVar(value="未连接")
        ttk.Label(top, textvariable=self.conn_state_var, font=("Segoe UI", 11, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(top, text="清空日志", command=self._clear_log_view).pack(side=tk.RIGHT)

        text_frame = ttk.Frame(tab)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            text_frame, height=14, state=tk.DISABLED, wrap=tk.NONE,
            font=("Consolas", 9), bg="#111827", fg="#d1d5db",
            insertbackground="#d1d5db",
        )
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.log_text.tag_config("warn", foreground="#fbbf24")
        self.log_text.tag_config("error", foreground="#f87171")
        self._log_rendered = 0

        ttk.Label(
            tab,
            text="绿色=信息，黄色=需要注意（断线重试/拒绝/超时），红色=错误。"
            "排查问题时先看这里：连没连上、为什么断、操作有没有到达本机。",
            wraplength=560,
            foreground="#6b7280",
        ).pack(anchor=tk.W, pady=(8, 0))

    def _clear_log_view(self) -> None:
        clear_log()
        self._log_rendered = 0
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _refresh_log_view(self) -> None:
        events = log_snapshot()
        if len(events) < self._log_rendered:  # buffer wrapped or cleared
            self._log_rendered = 0
            self.log_text.config(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
        new = events[self._log_rendered:]
        if new:
            at_end = self.log_text.yview()[1] >= 0.99
            self.log_text.config(state=tk.NORMAL)
            for event in new:
                stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
                tag = event.level if event.level in ("warn", "error") else ()
                self.log_text.insert(tk.END, f"{stamp}  {event.message}\n", tag)
            self.log_text.config(state=tk.DISABLED)
            if at_end:
                self.log_text.see(tk.END)
            self._log_rendered = len(events)


    def _arm(self, category: str) -> None:
        label = self.arm_duration_vars[category].get()
        seconds = dict(_ARM_DURATIONS).get(label, 30 * 60)
        ArmStore().arm(category, seconds)
        self._refresh_arm()

    def _disarm(self, category: str) -> None:
        ArmStore().disarm(category)
        self._refresh_arm()

    def _refresh_arm(self) -> None:
        armed = ArmStore().status()
        for category, var in self.arm_status_vars.items():
            remaining = armed.get(category, 0)
            var.set(f"剩余 {_fmt_remaining(remaining)}" if remaining else "未授权")

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    def _start_client(self) -> None:
        if self._client_thread and self._client_thread.is_alive():
            return
        self.cfg = ConnectorClientConfig.load()
        client = build_daemon_client(self.cfg)
        log_event("正在启动连接器…")

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(client.run_forever())
            self._client_loop = loop
            self._client_task = task
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - surface to the log tab
                log_event(f"连接器异常退出：{exc}", "error")
            finally:
                loop.close()
                self._running = False
                self._client_loop = None
                self._client_task = None

        self._client_thread = threading.Thread(
            target=_run, name="connector-client", daemon=True
        )
        self._running = True
        self._client_thread.start()

    def _stop_client(self) -> None:
        loop, task = self._client_loop, self._client_task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=5)
        self._client_thread = None
        self._running = False
        log_event("已断开连接。")

    def _apply_and_reconnect(self) -> None:
        """Persisted config changed; reconnect so the daemon picks it up."""
        if self._running:
            self._stop_client()
            self._start_client()
            log_event("设置已更改，已用新配置重新连接。")
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self._running:
            self.status_var.set("已连接。请保持此窗口打开；关闭窗口将断开连接。")
            self.reconnect_btn.config(state=tk.NORMAL)
        else:
            self.status_var.set("未连接。在「连接」页签填写配对码并点击「连接」。")
            self.reconnect_btn.config(state=tk.DISABLED)

    def _tick(self) -> None:
        """Per-second UI refresh (arm countdowns + log tail + connection badge)."""
        self._refresh_arm()
        self._refresh_log_view()
        if self._running:
            self.conn_state_var.set("● 已连接")
        else:
            self.conn_state_var.set("○ 未连接")
        self.root.after(1000, self._tick)

    def _on_close(self) -> None:
        self._stop_client()
        self.root.destroy()


# ----------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------


class _Dialog:
    """Modal Toplevel helper; subclasses set ``self.result`` before destroy."""

    def __init__(self, parent: tk.Tk, title: str) -> None:
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.transient(parent)
        self.top.resizable(False, False)
        self.top.grab_set()
        self.body = ttk.Frame(self.top, padding=16)
        self.body.pack(fill=tk.BOTH, expand=True)

    def close(self) -> None:
        self.top.grab_release()
        self.top.destroy()

    def wait(self, parent: tk.Tk) -> None:
        self.top.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.top.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.top.winfo_height()) // 2
        self.top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        parent.wait_window(self.top)


class _ToolDialog(_Dialog):
    """Add a simple no-argument tool; advanced tools use JSON import."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent, "添加工具")
        self.result: ToolDef | None = None

        form = ttk.Frame(self.body)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="名称").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.name_var, width=34).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 0)
        )

        ttk.Label(form, text="可执行程序").grid(row=1, column=0, sticky=tk.W, pady=4)
        exec_row = ttk.Frame(form)
        exec_row.grid(row=1, column=1, sticky=tk.EW, padx=(8, 0))
        exec_row.columnconfigure(0, weight=1)
        self.exec_var = tk.StringVar()
        ttk.Entry(exec_row, textvariable=self.exec_var).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(exec_row, text="浏览…", command=self._browse).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(form, text="描述").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.desc_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.desc_var).grid(row=2, column=1, sticky=tk.EW, padx=(8, 0))

        ttk.Label(form, text="审批方式").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.approval_var = tk.StringVar(value="local")
        ttk.Combobox(
            form, textvariable=self.approval_var, values=_APPROVALS, state="readonly", width=10
        ).grid(row=3, column=1, sticky=tk.W, padx=(8, 0))

        self.launch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="启动后立即返回（浏览器、QQ 等常驻程序）",
            variable=self.launch_var,
        ).grid(row=4, column=1, sticky=tk.W, padx=(8, 0), pady=4)

        ttk.Label(
            self.body,
            text="审批方式：local=本机授权窗口内执行；webui=网页端逐次审批；auto=直接执行（仅低危工具）。\n"
            "常驻程序请勾选“启动后立即返回”，此模式不等待程序退出，也不回传程序输出。\n"
            "带参数/凭据的高级工具请用「从文件导入」。",
            wraplength=380,
            foreground="#6b7280",
        ).pack(anchor=tk.W, pady=(10, 0))

        btns = ttk.Frame(self.body)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="确定", command=self._ok).pack(side=tk.RIGHT)
        ttk.Button(btns, text="取消", command=self.close).pack(side=tk.RIGHT, padx=(0, 8))

        self.wait(parent)

    def _browse(self) -> None:
        file = filedialog.askopenfilename(title="选择可执行程序")
        if file:
            self.exec_var.set(file)

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        executable = self.exec_var.get().strip()
        if not name or not executable:
            messagebox.showerror("信息不完整", "请填写名称和可执行程序。", parent=self.top)
            return
        try:
            self.result = ToolDef(
                name=name,
                exec=executable,
                description=self.desc_var.get().strip(),
                approval=self.approval_var.get(),  # type: ignore[arg-type]
                completion="launch" if self.launch_var.get() else "wait",
            )
        except ValueError as exc:
            messagebox.showerror("无效的工具", str(exc), parent=self.top)
            return
        self.close()


class _SecretsDialog(_Dialog):
    """List/add/delete on-device credential ids (values are never shown)."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent, "密钥管理（仅存本机）")
        self.store = SecretStore()

        ttk.Label(
            self.body,
            text="密钥保存在这台电脑的操作系统凭据库中，执行时注入工具进程，绝不会发给服务器。",
            wraplength=400,
        ).pack(anchor=tk.W, pady=(0, 8))

        self.id_list = tk.Listbox(self.body, height=6, width=44, activestyle="dotbox")
        self.id_list.pack(fill=tk.BOTH, expand=True)

        btns = ttk.Frame(self.body)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="添加…", command=self._add).pack(side=tk.LEFT)
        ttk.Button(btns, text="删除选中", command=self._remove).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="关闭", command=self.close).pack(side=tk.RIGHT)

        self._refresh()
        self.wait(parent)

    def _refresh(self) -> None:
        self.id_list.delete(0, tk.END)
        try:
            ids = self.store.ids()
        except CredentialStoreError as exc:
            self.id_list.insert(tk.END, "（系统凭据库不可用）")
            messagebox.showerror("密钥库不可用", str(exc), parent=self.top)
            return
        for sid in ids:
            self.id_list.insert(tk.END, sid)
        if not ids:
            self.id_list.insert(tk.END, "（暂无密钥）")

    def _add(self) -> None:
        secret_id = simpledialog.askstring("添加密钥", "密钥名称（工具定义里引用的 id）：",
                                           parent=self.top)
        if not secret_id or not secret_id.strip():
            return
        secret_id = secret_id.strip()
        value = simpledialog.askstring("添加密钥", f"「{secret_id}」的值（输入不显示）：",
                                       show="*", parent=self.top)
        if value is None:
            return
        try:
            self.store.set(secret_id, value)
        except CredentialStoreError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.top)
            return
        self._refresh()

    def _remove(self) -> None:
        selection = self.id_list.curselection()
        if not selection:
            return
        secret_id = self.id_list.get(selection[0])
        if not messagebox.askyesno("删除密钥", f"确定删除「{secret_id}」？", parent=self.top):
            return
        try:
            self.store.delete(secret_id)
        except CredentialStoreError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self.top)
            return
        self._refresh()


class _McpDialog(_Dialog):
    """Add a local MCP server (stdio command or remote URL)."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent, "添加 MCP 服务")
        self.result: McpServerDef | None = None

        form = ttk.Frame(self.body)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="名称").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.name_var, width=36).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 0)
        )

        ttk.Label(form, text="连接方式").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.mode_var = tk.StringVar(value="stdio 命令")
        mode_box = ttk.Combobox(
            form,
            textvariable=self.mode_var,
            values=("stdio 命令", "URL 地址"),
            state="readonly",
            width=12,
        )
        mode_box.grid(row=1, column=1, sticky=tk.W, padx=(8, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda *_: self._toggle_mode())

        self.command_label = ttk.Label(form, text="启动命令")
        self.command_var = tk.StringVar()
        self.command_entry = ttk.Entry(form, textvariable=self.command_var)
        self.url_label = ttk.Label(form, text="URL")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(form, textvariable=self.url_var)

        ttk.Label(form, text="审批方式").grid(row=4, column=0, sticky=tk.W, pady=4)
        self.approval_var = tk.StringVar(value="local")
        ttk.Combobox(
            form, textvariable=self.approval_var, values=_APPROVALS, state="readonly", width=10
        ).grid(row=4, column=1, sticky=tk.W, padx=(8, 0))

        self._toggle_mode()

        ttk.Label(
            self.body,
            text="stdio 命令示例：npx -y @modelcontextprotocol/server-filesystem\n"
            "URL 示例：http://127.0.0.1:8000/sse",
            wraplength=400,
            foreground="#6b7280",
        ).pack(anchor=tk.W, pady=(10, 0))

        btns = ttk.Frame(self.body)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="确定", command=self._ok).pack(side=tk.RIGHT)
        ttk.Button(btns, text="取消", command=self.close).pack(side=tk.RIGHT, padx=(0, 8))

        self.wait(parent)

    def _toggle_mode(self) -> None:
        stdio = self.mode_var.get().startswith("stdio")
        if stdio:
            self.url_label.grid_remove()
            self.url_entry.grid_remove()
            self.command_label.grid(row=2, column=0, sticky=tk.W, pady=4)
            self.command_entry.grid(row=2, column=1, sticky=tk.EW, padx=(8, 0))
        else:
            self.command_label.grid_remove()
            self.command_entry.grid_remove()
            self.url_label.grid(row=2, column=0, sticky=tk.W, pady=4)
            self.url_entry.grid(row=2, column=1, sticky=tk.EW, padx=(8, 0))

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        stdio = self.mode_var.get().startswith("stdio")
        command = self.command_var.get().strip()
        url = self.url_var.get().strip()
        if not name or (stdio and not command) or (not stdio and not url):
            messagebox.showerror("信息不完整", "请填写名称和命令/URL。", parent=self.top)
            return
        try:
            if stdio:
                parts = command.split()
                self.result = McpServerDef(
                    name=name,
                    command=parts[0],
                    args=parts[1:],
                    approval=self.approval_var.get(),  # type: ignore[arg-type]
                )
            else:
                self.result = McpServerDef(
                    name=name,
                    url=url,
                    approval=self.approval_var.get(),  # type: ignore[arg-type]
                )
        except ValueError as exc:
            messagebox.showerror("无效的服务", str(exc), parent=self.top)
            return
        self.close()


def run_gui() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ConnectorGui(root)
    root.mainloop()
