// An orchestrator's status line with its age ("waits on CI · 3m"), computed
// from the browser's clock and re-rendered by the shared 30 s tick, so a line
// its writer stopped updating turns stale instead of looking current.

import { useNow } from "@/hooks/useNow";
import { cn } from "@/lib/utils";
import { sessionStatusAge } from "@/shell/sidebarNav";
import type { Conversation } from "@/hooks/useConversations";

export function SessionStatusLine({
  conversation,
  className,
  testId,
}: {
  conversation: Pick<Conversation, "labels">;
  className?: string;
  testId: string;
}) {
  const status = sessionStatusAge(conversation, useNow().getTime());
  if (!status) return null;
  return (
    <span
      data-testid={testId}
      data-stale={status.stale || undefined}
      className={cn(className, status.stale && "opacity-60")}
      title={status.line}
    >
      <span className={status.stale ? "line-through" : undefined}>{status.line}</span>
      {status.age && ` · ${status.age}`}
      {status.stale && <span className="sr-only"> (stale)</span>}
    </span>
  );
}
