import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { absoluteTime } from "@/lib/relativeTime";
import type { Conversation } from "@/hooks/useConversations";
import { CopyPathButton } from "./CopyPathButton";

/** Rows shown for a session; empty values are dropped. Built from the list row, no fetch. */
export function sessionDetailRows(
  conversation: Conversation,
  project: string | null,
): { label: string; value: string }[] {
  const rows: [string, string | null | undefined][] = [
    ["Session ID", conversation.id],
    ["Title", conversation.title],
    ["Agent", conversation.agent_name],
    ["Host", conversation.host_id],
    ["Workspace", conversation.workspace],
    ["Branch", conversation.git_branch],
    ["Project", project],
    ["Owner", conversation.owner],
    ["Created", absoluteTime(conversation.created_at * 1000)],
    ["Updated", absoluteTime(conversation.updated_at * 1000)],
  ];
  return rows.flatMap(([label, value]) => (value ? [{ label, value }] : []));
}

export function SessionDetailsDialog({
  conversation,
  project,
  onOpenChange,
  onCloseFocus,
}: {
  conversation: Conversation;
  project: string | null;
  onOpenChange: (open: boolean) => void;
  /** Puts focus back on the row the dialog was opened from. */
  onCloseFocus: () => void;
}) {
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="session-details-dialog"
        onClick={(e) => e.stopPropagation()}
        // Focus the dialog itself: auto-focusing the first copy button pops its tooltip
        // over the next row's button.
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          (e.target as HTMLElement).focus();
        }}
        // No trigger element exists (the menu item is gone), so hand focus back by hand.
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          onCloseFocus();
        }}
      >
        <DialogHeader>
          <DialogTitle>Session details</DialogTitle>
          <DialogDescription className="sr-only">
            Details of this session. Each value has a copy button.
          </DialogDescription>
        </DialogHeader>
        <dl className="flex max-h-[70dvh] flex-col gap-3 overflow-y-auto text-ui">
          {sessionDetailRows(conversation, project).map(({ label, value }) => (
            <div key={label} className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <dt className="text-muted-foreground text-sm">{label}</dt>
                <dd className="break-all" data-testid={`session-detail-${label}`}>
                  {value}
                </dd>
              </div>
              <CopyPathButton path={value} label={`Copy ${label.toLowerCase()}`} />
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  );
}
