import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CircleAlert, Download, Laptop, Loader2, Plus, RefreshCw, Settings2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import {
  createConnectorPairingCode,
  fetchConnectorDownloads,
  fetchConnectorNodes,
  revokeConnectorNode,
} from "@/lib/api";
import type { ConnectorDownloadPlatform, ConnectorDownloadsPayload, ConnectorNode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import { ConnectorApprovalsBanner, ConnectorDeviceManager } from "./ConnectorDeviceManager";

const POLL_MS = 4000;
const WIZARD_POLL_MS = 2000;
const CODE_TTL_S = 600;

function connectorServerUrl(): string {
  if (typeof window === "undefined") {
    return "wss://<host>:8765";
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}`;
}

function detectClientPlatform(): ConnectorDownloadPlatform["id"] | null {
  if (typeof navigator === "undefined") return null;
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("win")) return "windows";
  if (ua.includes("mac")) return "macos";
  if (ua.includes("linux")) return "linux";
  return null;
}

function formatRoots(roots: string[] | undefined): string | null {
  if (!roots?.length) return null;
  if (roots.length <= 2) return roots.join(", ");
  return `${roots.slice(0, 2).join(", ")} +${roots.length - 2}`;
}

export function ConnectorDevicesSettings({ allowExec = false }: { allowExec?: boolean }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [nodes, setNodes] = useState<ConnectorNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [managing, setManaging] = useState<ConnectorNode | null>(null);

  const loadNodes = useCallback(async () => {
    try {
      const payload = await fetchConnectorNodes(token);
      setNodes(payload.nodes ?? []);
      setError(null);
      setDisabled(false);
    } catch (e) {
      const status = e && typeof e === "object" && "status" in e ? (e as { status: number }).status : 0;
      if (status === 404) {
        setDisabled(true);
        setNodes([]);
        setError(null);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadNodes();
    const id = setInterval(() => void loadNodes(), POLL_MS);
    return () => clearInterval(id);
  }, [loadNodes]);

  const handleRevoke = useCallback(
    async (nodeId: string) => {
      await revokeConnectorNode(token, nodeId);
      await loadNodes();
    },
    [token, loadNodes],
  );

  if (disabled) {
    return (
      <div className="rounded-lg border border-dashed border-border/60 px-4 py-10 text-center text-sm text-muted-foreground">
        {t("settings.devices.disabled", {
          defaultValue:
            "Connector is disabled on this gateway. Set connector.enabled to true in config and restart the gateway.",
        })}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SettingsSectionTitle>
        {t("settings.nav.devices", { defaultValue: "Devices" })}
      </SettingsSectionTitle>
      <section className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
          {t("settings.devices.description", {
            defaultValue:
              "Connect your own computer so the agent can read files you share — without uploading them first. Install the connector, pair it, then choose folders to share.",
          })}
        </p>
        <Button size="sm" onClick={() => setWizardOpen(true)}>
          <Plus className="mr-1.5 size-4" />
          {t("settings.devices.add", { defaultValue: "Add device" })}
        </Button>
      </section>

      {allowExec ? <ConnectorApprovalsBanner /> : null}

      {error ? (
        <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
          <CircleAlert className="size-4" />
          {error}
        </div>
      ) : null}

      {loading ? (
        <div className="flex items-center gap-2 px-1 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          {t("settings.devices.loading", { defaultValue: "Loading…" })}
        </div>
      ) : nodes.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border/60 px-4 py-12 text-center text-sm text-muted-foreground">
          {t("settings.devices.empty", {
            defaultValue: "No devices yet. Click “Add device” to connect your computer.",
          })}
        </div>
      ) : (
        <div className="divide-y divide-border/45 rounded-lg border border-border/45">
          {nodes.map((node) => (
            <DeviceRow
              key={node.nodeId}
              node={node}
              onRevoke={handleRevoke}
              onManage={allowExec ? () => setManaging(node) : undefined}
            />
          ))}
        </div>
      )}

      {wizardOpen ? (
        <AddDeviceWizard
          initialOnlineNodeIds={nodes.filter((n) => n.online).map((n) => n.nodeId)}
          onClose={() => {
            setWizardOpen(false);
            void loadNodes();
          }}
        />
      ) : null}

      {managing ? (
        <ConnectorDeviceManager
          node={managing}
          onClose={() => setManaging(null)}
          onAliasSaved={() => void loadNodes()}
        />
      ) : null}
    </div>
  );
}

function ConnectorDownloadPanel({ token }: { token: string }) {
  const { t } = useTranslation();
  const [downloads, setDownloads] = useState<ConnectorDownloadsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const detected = detectClientPlatform();

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchConnectorDownloads(token);
        if (!cancelled) {
          setDownloads(payload);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const copySourceInstall = useCallback(async () => {
    if (!downloads?.sourceInstall) return;
    try {
      await navigator.clipboard.writeText(downloads.sourceInstall);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore clipboard failures
    }
  }, [downloads?.sourceInstall]);

  if (error) {
    return <p className="mt-1 text-destructive">{error}</p>;
  }

  if (!downloads) {
    return (
      <p className="mt-1 flex items-center gap-1.5 text-[12px]">
        <Loader2 className="size-3.5 animate-spin" />
        {t("settings.devices.loadingDownloads", { defaultValue: "Loading download links…" })}
      </p>
    );
  }

  const primary =
    downloads.platforms.find((platform) => platform.id === detected) ?? downloads.platforms[0];
  const others = downloads.platforms.filter((platform) => platform.id !== primary?.id);

  return (
    <div className="mt-2 space-y-2">
      {primary ? (
        <Button size="sm" variant="default" className="w-full" asChild>
          <a href={primary.url} target="_blank" rel="noopener noreferrer">
            <Download className="mr-1.5 size-4" />
            {t("settings.devices.downloadFor", {
              platform: primary.label,
              defaultValue: "Download for {{platform}}",
            })}
          </a>
        </Button>
      ) : null}
      {others.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {others.map((platform) => (
            <Button key={platform.id} size="sm" variant="outline" asChild>
              <a href={platform.url} target="_blank" rel="noopener noreferrer">
                {platform.label}
              </a>
            </Button>
          ))}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <a
          href={downloads.releasesUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline-offset-2 hover:underline"
        >
          {t("settings.devices.allReleases", { defaultValue: "View all releases" })}
        </a>
        <span className="text-muted-foreground">· v{downloads.version}</span>
      </div>
      <div className="rounded-md border border-border/50 bg-muted/40 px-2 py-2">
        <p className="text-[12px] font-medium text-foreground/90">
          {t("settings.devices.sourceInstall", { defaultValue: "Or install from source" })}
        </p>
        <p className="mt-1 break-all font-mono text-[11px] text-foreground/90">
          {downloads.sourceInstall}
        </p>
        <Button size="sm" variant="ghost" className="mt-1 h-7 px-2 text-[12px]" onClick={() => void copySourceInstall()}>
          {copied
            ? t("settings.devices.copiedInstall", { defaultValue: "Copied" })
            : t("settings.devices.copyInstall", { defaultValue: "Copy command" })}
        </Button>
      </div>
    </div>
  );
}

function DeviceRow({
  node,
  onRevoke,
  onManage,
}: {
  node: ConnectorNode;
  onRevoke: (nodeId: string) => Promise<void>;
  onManage?: () => void;
}) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const rootsSummary = formatRoots(node.roots);

  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <Laptop className="size-5 shrink-0 text-muted-foreground" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[13px] font-medium text-foreground/90">
            {node.alias || node.name || node.nodeId}
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
                node.online
                  ? "bg-emerald-500/10 text-emerald-600"
                  : "bg-muted text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  node.online ? "bg-emerald-500" : "bg-muted-foreground/50",
                )}
              />
              {node.online
                ? t("settings.devices.online", { defaultValue: "online" })
                : t("settings.devices.offline", { defaultValue: "offline" })}
            </span>
          </div>
          <div className="text-[12px] text-muted-foreground">
            {node.platform}
            {node.lastSeenAt
              ? ` · ${t("settings.devices.lastSeen", { defaultValue: "last seen" })} ${node.lastSeenAt}`
              : ""}
          </div>
          {rootsSummary ? (
            <div className="mt-0.5 truncate text-[11px] text-muted-foreground/80">
              {t("settings.devices.sharedRoots", {
                defaultValue: "Shared: {{roots}}",
                roots: rootsSummary,
              })}
            </div>
          ) : null}
        </div>
      </div>

      {confirming ? (
        <div className="flex shrink-0 items-center gap-2">
          <span className="text-[12px] text-muted-foreground">
            {t("settings.devices.confirmRevoke", { defaultValue: "Revoke this device?" })}
          </span>
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onRevoke(node.nodeId);
              } finally {
                setBusy(false);
                setConfirming(false);
              }
            }}
          >
            {busy ? <Loader2 className="size-4 animate-spin" /> : t("settings.devices.revoke", { defaultValue: "Revoke" })}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
            {t("settings.actions.cancel", { defaultValue: "Cancel" })}
          </Button>
        </div>
      ) : (
        <div className="flex shrink-0 items-center gap-1">
          {onManage ? (
            <Button size="sm" variant="ghost" onClick={onManage}>
              <Settings2 className="mr-1.5 size-4" />
              {t("settings.devices.manage", { defaultValue: "Manage" })}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:text-destructive"
            onClick={() => setConfirming(true)}
          >
            <Trash2 className="mr-1.5 size-4" />
            {t("settings.devices.revoke", { defaultValue: "Revoke" })}
          </Button>
        </div>
      )}
    </div>
  );
}

function AddDeviceWizard({
  initialOnlineNodeIds,
  onClose,
}: {
  initialOnlineNodeIds: string[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [code, setCode] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(CODE_TTL_S);
  const [error, setError] = useState<string | null>(null);
  const [pairedOnline, setPairedOnline] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Snapshot of the devices online when the wizard opened; intentionally NOT
  // refreshed afterwards, so a device coming online later is always detected
  // (including re-paired devices that keep their node id).
  const knownRef = useRef(new Set(initialOnlineNodeIds));
  const serverUrl = connectorServerUrl();

  const generate = useCallback(async () => {
    setError(null);
    setPairedOnline(false);
    try {
      const payload = await createConnectorPairingCode(token);
      setCode(payload.code);
      const nowS = Math.floor(Date.now() / 1000);
      const ttl = Math.max(1, Math.floor(payload.expiresAt - nowS));
      setRemaining(Number.isFinite(ttl) && ttl > 0 ? ttl : CODE_TTL_S);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    void generate();
  }, [generate]);

  useEffect(() => {
    if (code === null) return;
    timerRef.current = setInterval(() => {
      setRemaining((r) => Math.max(0, r - 1));
    }, 1000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [code]);

  useEffect(() => {
    if (!code || pairedOnline) return;
    const poll = setInterval(async () => {
      try {
        const payload = await fetchConnectorNodes(token);
        const online = (payload.nodes ?? []).filter((n) => n.online);
        const hasNew = online.some((n) => !knownRef.current.has(n.nodeId));
        if (hasNew) {
          setPairedOnline(true);
        }
      } catch {
        // ignore transient poll errors while waiting
      }
    }, WIZARD_POLL_MS);
    return () => clearInterval(poll);
  }, [code, pairedOnline, token]);

  const expired = remaining <= 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-xl border border-border/60 bg-background p-6 shadow-xl">
        <h3 className="text-[15px] font-semibold">
          {t("settings.devices.wizardTitle", { defaultValue: "Connect a device" })}
        </h3>
        <ol className="mt-4 space-y-3 text-[13px] text-muted-foreground">
          <li>
            <span className="font-medium text-foreground/90">
              1. {t("settings.devices.step1", { defaultValue: "Install the connector" })}
            </span>
            <p>
              {t("settings.devices.step1desc", {
                defaultValue:
                  "Download and install nanobot-connector on the computer that holds your files.",
              })}
            </p>
            <ConnectorDownloadPanel token={token} />
          </li>
          <li>
            <span className="font-medium text-foreground/90">
              2. {t("settings.devices.step2", { defaultValue: "Pair with this code" })}
            </span>
            {error ? (
              <p className="text-destructive">{error}</p>
            ) : code ? (
              <div className="mt-1">
                <div
                  className={cn(
                    "rounded-md border px-3 py-2 text-center font-mono text-lg tracking-[0.3em]",
                    expired
                      ? "border-border/50 text-muted-foreground line-through"
                      : "border-primary/40 text-foreground",
                  )}
                >
                  {code}
                </div>
                <p className="mt-1 text-center text-[12px]">
                  {expired
                    ? t("settings.devices.codeExpired", { defaultValue: "Code expired" })
                    : t("settings.devices.codeExpires", {
                        seconds: remaining,
                        defaultValue: "Expires in {{seconds}}s",
                      })}
                </p>
                <p className="mt-2 break-all rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground/90">
                  {t("settings.devices.pairCommand", {
                    server: serverUrl,
                    code,
                    defaultValue: "nanobot-connector pair --server {{server}} --code {{code}}",
                  })}
                </p>
                {expired ? (
                  <Button size="sm" variant="outline" className="mt-2 w-full" onClick={() => void generate()}>
                    <RefreshCw className="mr-1.5 size-4" />
                    {t("settings.devices.regenerate", { defaultValue: "New code" })}
                  </Button>
                ) : null}
              </div>
            ) : (
              <Loader2 className="size-4 animate-spin" />
            )}
          </li>
          <li>
            <span className="font-medium text-foreground/90">
              3. {t("settings.devices.step3", { defaultValue: "Wait for it to appear online" })}
            </span>
            {pairedOnline ? (
              <p className="mt-1 text-emerald-600">
                {t("settings.devices.pairedOnline", {
                  defaultValue: "Device connected. You can close this wizard.",
                })}
              </p>
            ) : (
              <p className="mt-1 flex items-center gap-1.5 text-[12px]">
                <Loader2 className="size-3.5 animate-spin" />
                {t("settings.devices.waitingOnline", { defaultValue: "Waiting for device to come online…" })}
              </p>
            )}
          </li>
        </ol>

        <div className="mt-5 flex items-center justify-between">
          <Button size="sm" variant="ghost" onClick={() => void generate()} disabled={!expired && code !== null}>
            <RefreshCw className="mr-1.5 size-4" />
            {t("settings.devices.regenerate", { defaultValue: "New code" })}
          </Button>
          <Button size="sm" onClick={onClose}>
            {t("settings.devices.done", { defaultValue: "Done" })}
          </Button>
        </div>
      </div>
    </div>
  );
}
