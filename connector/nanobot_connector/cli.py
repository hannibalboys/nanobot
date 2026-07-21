"""Command-line interface for the nanobot connector."""

from __future__ import annotations

import asyncio

import typer

from nanobot_connector.client import build_daemon_client
from nanobot_connector.config import ConnectorClientConfig
from nanobot_connector.files import is_filesystem_root
from nanobot_connector.pairing import PairingError, pair_device

app = typer.Typer(help="nanobot connector: share local files with a nanobot server.", no_args_is_help=True)


@app.command()
def pair(
    server: str = typer.Option(..., "--server", help="Server URL, e.g. wss://192.168.90.100:8765"),
    code: str = typer.Option(..., "--code", help="Pairing code from the WebUI."),
    name: str = typer.Option("", "--name", help="Display name for this device."),
    fingerprint: str = typer.Option("", "--fingerprint", help="Pinned server cert sha256 (self-signed)."),
    insecure: bool = typer.Option(False, "--insecure", help="Skip TLS verification (discouraged)."),
) -> None:
    """Redeem a pairing code and store the device token."""
    cfg = ConnectorClientConfig.load()
    if insecure:
        typer.secho("WARNING: --insecure disables TLS verification.", fg=typer.colors.YELLOW)
    try:
        pair_device(
            cfg,
            server=server,
            code=code,
            name=name,
            fingerprint=fingerprint,
            insecure=insecure,
        )
    except PairingError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(
        f"Paired as {cfg.node_id}. Add folders with `nanobot-connector allow <dir>`.",
        fg=typer.colors.GREEN,
    )


@app.command("gui")
def gui_cmd() -> None:
    """Open the desktop pairing window."""
    from nanobot_connector.gui import run_gui

    run_gui()


@app.command()
def allow(directory: str = typer.Argument(..., help="Folder to share (read-only).")) -> None:
    """Add a shared folder to the allow-list."""
    from pathlib import Path

    path = Path(directory).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        typer.secho(f"Not a directory: {path}", fg=typer.colors.RED)
        raise typer.Exit(1)
    if is_filesystem_root(path):
        typer.secho("Refusing to share a filesystem root. Choose a specific folder.", fg=typer.colors.RED)
        raise typer.Exit(1)
    cfg = ConnectorClientConfig.load()
    entry = str(path)
    if entry not in cfg.roots:
        cfg.roots.append(entry)
        cfg.save()
    typer.secho(f"Shared: {entry}", fg=typer.colors.GREEN)


@app.command()
def remove(directory: str = typer.Argument(..., help="Folder to stop sharing.")) -> None:
    """Remove a shared folder from the allow-list."""
    from pathlib import Path

    path = str(Path(directory).expanduser().resolve())
    cfg = ConnectorClientConfig.load()
    if path in cfg.roots:
        cfg.roots.remove(path)
        cfg.save()
        typer.secho(f"Removed: {path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Not shared: {path}", fg=typer.colors.YELLOW)


@app.command()
def status() -> None:
    """Show connection settings and shared folders."""
    cfg = ConnectorClientConfig.load()
    typer.echo(f"server:  {cfg.server or '(not paired)'}")
    typer.echo(f"node_id: {cfg.node_id or '(not paired)'}")
    typer.echo(f"paired:  {'yes' if cfg.device_token else 'no'}")
    typer.echo(f"tls:     {'insecure' if cfg.insecure else ('pinned' if cfg.cert_fingerprint else 'system')}")
    typer.echo("shared folders:")
    for r in cfg.roots or []:
        typer.echo(f"  - {r}")
    if not cfg.roots:
        typer.echo("  (none — add with `allow <dir>`)")


@app.command()
def start() -> None:
    """Run the connector in the foreground."""
    cfg = ConnectorClientConfig.load()
    if not cfg.device_token or not cfg.server:
        typer.secho("Not paired. Run `nanobot-connector pair` first.", fg=typer.colors.RED)
        raise typer.Exit(1)
    client = build_daemon_client(cfg)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        typer.echo("stopped")


tool_app = typer.Typer(help="Register local tools the server may invoke (controlled execution).")
app.add_typer(tool_app, name="tool")

secret_app = typer.Typer(help="Manage on-device credentials referenced by tools.")
tool_app.add_typer(secret_app, name="secret")


@tool_app.command("add")
def tool_add(
    file: str = typer.Option("", "--file", "-f", help="Path to a tool definition JSON file."),
    name: str = typer.Option("", "--name", help="Tool name (simple no-argument tool)."),
    executable: str = typer.Option("", "--exec", help="Executable path (simple no-argument tool)."),
    approval: str = typer.Option("local", "--approval", help="auto | webui | local."),
) -> None:
    """Register a tool from a JSON file, or a simple no-argument tool via flags."""
    import json
    from pathlib import Path

    from nanobot_connector.tools import ToolDef, ToolRegistry

    if file:
        try:
            payload = json.loads(Path(file).expanduser().read_text(encoding="utf-8"))
            tool = ToolDef.model_validate(payload)
        except (OSError, ValueError) as exc:
            typer.secho(f"Invalid tool definition: {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc
    elif name and executable:
        tool = ToolDef(name=name, exec=executable, approval=approval)  # type: ignore[arg-type]
    else:
        typer.secho("Provide --file, or both --name and --exec.", fg=typer.colors.RED)
        raise typer.Exit(1)

    registry = ToolRegistry.load()
    registry.add(tool)
    registry.save()
    typer.secho(f"Registered tool '{tool.name}' (approval={tool.approval}).", fg=typer.colors.GREEN)


@tool_app.command("list")
def tool_list() -> None:
    """List registered tools."""
    from nanobot_connector.tools import ToolRegistry

    tools = ToolRegistry.load().list()
    if not tools:
        typer.echo("(no tools registered — add one with `tool add`)")
        return
    for t in tools:
        params = ", ".join(p.name for p in t.params)
        typer.echo(f"  {t.name}  [approval={t.approval}]  exec={t.exec}  params=({params})")


@tool_app.command("show")
def tool_show(name: str = typer.Argument(..., help="Tool name.")) -> None:
    """Print the public schema of a tool (as the server would see it)."""
    import json

    from nanobot_connector.tools import ToolNotFoundError, ToolRegistry

    try:
        tool = ToolRegistry.load().get(name)
    except ToolNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(tool.public(), ensure_ascii=False, indent=2))


@tool_app.command("remove")
def tool_remove(name: str = typer.Argument(..., help="Tool name to remove.")) -> None:
    """Remove a registered tool."""
    from nanobot_connector.tools import ToolRegistry

    registry = ToolRegistry.load()
    if registry.remove(name):
        registry.save()
        typer.secho(f"Removed tool '{name}'.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No such tool: {name}", fg=typer.colors.YELLOW)


@secret_app.command("set")
def secret_set(secret_id: str = typer.Argument(..., help="Credential id referenced by a tool.")) -> None:
    """Store a credential value on this device (prompted, never echoed)."""
    from nanobot_connector.tools import SecretStore

    value = typer.prompt(f"Value for '{secret_id}'", hide_input=True)
    SecretStore().set(secret_id, value)
    typer.secho(f"Stored credential '{secret_id}' on this device only.", fg=typer.colors.GREEN)


@secret_app.command("list")
def secret_list() -> None:
    """List stored credential ids (values are never shown)."""
    from nanobot_connector.tools import SecretStore

    ids = SecretStore().ids()
    if not ids:
        typer.echo("(no credentials stored)")
    for sid in ids:
        typer.echo(f"  - {sid}")


@secret_app.command("remove")
def secret_remove(secret_id: str = typer.Argument(..., help="Credential id to delete.")) -> None:
    """Delete a stored credential."""
    from nanobot_connector.tools import SecretStore

    if SecretStore().delete(secret_id):
        typer.secho(f"Removed credential '{secret_id}'.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No such credential: {secret_id}", fg=typer.colors.YELLOW)


arm_app = typer.Typer(help="Time-boxed on-device consent for controlled capabilities.")
app.add_typer(arm_app, name="arm")


@arm_app.callback(invoke_without_command=True)
def arm_root(
    ctx: typer.Context,
    category: str = typer.Argument(None, help="exec | mcp | desktop"),
    duration: str = typer.Option("30m", "--for", help="Duration, e.g. 30m, 2h, 90s."),
) -> None:
    """Arm a capability for a bounded window (owner consent for `local`/desktop).

    Example: `nanobot-connector arm desktop --for 30m`
    """
    if ctx.invoked_subcommand is not None:
        return
    from nanobot_connector.arm import CATEGORIES, ArmStore, parse_duration

    if category is None:
        typer.echo("Usage: nanobot-connector arm <exec|mcp|desktop> --for 30m")
        typer.echo("       nanobot-connector arm status | disarm [category|all]")
        raise typer.Exit(0)
    if category not in CATEGORIES:
        typer.secho(f"Unknown category '{category}'. One of: {', '.join(CATEGORIES)}", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        ttl = parse_duration(duration)
    except ValueError as exc:
        typer.secho(f"Invalid duration: {duration}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    ArmStore().arm(category, ttl)
    mins = ttl // 60
    typer.secho(
        f"Armed '{category}' for {mins} min {ttl % 60}s. The daemon may now run "
        f"{category} actions until it expires.",
        fg=typer.colors.GREEN,
    )


@arm_app.command("status")
def arm_status() -> None:
    """Show currently-armed capabilities and remaining time."""
    from nanobot_connector.arm import ArmStore

    armed = ArmStore().status()
    if not armed:
        typer.echo("(nothing armed)")
        return
    for cat, remaining in armed.items():
        typer.echo(f"  {cat}: {remaining // 60}m {remaining % 60}s remaining")


@arm_app.command("disarm")
def arm_disarm(category: str = typer.Argument("all", help="exec | mcp | desktop | all")) -> None:
    """Immediately revoke a prior arm (all by default)."""
    from nanobot_connector.arm import ArmStore

    ArmStore().disarm(category)
    typer.secho(f"Disarmed '{category}'.", fg=typer.colors.GREEN)


mcp_app = typer.Typer(help="Bridge local MCP servers to the nanobot server (controlled).")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("add")
def mcp_add(
    file: str = typer.Option("", "--file", "-f", help="Path to an MCP server definition JSON file."),
    name: str = typer.Option("", "--name", help="Server name (stdio quick form)."),
    command: str = typer.Option("", "--command", help="Command to launch a stdio MCP server."),
    url: str = typer.Option("", "--url", help="URL for an SSE/streamable-HTTP MCP server."),
    approval: str = typer.Option("local", "--approval", help="auto | webui | local."),
) -> None:
    """Register a local MCP server from a JSON file, or a quick stdio/url form."""
    import json
    from pathlib import Path

    from nanobot_connector.mcp_bridge import McpRegistry, McpServerDef

    if file:
        try:
            payload = json.loads(Path(file).expanduser().read_text(encoding="utf-8"))
            server = McpServerDef.model_validate(payload)
        except (OSError, ValueError) as exc:
            typer.secho(f"Invalid MCP server definition: {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc
    elif name and (command or url):
        server = McpServerDef(name=name, command=command, url=url, approval=approval)  # type: ignore[arg-type]
    else:
        typer.secho("Provide --file, or --name with --command or --url.", fg=typer.colors.RED)
        raise typer.Exit(1)

    registry = McpRegistry.load()
    registry.add(server)
    registry.save()
    typer.secho(f"Registered MCP server '{server.name}' (approval={server.approval}).", fg=typer.colors.GREEN)


@mcp_app.command("list")
def mcp_list() -> None:
    """List registered local MCP servers."""
    from nanobot_connector.mcp_bridge import McpRegistry

    servers = McpRegistry.load().list()
    if not servers:
        typer.echo("(no MCP servers registered — add one with `mcp add`)")
        return
    for s in servers:
        target = s.command or s.url
        typer.echo(f"  {s.name}  [{s.transport()}, approval={s.approval}]  {target}")


@mcp_app.command("remove")
def mcp_remove(name: str = typer.Argument(..., help="MCP server name to remove.")) -> None:
    """Remove a registered local MCP server."""
    from nanobot_connector.mcp_bridge import McpRegistry

    registry = McpRegistry.load()
    if registry.remove(name):
        registry.save()
        typer.secho(f"Removed MCP server '{name}'.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No such MCP server: {name}", fg=typer.colors.YELLOW)


desktop_app = typer.Typer(help="Enable/disable controlled desktop control on this device.")
app.add_typer(desktop_app, name="desktop")


@desktop_app.command("enable")
def desktop_enable() -> None:
    """Opt this device into desktop control (still needs `arm desktop` per session)."""
    cfg = ConnectorClientConfig.load()
    cfg.desktop_enabled = True
    cfg.save()
    typer.secho(
        "Desktop control enabled on this device. Grant OS screen-recording / "
        "accessibility permission, then `arm desktop --for 30m` before each use.",
        fg=typer.colors.GREEN,
    )


@desktop_app.command("disable")
def desktop_disable() -> None:
    """Opt this device out of desktop control."""
    cfg = ConnectorClientConfig.load()
    cfg.desktop_enabled = False
    cfg.save()
    typer.secho("Desktop control disabled on this device.", fg=typer.colors.GREEN)


@desktop_app.command("status")
def desktop_status() -> None:
    """Show whether desktop control is enabled and currently armed."""
    from nanobot_connector.arm import ArmStore

    cfg = ConnectorClientConfig.load()
    typer.echo(f"enabled: {'yes' if cfg.desktop_enabled else 'no'}")
    remaining = ArmStore().status().get("desktop", 0)
    typer.echo(f"armed:   {'yes (' + str(remaining // 60) + 'm left)' if remaining else 'no'}")


service_app = typer.Typer(help="Install/remove OS autostart.")
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install() -> None:
    """Register the connector to start on boot."""
    from nanobot_connector.service import install_service

    install_service()


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Remove the boot autostart registration."""
    from nanobot_connector.service import uninstall_service

    uninstall_service()


if __name__ == "__main__":
    app()
