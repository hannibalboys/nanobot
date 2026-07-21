"""End-to-end flow: hub + fake connector + agent tools (task 7.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.agent.tools.connector import (
    ConnectorFetchFileTool,
    ConnectorListFilesTool,
    ConnectorListNodesTool,
)
from nanobot.config.schema import ConnectorConfig
from nanobot.connector.hub import ConnectorHub

from .conftest import FakeConnector, duplex


async def _online_hub(files, *, chunk_bytes=64):
    server_conn, client_conn = duplex()
    hub = ConnectorHub()
    connector = FakeConnector(client_conn, files=files, chunk_bytes=chunk_bytes)
    serve_task = asyncio.create_task(
        hub.serve(server_conn, node_id="dev-1", owner_id="webui")
    )
    connector_task = asyncio.create_task(connector.run())
    for _ in range(100):
        if hub.list_nodes():
            break
        await asyncio.sleep(0.01)
    return hub, serve_task, connector_task, server_conn


@pytest.mark.asyncio
async def test_agent_tools_list_fetch_chain(tmp_path):
    """列节点 → 列文件 → 拉取文档，全链路经 agent 工具完成。"""
    content = b"# report\nPPT source material"
    hub, serve_task, ctask, sconn = await _online_hub({"D:/资料/report.md": content})
    cfg = ConnectorConfig(enabled=True)

    nodes_out = await ConnectorListNodesTool(
        connector_config=cfg, workspace=tmp_path, hub=hub
    ).execute()
    nodes = json.loads(str(nodes_out))["nodes"]
    assert nodes[0]["nodeId"] == "dev-1"

    list_out = await ConnectorListFilesTool(
        connector_config=cfg, workspace=tmp_path, hub=hub
    ).execute(node_id="dev-1", path="D:/资料")
    entries = json.loads(str(list_out))["entries"]
    assert "report.md" in str(entries)

    fetch_out = await ConnectorFetchFileTool(
        connector_config=cfg, workspace=tmp_path, hub=hub
    ).execute(node_id="dev-1", path="D:/资料/report.md")
    payload = json.loads(str(fetch_out))
    landed = Path(payload["server_path"])
    assert landed.read_bytes() == content
    assert (tmp_path / "connector").resolve() in landed.resolve().parents

    await sconn.close()
    await asyncio.gather(serve_task, ctask)
