// Previews for chat links Omnigent can read through its own APIs: GitHub
// issues, PRs, commits and Actions runs (via the server, with the viewer's
// GitHub connection) and sessions on this server. No other link is fetched.

import type React from "react";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { fetchGithubLinkPreview, type GithubLinkPreview } from "@/lib/githubIntegration";
import { parseGithubLink, parseSessionLink, type GithubLinkRef } from "@/lib/linkPreview";
import { PullRequestLabelChip } from "@/shell/PullRequestLabels";
import { PullRequestStatus } from "@/shell/PullRequestStatus";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getSessionSlim } from "@/lib/sessionsApi";

// A preview's state and checks change; a minute keeps them close to live.
const PREVIEW_STALE_MS = 60_000;

const SUBJECT: Record<GithubLinkPreview["kind"], string> = {
  pull: "Pull request",
  issue: "Issue",
  commit: "Commit",
  run: "Workflow run",
};

/** Whether `ref`'s element has scrolled into view; stays true once it has. */
function useSeen(ref: React.RefObject<HTMLElement | null>, active: boolean): boolean {
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!active || seen || !element) return;
    if (typeof IntersectionObserver === "undefined") {
      setSeen(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setSeen(true);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, active, seen]);
  return seen;
}

// Re-read each minute while shown; a failed read hides the preview rather than
// keeping what an earlier read returned, so revoked access shows nothing.
const PREVIEW_QUERY = {
  staleTime: PREVIEW_STALE_MS,
  refetchInterval: PREVIEW_STALE_MS,
  retry: false,
} as const;

function useGithubPreview(link: GithubLinkRef | null, enabled: boolean) {
  const { data, isError } = useQuery({
    queryKey: ["github-link-preview", link?.owner, link?.repo, link?.kind, link?.ref],
    queryFn: () => fetchGithubLinkPreview(link as GithubLinkRef),
    enabled: enabled && link !== null,
    ...PREVIEW_QUERY,
  });
  return isError ? null : (data?.preview ?? null);
}

// Its own query rather than the open session's never-stale cache.
function useSessionPreview(sessionId: string | null, enabled: boolean) {
  const { data, isError } = useQuery({
    queryKey: ["session-link-preview", sessionId],
    queryFn: () => getSessionSlim(sessionId as string),
    enabled: enabled && sessionId !== null,
    ...PREVIEW_QUERY,
  });
  return isError ? null : (data ?? null);
}

function ChecksSummary({ checks }: { checks: NonNullable<GithubLinkPreview["checks"]> }) {
  const parts = [
    checks.failed ? `${checks.failed} failed` : "",
    checks.pending ? `${checks.pending} pending` : "",
    checks.passed ? `${checks.passed} passed` : "",
  ].filter(Boolean);
  const tone = checks.failed
    ? "text-red-700 dark:text-red-400"
    : checks.pending
      ? "text-amber-700 dark:text-amber-400"
      : "text-green-700 dark:text-green-400";
  return <p className={`text-xs ${tone}`}>Checks: {parts.join(", ")}</p>;
}

function GithubPreviewCard({ preview }: { preview: GithubLinkPreview }) {
  return (
    <div className="space-y-1.5" data-testid="github-link-preview">
      <p className="font-medium leading-snug break-words">{preview.title}</p>
      {preview.state && <PullRequestStatus state={preview.state} subject={SUBJECT[preview.kind]} />}
      {preview.labels.length > 0 && (
        <ul aria-label="Labels" className="flex flex-wrap gap-1">
          {preview.labels.map((label) => (
            <PullRequestLabelChip key={label.name} label={label} />
          ))}
        </ul>
      )}
      {preview.checks && <ChecksSummary checks={preview.checks} />}
    </div>
  );
}

/**
 * Wraps a chat link: a previewable one gets its state beside it and a hover
 * card with the details, loaded once the link scrolls into view; any other
 * link is returned unchanged and nothing is fetched for it.
 *
 * @param href - The link's destination, already rebased under the base path.
 * @param children - The rendered anchor.
 */
export function ChatLinkPreview({
  href,
  children,
}: {
  href: string | undefined;
  children: React.ReactElement;
}) {
  const github = parseGithubLink(href);
  const sessionId = github ? null : parseSessionLink(href, window.location.origin);
  const triggerRef = useRef<HTMLSpanElement>(null);
  const seen = useSeen(triggerRef, github !== null || sessionId !== null);
  // Without the GitHub connection the preview route doesn't exist: ask nothing.
  const info = useServerInfo();
  const githubEnabled = info !== "loading" && (info.enabled_connections ?? []).includes("github");
  const preview = useGithubPreview(github, seen && githubEnabled);
  const session = useSessionPreview(sessionId, seen);

  if (!github && !sessionId) return children;

  const card = preview ? (
    <GithubPreviewCard preview={preview} />
  ) : session ? (
    <div className="space-y-1.5" data-testid="session-link-preview">
      <p className="font-medium leading-snug break-words">{session.title ?? "Untitled session"}</p>
      <PullRequestStatus state={session.status} subject="Session" />
    </div>
  ) : null;

  return (
    <HoverCard openDelay={200} closeDelay={100}>
      <HoverCardTrigger asChild>
        <span ref={triggerRef} className="inline">
          {children}
          {preview?.state && (
            <PullRequestStatus
              state={preview.state}
              subject={SUBJECT[preview.kind]}
              className="ml-1 align-[0.1em]"
            />
          )}
        </span>
      </HoverCardTrigger>
      {card && <HoverCardContent className="w-72">{card}</HoverCardContent>}
    </HoverCard>
  );
}
