// The state pill shared by the Pull Requests panel and chat link previews.

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const GREEN = "border-green-500/25 bg-green-500/10 text-green-700 dark:text-green-400";
const PURPLE = "border-purple-500/25 bg-purple-500/10 text-purple-700 dark:text-purple-400";
const RED = "border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-400";
const MUTED = "border-border bg-muted text-muted-foreground";

// A state's label and colour; any other state shows muted, as written.
const VISUALS: Record<string, { label: string; className: string }> = {
  OPEN: { label: "Open", className: GREEN },
  MERGED: { label: "Merged", className: PURPLE },
  CLOSED: { label: "Closed", className: RED },
  DRAFT: { label: "Draft", className: MUTED },
  NOT_PLANNED: { label: "Not planned", className: MUTED },
  SUCCESS: { label: "Success", className: GREEN },
  FAILURE: { label: "Failure", className: RED },
};

// Colour for a GitHub issue status's category; any other category shows muted.
const STATUS_CATEGORY_CLASS: Record<string, string> = {
  active: GREEN,
  completed: PURPLE,
};

/**
 * A rounded pill naming a PR's, issue's or run's state, e.g. "Merged".
 *
 * @param state - The provider's state, any case, e.g. `"OPEN"` or `"merged"`.
 * @param isDraft - Whether an open PR is a draft.
 * @param status - An issue's GitHub status; its name replaces the state label and its
 *   category sets the colour.
 * @param subject - What the state belongs to, for the accessible name.
 */
export function PullRequestStatus({
  state,
  isDraft = false,
  status,
  subject = "Pull request",
  className,
}: {
  state: string;
  /** An open PR that is still a draft shows "Draft". */
  isDraft?: boolean;
  status?: { name: string; category: string };
  subject?: string;
  className?: string;
}) {
  const fallback = state.replaceAll("_", " ");
  const key = state.toUpperCase() === "OPEN" && isDraft ? "DRAFT" : state.toUpperCase();
  const visual = status
    ? { label: status.name, className: STATUS_CATEGORY_CLASS[status.category] ?? MUTED }
    : (VISUALS[key] ?? {
        label: fallback.charAt(0).toUpperCase() + fallback.slice(1),
        className: MUTED,
      });
  return (
    <Badge
      aria-label={`${subject} status: ${visual.label}`}
      className={cn(
        "h-5 rounded-full border px-2 py-px text-xs leading-none",
        visual.className,
        className,
      )}
    >
      {visual.label}
    </Badge>
  );
}
