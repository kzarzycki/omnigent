import { Loader2Icon, MoonIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useStopSession } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { isSessionStoppable } from "@/lib/sessionStop";
import { cn } from "@/lib/utils";
import { ComposerConfigTooltipRows, ComposerMenuTooltip } from "./ComposerControls";

export type SessionPowerState = "live" | "asleep";

/** Upper bound on the spinner after a successful switch, if liveness never flips. */
const SETTLE_MS = 30_000;

/**
 * Composer toggle that shows whether the session's runner is live or asleep and
 * switches it: Sleep stops the runner (`stop_session`), Resume relaunches it
 * (`retry_session`) without sending a message. Read-only viewers see the state only.
 */
export function SessionPowerToggle({
  sessionId,
  state,
  onResume,
  readOnly = false,
  turnRunning = false,
}: {
  sessionId: string;
  state: SessionPowerState;
  onResume: () => Promise<void>;
  readOnly?: boolean;
  /** A turn is streaming; sleeping now would kill it, so Sleep waits. */
  turnRunning?: boolean;
}) {
  const { session } = useSession(sessionId);
  const stopSession = useStopSession();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const settleTimer = useRef<number>(0);

  // The switch is done once liveness reports the new state.
  useEffect(() => {
    window.clearTimeout(settleTimer.current);
    setPending(false);
  }, [state]);
  useEffect(() => () => window.clearTimeout(settleTimer.current), []);

  const stoppable = isSessionStoppable({
    labels: session?.labels,
    hostId: session?.hostId,
    runnerId: session?.runnerId,
  });
  const live = state === "live";
  const disabledReason = readOnly
    ? "Only the session owner can switch it"
    : live && !stoppable
      ? "This session can't be put to sleep"
      : live && turnRunning
        ? "Wait for the turn to finish"
        : null;

  const toggle = async () => {
    if (pending || disabledReason) return;
    setError(null);
    setPending(true);
    try {
      if (live) await stopSession.mutateAsync(sessionId);
      else await onResume();
      // Liveness flips on the next poll or stream push and clears `pending`.
      settleTimer.current = window.setTimeout(() => setPending(false), SETTLE_MS);
    } catch (cause) {
      setError(cause instanceof Error && cause.message ? cause.message : "Something went wrong");
      setPending(false);
    }
  };

  const label = pending ? (live ? "Sleeping…" : "Resuming…") : live ? "Live" : "Asleep";
  const tooltipRows = [
    {
      label: "Session",
      value: live ? "Live" : "Asleep — your next message wakes it",
    },
    {
      label: error ? "Error" : "Click",
      value:
        error ??
        disabledReason ??
        (live ? "Put it to sleep (stops its runner and side chats on it)" : "Resume it now"),
    },
  ];

  return (
    <ComposerMenuTooltip
      menuOpen={false}
      content={<ComposerConfigTooltipRows rows={tooltipRows} />}
      testId="composer-session-power-tooltip"
      className="shrink-0"
    >
      <button
        type="button"
        role="switch"
        aria-checked={live}
        aria-label={`Session ${label.toLowerCase()}`}
        aria-disabled={disabledReason !== null || pending}
        onClick={() => void toggle()}
        data-testid="composer-session-power"
        className={cn(
          "flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-muted-foreground text-sm transition-colors hover:bg-muted/70 hover:text-foreground dark:hover:bg-muted/50 md:h-7",
          (disabledReason !== null || pending) && "cursor-default hover:bg-transparent",
          error && "text-destructive",
        )}
      >
        {pending ? (
          <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
        ) : live ? (
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-success" />
        ) : (
          <MoonIcon className="size-3.5 shrink-0" aria-hidden />
        )}
        <span className="max-sm:sr-only">{label}</span>
      </button>
    </ComposerMenuTooltip>
  );
}
