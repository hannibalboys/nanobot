import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Loader2, Monitor, Server, ShieldCheck, Terminal, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteConnectorDesktopRecording,
  denyConnectorRequest,
  fetchConnectorApprovals,
  fetchConnectorDesktopAudit,
  fetchConnectorDesktopRecordings,
  fetchConnectorDesktopSessions,
  fetchConnectorExecAudit,
  fetchConnectorGrants,
  fetchConnectorMcpTools,
  fetchConnectorRequests,
  fetchConnectorTools,
  grantConnectorTool,
  resolveConnectorApproval,
  revokeConnectorGrant,
  setConnectorAlias,
  takeOverConnectorDesktop,
} from "@/lib/api";
import type {
  ConnectorAccessRequest,
  ConnectorApproval,
  ConnectorDesktopAuditRecord,
  ConnectorDesktopRecording,
  ConnectorDesktopSession,
  ConnectorExecAuditRecord,
  ConnectorGrantsPayload,
  ConnectorMcpServerHealth,
  ConnectorMcpTool,
  ConnectorNode,
  ConnectorToolSchema,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const APPROVALS_POLL_MS = 3000;

/**
 * Pending WebUI-approval requests. Polls while the connector exec feature is on;
 * shows the tool, its (redacted) args, and approve/deny actions. This is what
 * makes `approval=webui` tools usable — the device owner confirms each run here.
 */
export function ConnectorApprovalsBanner() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [approvals, setApprovals] = useState<ConnectorApproval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await fetchConnectorApprovals(token);
      setApprovals(payload.approvals ?? []);
    } catch {
      setApprovals([]); // 404 (exec off) or transient — nothing to show
    }
  }, [token]);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), APPROVALS_POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const resolve = useCallback(
    async (approvalId: string, decision: "approve" | "deny") => {
      setBusy(approvalId);
      setError(null);
      try {
        await resolveConnectorApproval(token, approvalId, decision);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [token, load],
  );

  if (approvals.length === 0) return null;

  return (
    <section className="space-y-2" aria-label="pending-approvals">
      {error ? (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-[12px] text-destructive">
          {error}
        </p>
      ) : null}
      {approvals.map((a) => (
        <div
          key={a.approvalId}
          className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/90">
                <ShieldCheck className="size-4 text-amber-600" />
                {t("settings.devices.approvalTitle", {
                  defaultValue: "Approve execution of {{tool}}?",
                  tool: a.tool,
                })}
              </div>
              <div className="mt-1 text-[12px] text-muted-foreground">
                {t("settings.devices.approvalBy", {
                  defaultValue: "Requested by {{operator}} on device {{node}}",
                  operator: a.operatorId,
                  node: a.nodeId,
                })}
              </div>
              {Object.keys(a.args ?? {}).length > 0 ? (
                <pre className="mt-1 overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground/90">
                  {JSON.stringify(a.args, null, 2)}
                </pre>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                variant="default"
                disabled={busy === a.approvalId}
                onClick={() => void resolve(a.approvalId, "approve")}
              >
                {busy === a.approvalId ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Check className="mr-1.5 size-4" />
                )}
                {t("settings.devices.approve", { defaultValue: "Approve" })}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-destructive hover:text-destructive"
                disabled={busy === a.approvalId}
                onClick={() => void resolve(a.approvalId, "deny")}
              >
                <X className="mr-1.5 size-4" />
                {t("settings.devices.deny", { defaultValue: "Deny" })}
              </Button>
            </div>
          </div>
        </div>
      ))}
    </section>
  );
}

type ManagerTab = "tools" | "authz" | "audit";

/** Device Control Center: per-device tabs for local tools, authorization, and audit. */
export function ConnectorDeviceManager({
  node,
  onClose,
  onAliasSaved,
}: {
  node: ConnectorNode;
  onClose: () => void;
  onAliasSaved: () => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [tab, setTab] = useState<ManagerTab>("tools");
  const [alias, setAlias] = useState(node.alias ?? "");
  const [aliasBusy, setAliasBusy] = useState(false);
  const [aliasError, setAliasError] = useState<string | null>(null);

  const saveAlias = useCallback(async () => {
    setAliasBusy(true);
    setAliasError(null);
    try {
      await setConnectorAlias(token, node.nodeId, alias.trim());
      onAliasSaved();
    } catch (e) {
      setAliasError(e instanceof Error ? e.message : String(e));
    } finally {
      setAliasBusy(false);
    }
  }, [token, node.nodeId, alias, onAliasSaved]);

  // Close on Escape, matching common modal expectations.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const tabs: Array<{ key: ManagerTab; label: string }> = [
    { key: "tools", label: t("settings.devices.tabTools", { defaultValue: "Local tools" }) },
    { key: "authz", label: t("settings.devices.tabAuthz", { defaultValue: "Authorization" }) },
    { key: "audit", label: t("settings.devices.tabAudit", { defaultValue: "Audit" }) },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={node.alias || node.name || node.nodeId}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl border border-border/60 bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border/45 px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold">
              {node.alias || node.name || node.nodeId}
            </h3>
            <p className="text-[12px] text-muted-foreground">
              {node.platform} · {node.nodeId}
            </p>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            <X className="size-4" />
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-b border-border/45 px-5 py-2">
          <Input
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            placeholder={t("settings.devices.aliasPlaceholder", { defaultValue: "Set a readable name…" })}
            className="h-8 max-w-[240px] text-[13px]"
          />
          <Button size="sm" variant="outline" disabled={aliasBusy} onClick={() => void saveAlias()}>
            {aliasBusy ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              t("settings.devices.saveAlias", { defaultValue: "Save name" })
            )}
          </Button>
          {aliasError ? <span className="text-[12px] text-destructive">{aliasError}</span> : null}
        </div>

        <div className="flex gap-1 border-b border-border/45 px-4 pt-2">
          {tabs.map((tabItem) => (
            <button
              key={tabItem.key}
              type="button"
              onClick={() => setTab(tabItem.key)}
              className={cn(
                "rounded-t-md px-3 py-1.5 text-[13px] font-medium",
                tab === tabItem.key
                  ? "border-b-2 border-primary text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {tabItem.label}
            </button>
          ))}
        </div>

        <div className="min-h-[200px] flex-1 overflow-y-auto px-5 py-4">
          {tab === "tools" ? <ToolsTab node={node} /> : null}
          {tab === "authz" ? <AuthzTab node={node} /> : null}
          {tab === "audit" ? <AuditTab node={node} /> : null}
        </div>
      </div>
    </div>
  );
}

function useDeviceOffline(node: ConnectorNode): string | null {
  const { t } = useTranslation();
  if (!node.online) {
    return t("settings.devices.deviceOffline", {
      defaultValue: "Device is offline. Bring it online to load this data.",
    });
  }
  return null;
}

function ToolsTab({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [tools, setTools] = useState<ConnectorToolSchema[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const offline = useDeviceOffline(node);

  useEffect(() => {
    if (!node.online) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchConnectorTools(token, node.nodeId);
        if (!cancelled) {
          setTools(payload.tools ?? []);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, node.nodeId, node.online]);

  if (offline) return <EmptyNote text={offline} />;
  if (error) return <p className="text-[13px] text-destructive">{error}</p>;
  if (tools === null) return <LoadingNote />;

  return (
    <div className="space-y-4">
      {tools.length === 0 ? (
        <EmptyNote
          text={t("settings.devices.noTools", {
            defaultValue:
              "No tools registered on this device. The owner registers them locally with `nanobot-connector tool add`.",
          })}
        />
      ) : (
        <div className="space-y-2">
          {tools.map((tool) => (
            <div key={tool.name} className="rounded-lg border border-border/45 px-3 py-2">
              <div className="flex items-center gap-2">
                <Terminal className="size-4 text-muted-foreground" />
                <span className="font-mono text-[13px] font-medium">{tool.name}</span>
                <ApprovalBadge policy={tool.approval} />
              </div>
              {tool.description ? (
                <p className="mt-1 text-[12px] text-muted-foreground">{tool.description}</p>
              ) : null}
              {tool.params.length > 0 ? (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {tool.params.map((p) => (
                    <span
                      key={p.name}
                      className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                    >
                      {p.name}
                      {p.required ? "*" : ""}: {p.type}
                      {p.sensitive ? " 🔒" : ""}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
      <McpServersSection node={node} />
      <DesktopSessionsSection node={node} />
    </div>
  );
}

function DesktopSessionsSection({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [sessions, setSessions] = useState<ConnectorDesktopSession[]>([]);
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await fetchConnectorDesktopSessions(token);
      setSessions((payload.sessions ?? []).filter((s) => s.nodeId === node.nodeId));
      setAvailable(true);
    } catch {
      setAvailable(false); // 404 = desktop control off; hide section
    }
  }, [token, node.nodeId]);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 3000);
    return () => clearInterval(id);
  }, [load]);

  const takeOver = useCallback(
    async (sessionId: string) => {
      setBusy(sessionId);
      try {
        await takeOverConnectorDesktop(token, sessionId);
        await load();
      } finally {
        setBusy(null);
      }
    },
    [token, load],
  );

  if (!available) return null;

  return (
    <div>
      <h4 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.devices.desktopSessions", { defaultValue: "Desktop control sessions" })}
      </h4>
      {sessions.length === 0 ? (
        <p className="mt-1 text-[13px] text-muted-foreground">
          {t("settings.devices.noDesktopSessions", { defaultValue: "No active desktop session." })}
        </p>
      ) : null}
      {sessions.length > 0 ? (
        <div className="mt-2 space-y-2">
          {sessions.map((s) => (
            <div key={s.sessionId} className="flex items-center justify-between gap-2 rounded-lg border border-amber-500/40 px-3 py-2">
              <span className="min-w-0 text-[13px]">
                <span className="inline-flex items-center gap-1 font-medium">
                  <Monitor className="size-4 text-amber-600" />
                  {s.operatorId}
                </span>
                <span className="ml-1 text-muted-foreground">“{s.goal}”</span>
                {s.recording ? (
                  <span className="ml-1 rounded bg-destructive/10 px-1.5 py-0.5 text-[11px] text-destructive">
                    {t("settings.devices.recording", { defaultValue: "recording" })}
                  </span>
                ) : null}
                <span className="ml-1 text-[11px] text-muted-foreground">{s.ageS}s</span>
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="text-destructive hover:text-destructive"
                disabled={busy === s.sessionId}
                onClick={() => void takeOver(s.sessionId)}
              >
                {busy === s.sessionId ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  t("settings.devices.takeOver", { defaultValue: "Take over / Stop" })
                )}
              </Button>
            </div>
          ))}
        </div>
      ) : null}
      <DesktopReviewSection node={node} />
    </div>
  );
}

function DesktopReviewSection({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [records, setRecords] = useState<ConnectorDesktopAuditRecord[]>([]);
  const [recordings, setRecordings] = useState<ConnectorDesktopRecording[]>([]);

  const load = useCallback(async () => {
    try {
      const [audit, recs] = await Promise.all([
        fetchConnectorDesktopAudit(token),
        fetchConnectorDesktopRecordings(token),
      ]);
      setRecords((audit.records ?? []).filter((r) => r.nodeId === node.nodeId).slice(0, 20));
      setRecordings((recs.recordings ?? []).filter((r) => r.nodeId === node.nodeId));
    } catch {
      setRecords([]);
      setRecordings([]);
    }
  }, [token, node.nodeId]);

  useEffect(() => {
    void load();
  }, [load]);

  const removeRecording = useCallback(
    async (sessionId: string) => {
      await deleteConnectorDesktopRecording(token, sessionId);
      await load();
    },
    [token, load],
  );

  if (records.length === 0 && recordings.length === 0) return null;

  return (
    <div className="mt-3 space-y-3">
      {recordings.length > 0 ? (
        <div>
          <h5 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.devices.recordings", { defaultValue: "Recordings" })}
          </h5>
          <div className="mt-1 divide-y divide-border/45 rounded-lg border border-border/45">
            {recordings.map((r) => (
              <div key={r.sessionId} className="flex items-center justify-between gap-2 px-3 py-1.5 text-[12px]">
                <span className="truncate font-mono">
                  {r.sessionId.slice(0, 8)} · {r.frames} {t("settings.devices.frames", { defaultValue: "frames" })}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 text-destructive hover:text-destructive"
                  onClick={() => void removeRecording(r.sessionId)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {records.length > 0 ? (
        <div>
          <h5 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.devices.desktopAudit", { defaultValue: "Recent actions" })}
          </h5>
          <div className="mt-1 space-y-1">
            {records.map((r, i) => (
              <div key={i} className="rounded-md border border-border/45 px-2 py-1 text-[11px]">
                <span className="font-mono">{r.action}</span>
                {r.sensitive ? (
                  <span className="ml-1 rounded bg-amber-500/10 px-1 text-amber-600">
                    {r.confirmed
                      ? t("settings.devices.confirmed", { defaultValue: "confirmed" })
                      : t("settings.devices.blocked", { defaultValue: "blocked" })}
                  </span>
                ) : null}
                <span className="ml-1 text-muted-foreground">{r.ts} · {r.result}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function McpServersSection({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [tools, setTools] = useState<ConnectorMcpTool[]>([]);
  const [servers, setServers] = useState<ConnectorMcpServerHealth[]>([]);
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    if (!node.online) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchConnectorMcpTools(token, node.nodeId);
        if (!cancelled) {
          setTools(payload.tools ?? []);
          setServers(payload.servers ?? []);
          setAvailable(true);
        }
      } catch {
        if (!cancelled) setAvailable(false); // 404 = MCP proxy off; hide section
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, node.nodeId, node.online]);

  if (!available) return null;

  const toolsByServer = new Map<string, ConnectorMcpTool[]>();
  for (const tool of tools) {
    const list = toolsByServer.get(tool.server) ?? [];
    list.push(tool);
    toolsByServer.set(tool.server, list);
  }

  return (
    <div>
      <h4 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.devices.mcpServers", { defaultValue: "Local MCP servers" })}
      </h4>
      {servers.length === 0 ? (
        <p className="mt-1 text-[13px] text-muted-foreground">
          {t("settings.devices.noMcpServers", {
            defaultValue: "None bridged. The owner registers them with `nanobot-connector mcp add`.",
          })}
        </p>
      ) : (
        <div className="mt-2 space-y-2">
          {servers.map((srv) => (
            <div key={srv.server} className="rounded-lg border border-border/45 px-3 py-2">
              <div className="flex items-center gap-2 text-[13px] font-medium">
                <Server className="size-4 text-muted-foreground" />
                {srv.server}
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                    srv.healthy ? "bg-emerald-500/10 text-emerald-600" : "bg-destructive/10 text-destructive",
                  )}
                >
                  {srv.healthy
                    ? t("settings.devices.mcpHealthy", { defaultValue: "connected" })
                    : t("settings.devices.mcpUnhealthy", { defaultValue: "unavailable" })}
                </span>
              </div>
              {(toolsByServer.get(srv.server) ?? []).length > 0 ? (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {(toolsByServer.get(srv.server) ?? []).map((tool) => (
                    <span
                      key={tool.name}
                      className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                    >
                      {tool.name}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const TTL_OPTIONS: Array<{ value: string; key: string; fallback: string }> = [
  { value: "", key: "settings.devices.noExpiry", fallback: "No expiry" },
  { value: "3600", key: "settings.devices.ttl1Hour", fallback: "1 hour" },
  { value: "28800", key: "settings.devices.ttl8Hours", fallback: "8 hours" },
  { value: "86400", key: "settings.devices.ttl1Day", fallback: "1 day" },
  { value: "604800", key: "settings.devices.ttl7Days", fallback: "7 days" },
];

function AuthzTab({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [data, setData] = useState<ConnectorGrantsPayload | null>(null);
  const [requests, setRequests] = useState<ConnectorAccessRequest[]>([]);
  const [tools, setTools] = useState<ConnectorToolSchema[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [operatorId, setOperatorId] = useState("");
  const [tool, setTool] = useState("");
  const [ttl, setTtl] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [grants, reqs] = await Promise.all([
        fetchConnectorGrants(token, node.nodeId),
        fetchConnectorRequests(token),
      ]);
      setData(grants);
      setRequests((reqs.requests ?? []).filter((r) => r.nodeId === node.nodeId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token, node.nodeId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!node.online) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchConnectorTools(token, node.nodeId);
        if (!cancelled) setTools(payload.tools ?? []);
      } catch {
        // tools list is optional here; the operator can still type a tool name
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, node.nodeId, node.online]);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setActionError(null);
      setBusy(true);
      try {
        await fn();
        await load();
      } catch (e) {
        setActionError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const grant = useCallback(() => {
    if (!operatorId.trim() || !tool.trim()) return;
    void runAction(async () => {
      await grantConnectorTool(token, {
        nodeId: node.nodeId,
        tool: tool.trim(),
        operatorId: operatorId.trim(),
        ttlS: ttl ? Number(ttl) : undefined,
      });
      setOperatorId("");
      setTool("");
      setTtl("");
    });
  }, [runAction, token, node.nodeId, operatorId, tool, ttl]);

  const revoke = useCallback(
    (grantTool: string, grantOperator: string) =>
      void runAction(() =>
        revokeConnectorGrant(token, { nodeId: node.nodeId, tool: grantTool, operatorId: grantOperator }),
      ),
    [runAction, token, node.nodeId],
  );

  const acceptRequest = useCallback(
    (req: ConnectorAccessRequest) =>
      void runAction(async () => {
        // Accepting a request grants each requested tool (owner can add TTL later).
        for (const requestedTool of req.tools) {
          await grantConnectorTool(token, {
            nodeId: node.nodeId,
            tool: requestedTool,
            operatorId: req.operatorId,
          });
        }
      }),
    [runAction, token, node.nodeId],
  );

  const denyRequest = useCallback(
    (req: ConnectorAccessRequest) =>
      void runAction(() => denyConnectorRequest(token, { nodeId: node.nodeId, operatorId: req.operatorId })),
    [runAction, token, node.nodeId],
  );

  if (error) return <p className="text-[13px] text-destructive">{error}</p>;
  if (data === null) return <LoadingNote />;

  return (
    <div className="space-y-4">
      {actionError ? (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-[12px] text-destructive">
          {actionError}
        </p>
      ) : null}

      {requests.length > 0 ? (
        <div>
          <h4 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.devices.pendingRequests", { defaultValue: "Pending access requests" })}
          </h4>
          <div className="mt-2 divide-y divide-border/45 rounded-lg border border-amber-500/40">
            {requests.map((req) => (
              <div key={req.operatorId} className="flex items-center justify-between gap-2 px-3 py-2 text-[13px]">
                <span className="min-w-0">
                  <span className="font-medium">{req.operatorId}</span>
                  <span className="text-muted-foreground"> → </span>
                  <span className="font-mono">{req.tools.join(", ")}</span>
                  {req.reason ? (
                    <span className="ml-1 text-[11px] text-muted-foreground">“{req.reason}”</span>
                  ) : null}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  <Button size="sm" variant="default" disabled={busy} onClick={() => acceptRequest(req)}>
                    {t("settings.devices.accept", { defaultValue: "Accept" })}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:text-destructive"
                    disabled={busy}
                    onClick={() => denyRequest(req)}
                  >
                    {t("settings.devices.deny", { defaultValue: "Deny" })}
                  </Button>
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div>
        <h4 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t("settings.devices.grants", { defaultValue: "Granted access" })}
        </h4>
        {data.grants.length === 0 ? (
          <p className="mt-1 text-[13px] text-muted-foreground">
            {t("settings.devices.noGrants", {
              defaultValue: "No cross-person grants. Only you (the device owner) can run tools.",
            })}
          </p>
        ) : (
          <div className="mt-2 divide-y divide-border/45 rounded-lg border border-border/45">
            {data.grants.map((g) => (
              <div
                key={`${g.operatorId}:${g.tool}`}
                className="flex items-center justify-between gap-2 px-3 py-2 text-[13px]"
              >
                <span>
                  <span className="font-medium">{g.operatorId}</span>
                  <span className="text-muted-foreground"> → </span>
                  <span className="font-mono">{g.tool}</span>
                  {g.expiresAt ? (
                    <span className="ml-2 text-[11px] text-muted-foreground">
                      {t("settings.devices.expires", { defaultValue: "expires" })}{" "}
                      {new Date(g.expiresAt * 1000).toLocaleString()}
                    </span>
                  ) : null}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 text-destructive hover:text-destructive"
                  disabled={busy}
                  onClick={() => revoke(g.tool, g.operatorId)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-border/45 px-3 py-2.5">
        <h4 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t("settings.devices.grantAccess", { defaultValue: "Grant access to another operator" })}
        </h4>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Input
            value={operatorId}
            onChange={(e) => setOperatorId(e.target.value)}
            placeholder={t("settings.devices.operatorId", { defaultValue: "operator id" })}
            className="h-8 max-w-[180px] text-[13px]"
          />
          <select
            aria-label={t("settings.devices.selectTool", { defaultValue: "select tool" })}
            value={tool}
            onChange={(e) => setTool(e.target.value)}
            className="h-8 rounded-md border border-input bg-background px-2 text-[13px]"
          >
            <option value="">{t("settings.devices.selectTool", { defaultValue: "select tool" })}</option>
            {tools.map((tl) => (
              <option key={tl.name} value={tl.name}>
                {tl.name}
              </option>
            ))}
          </select>
          <select
            aria-label={t("settings.devices.grantExpiry", { defaultValue: "Access expires" })}
            value={ttl}
            onChange={(e) => setTtl(e.target.value)}
            className="h-8 rounded-md border border-input bg-background px-2 text-[13px]"
          >
            {TTL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {t(opt.key, { defaultValue: opt.fallback })}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={busy || !operatorId.trim() || !tool.trim()} onClick={grant}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : t("settings.devices.grant", { defaultValue: "Grant" })}
          </Button>
        </div>
      </div>
    </div>
  );
}

function AuditTab({ node }: { node: ConnectorNode }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [records, setRecords] = useState<ConnectorExecAuditRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchConnectorExecAudit(token, node.nodeId);
        if (!cancelled) {
          setRecords(payload.records ?? []);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, node.nodeId]);

  if (error) return <p className="text-[13px] text-destructive">{error}</p>;
  if (records === null) return <LoadingNote />;
  if (records.length === 0) {
    return (
      <EmptyNote
        text={t("settings.devices.noAudit", { defaultValue: "No executions recorded for this device yet." })}
      />
    );
  }

  return (
    <div className="space-y-1.5">
      {records.map((r, i) => (
        <div key={i} className="rounded-md border border-border/45 px-3 py-1.5 text-[12px]">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono font-medium">{r.tool}</span>
            <ResultBadge result={r.result} />
          </div>
          <div className="mt-0.5 text-muted-foreground">
            {r.ts} · {r.operatorId} · {r.approval}
            {typeof r.exitCode === "number" ? ` · exit ${r.exitCode}` : ""}
            {r.durationMs ? ` · ${r.durationMs}ms` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

function ApprovalBadge({ policy }: { policy: string }) {
  const { t } = useTranslation();
  const label =
    policy === "auto"
      ? t("settings.devices.policyAuto", { defaultValue: "auto" })
      : policy === "webui"
        ? t("settings.devices.policyWebui", { defaultValue: "approval" })
        : t("settings.devices.policyLocal", { defaultValue: "on-device" });
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] font-medium",
        policy === "auto"
          ? "bg-muted text-muted-foreground"
          : policy === "webui"
            ? "bg-amber-500/10 text-amber-600"
            : "bg-blue-500/10 text-blue-600",
      )}
    >
      {label}
    </span>
  );
}

function ResultBadge({ result }: { result: string }) {
  const ok = result === "ok";
  const danger = result === "nonzero_exit" || result === "timeout" || result.includes("denied");
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[11px] font-medium",
        ok
          ? "bg-emerald-500/10 text-emerald-600"
          : danger
            ? "bg-destructive/10 text-destructive"
            : "bg-muted text-muted-foreground",
      )}
    >
      {result}
    </span>
  );
}

function LoadingNote() {
  const { t } = useTranslation();
  return (
    <p className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      {t("settings.devices.loading", { defaultValue: "Loading…" })}
    </p>
  );
}

function EmptyNote({ text }: { text: string }) {
  return <p className="text-[13px] text-muted-foreground">{text}</p>;
}
