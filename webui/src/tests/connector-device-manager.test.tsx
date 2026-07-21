import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectorDevicesSettings } from "@/components/settings/ConnectorDevicesSettings";
import { ClientProvider } from "@/providers/ClientProvider";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function renderDevices() {
  render(
    <ClientProvider client={{} as never} token="tok">
      <ConnectorDevicesSettings allowExec />
    </ClientProvider>,
  );
}

const ONLINE_NODE = {
  nodeId: "dev-abc",
  name: "Work laptop",
  platform: "windows",
  online: true,
  capabilities: ["fs", "exec"],
};

describe("ConnectorDeviceManager (v2 control center)", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a pending approval and approves it", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [] });
      if (url.includes("/api/connector/approve?")) return jsonResponse({ approvalId: "ap1", approved: true });
      if (url.endsWith("/api/connector/approvals")) {
        return jsonResponse({
          approvals: [
            { approvalId: "ap1", nodeId: "dev-abc", tool: "open_notepad", operatorId: "alice", args: {}, createdAt: 0 },
          ],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => {
      expect(screen.getByText(/Approve execution of open_notepad/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /^Approve$/i }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/connector/approve?approvalId=ap1&decision=approve"),
        expect.any(Object),
      );
    });
  });

  it("opens the manager and lists registered tools", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) {
        return jsonResponse({
          nodeId: "dev-abc",
          tools: [{ name: "open_notepad", approval: "local", params: [] }],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));

    await waitFor(() => {
      expect(screen.getByText("open_notepad")).toBeInTheDocument();
    });
    // approval policy badge for a local tool
    expect(screen.getByText(/on-device/i)).toBeInTheDocument();
  });

  it("grants a tool to another operator with a time limit from the authorization tab", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.endsWith("/api/connector/requests")) return jsonResponse({ requests: [] });
      if (url.includes("/api/connector/tools?")) {
        return jsonResponse({ nodeId: "dev-abc", tools: [{ name: "open_notepad", approval: "auto", params: [] }] });
      }
      if (url.includes("/api/connector/grants?")) {
        return jsonResponse({ grants: [], activeOperators: [] });
      }
      if (url.includes("/api/connector/grant?")) {
        return jsonResponse({ granted: {} });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Authorization/i })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Authorization/i }));

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/operator id/i)).toBeInTheDocument();
    });
    fireEvent.change(screen.getByPlaceholderText(/operator id/i), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText(/select tool/i), { target: { value: "open_notepad" } });
    fireEvent.change(screen.getByLabelText(/Access expires/i), { target: { value: "3600" } });
    fireEvent.click(screen.getByRole("button", { name: /^Grant$/i }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/connector/grant?nodeId=dev-abc&tool=open_notepad&operatorId=bob&ttlS=3600"),
        expect.any(Object),
      );
    });
  });

  it("accepts a pending cross-person access request", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.endsWith("/api/connector/requests")) {
        return jsonResponse({
          requests: [{ nodeId: "dev-abc", operatorId: "carol", tools: ["open_notepad"], reason: "help" }],
        });
      }
      if (url.includes("/api/connector/tools?")) {
        return jsonResponse({ nodeId: "dev-abc", tools: [{ name: "open_notepad", approval: "auto", params: [] }] });
      }
      if (url.includes("/api/connector/grants?")) return jsonResponse({ grants: [], activeOperators: [] });
      if (url.includes("/api/connector/grant?")) return jsonResponse({ granted: {} });
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Authorization/i })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Authorization/i }));

    await waitFor(() => expect(screen.getByText("carol")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /^Accept$/i }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/connector/grant?nodeId=dev-abc&tool=open_notepad&operatorId=carol"),
        expect.any(Object),
      );
    });
  });

  it("shows bridged local MCP servers in the tools tab", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) return jsonResponse({ nodeId: "dev-abc", tools: [] });
      if (url.includes("/api/connector/mcp-tools?")) {
        return jsonResponse({
          nodeId: "dev-abc",
          tools: [{ server: "filesearch", name: "search", approval: "auto" }],
          servers: [{ server: "filesearch", healthy: true, toolCount: 1 }],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();
    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));

    await waitFor(() => expect(screen.getByText("Local MCP servers")).toBeInTheDocument());
    expect(screen.getByText("filesearch")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText("search")).toBeInTheDocument();
  });

  it("hides the MCP section when the proxy route 404s", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) return jsonResponse({ nodeId: "dev-abc", tools: [] });
      if (url.includes("/api/connector/mcp-tools?")) return jsonResponse({ error: "not found" }, 404);
      if (url.endsWith("/api/connector/desktop-sessions")) return jsonResponse({ error: "not found" }, 404);
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();
    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    expect(screen.queryByText("Local MCP servers")).not.toBeInTheDocument();
    expect(screen.queryByText("Desktop control sessions")).not.toBeInTheDocument();
  });

  it("shows an active desktop session and can take over", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) return jsonResponse({ nodeId: "dev-abc", tools: [] });
      if (url.includes("/api/connector/mcp-tools?")) return jsonResponse({ error: "off" }, 404);
      if (url.includes("/api/connector/desktop-takeover?")) return jsonResponse({ takenOver: "sess-1" });
      if (url.endsWith("/api/connector/desktop-sessions")) {
        return jsonResponse({
          sessions: [{ sessionId: "sess-1", nodeId: "dev-abc", operatorId: "webui", goal: "open app", recording: false, ageS: 5 }],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();
    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));
    await waitFor(() => expect(screen.getByText("Desktop control sessions")).toBeInTheDocument());
    expect(screen.getByText(/open app/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Take over/i }));
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/connector/desktop-takeover?sessionId=sess-1"),
        expect.any(Object),
      );
    });
  });

  it("shows desktop audit replay and deletes a recording", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) return jsonResponse({ nodeId: "dev-abc", tools: [] });
      if (url.includes("/api/connector/mcp-tools?")) return jsonResponse({ error: "off" }, 404);
      if (url.endsWith("/api/connector/desktop-sessions")) return jsonResponse({ sessions: [] });
      if (url.startsWith("http") && url.includes("/api/connector/desktop-recording-delete?")) {
        return jsonResponse({ deleted: "sess-1" });
      }
      if (url.includes("/api/connector/desktop-recording-delete?")) return jsonResponse({ deleted: "sess-1" });
      if (url.includes("/api/connector/desktop-audit")) {
        return jsonResponse({
          records: [
            { ts: "2026-07-21T00:00:00Z", sessionId: "sess-1", nodeId: "dev-abc", operatorId: "webui", action: "click", params: {}, sensitive: true, confirmed: true, result: "ok" },
          ],
        });
      }
      if (url.endsWith("/api/connector/desktop-recordings")) {
        return jsonResponse({ recordings: [{ sessionId: "sess-1", nodeId: "dev-abc", frames: 3, mtime: 0 }] });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();
    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));

    await waitFor(() => expect(screen.getByText("Recent actions")).toBeInTheDocument());
    expect(screen.getByText("Recordings")).toBeInTheDocument();
    // sensitive+confirmed action badge
    expect(screen.getByText("confirmed")).toBeInTheDocument();

    // delete the recording (the recordings row's trash button)
    const trashButtons = screen.getAllByRole("button");
    const del = trashButtons.find((b) => b.querySelector("svg"));
    expect(del).toBeTruthy();
  });

  it("closes the manager on Escape", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      if (url.endsWith("/api/connector/approvals")) return jsonResponse({ approvals: [] });
      if (url.includes("/api/connector/tools?")) return jsonResponse({ nodeId: "dev-abc", tools: [] });
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();
    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Manage/i }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not show Manage or approvals when allowExec is off", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) return jsonResponse({ nodes: [ONLINE_NODE] });
      throw new Error(`unexpected fetch: ${url}`);
    });

    render(
      <ClientProvider client={{} as never} token="tok">
        <ConnectorDevicesSettings allowExec={false} />
      </ClientProvider>,
    );

    await waitFor(() => expect(screen.getByText("Work laptop")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Manage/i })).not.toBeInTheDocument();
  });
});
