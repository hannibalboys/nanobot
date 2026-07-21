"""Simple desktop UI for pairing and running the connector."""

from __future__ import annotations

import asyncio
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from nanobot_connector.client import build_daemon_client
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.files import is_filesystem_root
from nanobot_connector.pairing import PairingError, normalize_pairing_code, pair_device

_PAIR_CMD_RE = re.compile(
    r"--server\s+(?P<server>\S+)\s+--code\s+(?P<code>\S+)",
    re.IGNORECASE,
)
# WebUI settings shows gateway.port (often 18790) but Connector lives on websocket.port (8765).
_LEGACY_GATEWAY_PORT = ":18790"
_CONNECTOR_PORT = ":8765"


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


class ConnectorGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = ConnectorClientConfig.load()
        self._client_thread: threading.Thread | None = None
        self._running = False

        root.title("nanobot 连接器")
        root.geometry("520x360")
        root.minsize(480, 320)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="连接网关，共享本机文件夹给 nanobot Agent。", wraplength=460).pack(
            anchor=tk.W,
        )

        form = ttk.Frame(frame)
        form.pack(fill=tk.X, pady=(12, 0))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="网关地址").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.server_var = tk.StringVar(value=_normalize_server_url(self.cfg.server or "ws://127.0.0.1:8765"))
        ttk.Entry(form, textvariable=self.server_var).grid(row=0, column=1, sticky=tk.EW, padx=(8, 0))
        self.port_hint_var = tk.StringVar()
        self.port_hint = ttk.Label(form, textvariable=self.port_hint_var, foreground="#b45309", wraplength=360)
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
        ttk.Button(folder_row, text="浏览…", command=self._browse_folder).grid(row=0, column=1, padx=(6, 0))

        paste_row = ttk.Frame(frame)
        paste_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(paste_row, text="或粘贴向导命令：").pack(side=tk.LEFT)
        self.paste_var = tk.StringVar()
        paste_entry = ttk.Entry(paste_row, textvariable=self.paste_var)
        paste_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        ttk.Button(paste_row, text="填入", command=self._apply_paste).pack(side=tk.LEFT, padx=(6, 0))

        self.connect_btn = ttk.Button(frame, text="连接", command=self._connect)
        self.connect_btn.pack(pady=(16, 8))

        self.status_var = tk.StringVar(value=self._idle_status())
        ttk.Label(frame, textvariable=self.status_var, wraplength=460).pack(anchor=tk.W)

        if self.cfg.device_token and self.cfg.server:
            self.status_var.set(f"已配对设备 {self.cfg.node_id or ''}，填写新配对码可重新连接。")

    def _refresh_port_hint(self) -> None:
        if _server_needs_port_hint(self.server_var.get()):
            self.port_hint_var.set("Connector 使用 WebSocket 端口 8765，不是设置页里的 18790。")
        else:
            self.port_hint_var.set("")

    def _idle_status(self) -> str:
        if self._running:
            return "已连接。请保持此窗口打开；关闭窗口将断开连接。"
        return "填写配对码并选择文件夹，然后点击「连接」。"

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if chosen:
            self.folder_var.set(chosen)

    def _apply_paste(self) -> None:
        parsed = _parse_paste(self.paste_var.get())
        if not parsed:
            messagebox.showwarning("无法识别", "请粘贴向导中的完整命令，例如：\n"
                                 "nanobot-connector pair --server ws://host:18790 --code AB12CD34")
            return
        server, code = parsed
        self.server_var.set(_normalize_server_url(server))
        self.code_var.set(code)

    def _connect(self) -> None:
        if self._running:
            messagebox.showinfo("已连接", "连接器已在运行。")
            return

        folder = Path(self.folder_var.get().strip()).expanduser()
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("文件夹无效", "请选择一个存在的文件夹。")
            return
        if is_filesystem_root(folder.resolve()):
            messagebox.showerror("文件夹无效", "不能共享整个磁盘根目录，请选择具体文件夹。")
            return

        self.connect_btn.config(state=tk.DISABLED)
        self.status_var.set("正在配对…")
        self.root.update_idletasks()

        try:
            self.cfg = pair_device(
                self.cfg,
                server=_normalize_server_url(self.server_var.get()),
                code=self.code_var.get(),
            )
            entry = str(folder.resolve())
            if entry not in self.cfg.roots:
                self.cfg.roots = [entry]
                self.cfg.save()
            self._start_client()
            self.status_var.set(self._idle_status())
            messagebox.showinfo("连接成功", f"已配对并连接。\n共享：{entry}\n请保持此窗口打开。")
        except PairingError as exc:
            self.status_var.set(str(exc))
            messagebox.showerror("连接失败", str(exc))
            self.connect_btn.config(state=tk.NORMAL)
        except Exception as exc:  # noqa: BLE001 - surface to desktop user
            self.status_var.set(f"连接失败：{exc}")
            messagebox.showerror("连接失败", str(exc))
            self.connect_btn.config(state=tk.NORMAL)

    def _start_client(self) -> None:
        if self._client_thread and self._client_thread.is_alive():
            self._running = True
            return

        client = build_daemon_client(self.cfg)

        def _run() -> None:
            try:
                asyncio.run(client.run_forever())
            except Exception:
                pass
            finally:
                self._running = False

        self._client_thread = threading.Thread(target=_run, name="connector-client", daemon=True)
        self._running = True
        self._client_thread.start()

    def _on_close(self) -> None:
        self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ConnectorGui(root)
    root.mainloop()
