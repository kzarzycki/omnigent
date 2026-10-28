import { useReducer } from "react";
import { ArrowUpCircleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useHosts, useUpdateHost, type Host, type HostUpdate } from "@/hooks/useHosts";
import { dateKey } from "@/lib/arcaShutdownWarning";
import { cn } from "@/lib/utils";
import { CommandCopyButton } from "./ArcaShutdownBanner";

// Tab-local fallback keeps "Not now" working when localStorage throws, as in private mode.
const unavailableStorage = new Map<string, string>();

function dismissalKey(host: Host): string {
  return `omnigent:host-update:dismissed:${host.host_id}`;
}

// A dismissal holds for one target build on one day, so the nudge returns
// tomorrow and whenever the server moves ahead again.
function dismissalValue(update: HostUpdate, now: Date): string {
  return `${update.target}@${dateKey(now)}`;
}

function isDismissed(host: Host, update: HostUpdate): boolean {
  const key = dismissalKey(host);
  const expected = dismissalValue(update, new Date());
  try {
    return (unavailableStorage.get(key) ?? localStorage.getItem(key)) === expected;
  } catch {
    return unavailableStorage.get(key) === expected;
  }
}

export interface OutdatedHost {
  host: Host;
  update: HostUpdate;
}

/** The session's host and the update it needs, while it runs a build the server doesn't want. */
export function useOutdatedHost(hostId: string | null | undefined) {
  const { data: hosts } = useHosts({ enabled: Boolean(hostId) });
  const [, refresh] = useReducer((value: number) => value + 1, 0);
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const update = host?.status === "online" ? host.update : null;
  return {
    outdated: host && update && !isDismissed(host, update) ? { host, update } : null,
    dismissToday({ host: target, update: targetUpdate }: OutdatedHost) {
      const key = dismissalKey(target);
      const value = dismissalValue(targetUpdate, new Date());
      try {
        localStorage.setItem(key, value);
      } catch {
        unavailableStorage.set(key, value);
      }
      refresh();
    },
  };
}

// What the last "Update now" says, for every state but "running".
function runSummary(host: Host, update: HostUpdate): string | null {
  switch (update.run?.state) {
    case "failed":
      return "The update failed.";
    case "finished":
      return `The updater finished. ${host.name} runs ${update.current} until it restarts.`;
    case "restarted":
      return `${host.name} restarted, but it still runs ${update.current}.`;
    default:
      return null;
  }
}

function UpdateNow({ host, update }: OutdatedHost) {
  const updateHost = useUpdateHost(host.host_id);
  const running = update.run?.state === "running" || updateHost.isPending;
  const summary = runSummary(host, update);
  if (running) {
    return (
      <p className="mt-1.5 flex items-center gap-2 text-muted-foreground">
        <Spinner className="size-3.5" />
        Updating {host.name}. Running sessions finish first, so this can take a few minutes.
      </p>
    );
  }
  return (
    <>
      {summary && (
        <div className="mt-1.5">
          <p className="font-medium">{summary}</p>
          {update.run?.output && (
            <pre className="mt-1 max-h-32 overflow-auto rounded border border-border bg-background/70 px-2 py-1 text-xs whitespace-pre-wrap">
              {update.run.output}
            </pre>
          )}
        </div>
      )}
      {updateHost.error && <p className="mt-1.5 text-destructive">{updateHost.error.message}</p>}
      <div className="mt-1.5 flex flex-wrap gap-2">
        <Button size="sm" onClick={() => updateHost.mutate()}>
          {summary ? "Try again" : "Update now"}
        </Button>
      </div>
    </>
  );
}

export function HostUpdateBanner({
  outdated,
  onDismiss,
  hasPinnedAbove = false,
}: {
  outdated: OutdatedHost;
  onDismiss: () => void;
  hasPinnedAbove?: boolean;
}) {
  const { host, update } = outdated;
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
            Omnigent {update.target} is available for {host.name}.
          </p>
          {update.can_update ? (
            <>
              <p className="mt-0.5 text-muted-foreground">It runs {update.current}.</p>
              <UpdateNow host={host} update={update} />
            </>
          ) : (
            <>
              <p className="mt-0.5 text-muted-foreground">
                It runs {update.current}. Run this on {host.name} to get the latest fixes. Running
                sessions finish first.
              </p>
              <div className="mt-1.5 flex flex-wrap gap-2">
                <CommandCopyButton command={update.command} />
              </div>
            </>
          )}
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
