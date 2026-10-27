import { useReducer } from "react";
import { ArrowUpCircleIcon } from "lucide-react";
import { useHosts, type Host } from "@/hooks/useHosts";
import { dateKey } from "@/lib/arcaShutdownWarning";
import { cn } from "@/lib/utils";
import { CommandCopyButton } from "./ArcaShutdownBanner";

const UPGRADE_COMMAND = "omni upgrade";
// Tab-local fallback keeps "Not now" working when localStorage throws, as in private mode.
const unavailableStorage = new Map<string, string>();

function dismissalKey(host: Host): string {
  return `omnigent:host-update:dismissed:${host.host_id}`;
}

// A dismissal holds for one target version on one day, so the nudge returns
// tomorrow and whenever the server moves ahead again.
function dismissalValue(host: Host, now: Date): string {
  return `${host.update_version}@${dateKey(now)}`;
}

function isDismissed(host: Host): boolean {
  try {
    const key = dismissalKey(host);
    const stored = unavailableStorage.get(key) ?? localStorage.getItem(key);
    return stored === dismissalValue(host, new Date());
  } catch {
    return unavailableStorage.get(dismissalKey(host)) === dismissalValue(host, new Date());
  }
}

/** The session's host when it runs an older Omnigent than the server, else null. */
export function useOutdatedHost(hostId: string | null | undefined) {
  const { data: hosts } = useHosts({ enabled: Boolean(hostId) });
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const outdated = host?.status === "online" && host.update_version && !isDismissed(host);
  return {
    host: outdated ? host : null,
    dismissToday(target: Host) {
      const key = dismissalKey(target);
      const value = dismissalValue(target, new Date());
      try {
        localStorage.setItem(key, value);
      } catch {
        unavailableStorage.set(key, value);
      }
      refresh();
    },
  };
}

export function HostUpdateBanner({
  host,
  onDismiss,
  hasPinnedAbove = false,
}: {
  host: Host;
  onDismiss: () => void;
  hasPinnedAbove?: boolean;
}) {
  return (
    <div
      role="status"
      className={cn(
        "shrink-0 px-3 md:px-4",
        hasPinnedAbove ? "mt-1" : "chat-plan-accordion mt-14 md:mt-12",
      )}
    >
      <div className="mx-auto flex max-w-3xl items-start gap-2 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2 text-foreground">
        <ArrowUpCircleIcon className="mt-0.5 size-3.5 shrink-0 text-primary" aria-hidden="true" />
        <div className="min-w-0 flex-1 text-ui">
          <p className="font-medium">
            Omnigent {host.update_version} is available for {host.name}.
          </p>
          <p className="mt-0.5 text-muted-foreground">
            It runs {host.version}. Run this on {host.name} to get the latest fixes. Running
            sessions finish first.
          </p>
          <div className="mt-1.5 flex flex-wrap gap-2">
            <CommandCopyButton command={UPGRADE_COMMAND} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
            <button
              type="button"
              className="underline underline-offset-2 hover:no-underline"
              onClick={onDismiss}
            >
              Not now
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
