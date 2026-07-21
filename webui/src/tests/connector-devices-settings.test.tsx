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
      <ConnectorDevicesSettings />
    </ClientProvider>,
  );
}

describe("ConnectorDevicesSettings", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders device list with online status and shared roots", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) {
        return jsonResponse({
          nodes: [
            {
              nodeId: "dev-abc",
              name: "Work laptop",
              platform: "windows",
              online: true,
              roots: ["D:/PPT资料", "E:/docs"],
              lastSeenAt: "2026-07-14T02:00:00Z",
            },
          ],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => {
      expect(screen.getByText("Work laptop")).toBeInTheDocument();
    });
    expect(screen.getByText("online")).toBeInTheDocument();
    expect(screen.getByText(/Shared:/)).toBeInTheDocument();
    expect(screen.getByText(/D:\/PPT资料/)).toBeInTheDocument();
  });

  it("shows disabled message when connector API returns 404", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) {
        return jsonResponse({ error: "not found" }, 404);
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => {
      expect(screen.getByText(/Connector is disabled/i)).toBeInTheDocument();
    });
  });

  it("opens wizard and shows pairing code with server URL", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/connector/nodes")) {
        return jsonResponse({ nodes: [] });
      }
      if (url.endsWith("/api/connector/downloads")) {
        return jsonResponse({
          version: "0.1.0",
          tag: "connector-v0.1.0",
          releasesUrl: "https://github.com/HKUDS/nanobot/releases?q=connector",
          sourceInstall: 'pip install "nanobot-connector @ git+https://github.com/HKUDS/nanobot.git#subdirectory=connector"',
          platforms: [
            {
              id: "windows",
              label: "Windows",
              filename: "nanobot-connector.exe",
              url: "https://example.com/nanobot-connector.exe",
            },
          ],
        });
      }
      if (url.endsWith("/api/connector/pairing-codes")) {
        return jsonResponse({ code: "AB12CD34", expiresAt: Math.floor(Date.now() / 1000) + 600 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Add device/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Add device/i }));

    await waitFor(() => {
      expect(screen.getByText("AB12CD34")).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: /Download for Windows/i })).toHaveAttribute(
      "href",
      "https://example.com/nanobot-connector.exe",
    );
    expect(screen.getByText(/nanobot-connector pair --server/)).toBeInTheDocument();
  });

  it("requires confirmation before revoking a device", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/connector/revoke")) {
        return jsonResponse({ revoked: "dev-abc" });
      }
      if (url.endsWith("/api/connector/nodes")) {
        return jsonResponse({
          nodes: [{ nodeId: "dev-abc", name: "PC", platform: "linux", online: false }],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    renderDevices();

    await waitFor(() => {
      expect(screen.getByText("PC")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Revoke/i }));
    expect(screen.getByText(/Revoke this device/i)).toBeInTheDocument();

    // The row button is replaced by the confirm panel; its destructive
    // confirm button is also labeled "Revoke".
    fireEvent.click(screen.getByRole("button", { name: /Revoke/i }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/connector/revoke?nodeId=dev-abc"),
        expect.any(Object),
      );
    });
  });
});
