import { MonitorCloudIcon, MonitorIcon } from "lucide-react";

import type { Host } from "@/hooks/useHosts";

/** "v0.19.0 · 3609fea" for a host that reported its build, else null. */
export function hostBuildLabel(host: Host): string | null {
  if (!host.version) return null;
  return host.commit ? `v${host.version} · ${host.commit.slice(0, 7)}` : `v${host.version}`;
}

/**
 * Compact host row for a `<Select>` item — icon, name, and an
 * online/offline pill.
 *
 * Its own module rather than a sibling dialog's export: the dialogs that
 * render host pickers also reference each other (``ResumeWithDirectoryDialog``
 * takes ``buildReconnectCommand`` from ``ReconnectSessionDialog``, which
 * offers ``SwitchHostDialog``), so hanging the shared label off any one of
 * them closes an import cycle.
 *
 * @param host - The host to render, e.g. ``{name: "mac-laptop", status:
 *   "online"}``.
 */
export function HostLabel({ host }: { host: Host }) {
  const isOnline = host.status === "online";
  const build = hostBuildLabel(host);
  return (
    <span className="flex items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 text-muted-foreground" />
      )}
      <span className="text-ui">{host.name}</span>
      {build && <span className="text-[10px] text-muted-foreground">{build}</span>}
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${
          isOnline ? "text-green-600" : "text-muted-foreground"
        }`}
      >
        <span
          className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
        />
        {host.status}
      </span>
    </span>
  );
}
