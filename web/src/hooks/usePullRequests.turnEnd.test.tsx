// Turn-end refresh discovers PRs without opening the panel; composer polling
// then tracks their status until all linked PRs are terminal.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import { usePullRequestInfo, usePullRequestInfos } from "./usePullRequests";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

type StubStatus = "idle" | "running" | "waiting" | "failed";

function stubChatStore(conversationId: string | null, sessionStatus: StubStatus) {
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (
        selector as (s: { conversationId: string | null; sessionStatus: StubStatus }) => unknown
      )({ conversationId, sessionStatus });
    }
    return undefined;
  });
}

function pullRequestInfoResponse(info: Record<string, unknown> = {}): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ object: "session.github.info", available: true, ...info }),
  } as unknown as Response;
}

function PullRequestRow({ id, prUrl }: { id: string; prUrl: string }) {
  usePullRequestInfo(id, { poll: "list", prUrl });
  return null;
}

function Probe({
  id,
  batch = false,
  poll,
}: {
  id: string | undefined;
  batch?: boolean;
  poll?: "linked";
}) {
  usePullRequestInfo(id, { poll });
  usePullRequestInfos(id, batch ? ["https://github.com/org/repo/pull/1"] : []);
  return null;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(pullRequestInfoResponse());
  vi.stubGlobal("fetch", fetchMock);
  onlineMock.mockReturnValue(true);
  hostOnlineMock.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("usePullRequestInfo turn-end invalidate", () => {
  it.each([false, true])(
    "refetches info when the focused session goes running → idle (batch=%s)",
    async (batch) => {
      stubChatStore("conv_live", "running");
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
      const { rerender } = render(
        <QueryClientProvider client={qc}>
          <Probe id="conv_live" batch={batch} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(batch ? 2 : 1));

      // Turn end refetches every github-info query for the session.
      stubChatStore("conv_live", "idle");
      rerender(
        <QueryClientProvider client={qc}>
          <Probe id="conv_live" batch={batch} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(batch ? 4 : 2));
      expect(fetchMock.mock.calls[batch ? 2 : 1][0]).toBe(
        "/v1/sessions/conv_live/resources/github",
      );
      if (batch) {
        expect(fetchMock.mock.calls[3][0]).toBe(
          "/v1/sessions/conv_live/resources/github?pr_url=https%3A%2F%2Fgithub.com%2Forg%2Frepo%2Fpull%2F1",
        );
      }
    },
  );

  it("does not refetch when status stays idle across renders", async () => {
    // Guard against a spurious refetch on every render — only the
    // active → idle transition triggers the trailing edge.
    stubChatStore("conv_live", "idle");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["selected", "linked"])(
    "discovers a %s PR at turn end and polls until linked PRs are terminal",
    async (source) => {
      vi.useFakeTimers();
      stubChatStore("conv_live", "running");
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = (
        <QueryClientProvider client={client}>
          <Probe id="conv_live" poll="linked" />
        </QueryClientProvider>
      );
      const { rerender } = render(tree);
      await act(() => vi.advanceTimersByTimeAsync(90_000));
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const pr = { url: "https://github.com/org/repo/pull/1", state: "OPEN" };
      fetchMock.mockResolvedValue(
        pullRequestInfoResponse(source === "selected" ? { pr } : { prs: [pr] }),
      );
      stubChatStore("conv_live", "idle");
      rerender(
        <QueryClientProvider client={client}>
          <Probe id="conv_live" poll="linked" />
        </QueryClientProvider>,
      );
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(fetchMock).toHaveBeenCalledTimes(3);

      fetchMock.mockResolvedValue(
        pullRequestInfoResponse({
          pr: null,
          prs: [
            { ...pr, state: "MERGED" },
            { ...pr, url: `${pr.url}2`, state: "CLOSED" },
          ],
        }),
      );
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(fetchMock).toHaveBeenCalledTimes(4);
      await act(() => vi.advanceTimersByTimeAsync(90_000));
      expect(fetchMock).toHaveBeenCalledTimes(4);
    },
  );

  it("fetches each open PR once at turn end, joining in-flight fetches and skipping merged/closed PRs", async () => {
    // N rows each register a turn-end invalidate; without dedupe this is N×N fetches.
    const rowCount = 10;
    const urls = Array.from(
      { length: rowCount },
      (_, i) => `https://github.com/org/repo/pull/${i}`,
    );
    const stateByUrl = new Map(
      urls.map((url, i) => [url, i < 3 ? "MERGED" : i < 6 ? "CLOSED" : "OPEN"]),
    );
    const openCount = urls.filter((url) => stateByUrl.get(url) === "OPEN").length;
    const respond = (input: string) => {
      const prUrl = decodeURIComponent(String(input).split("pr_url=")[1] ?? "");
      return pullRequestInfoResponse(
        prUrl ? { pr: { url: prUrl, state: stateByUrl.get(prUrl) } } : {},
      );
    };
    fetchMock.mockImplementation(async (input: string) => respond(input));
    stubChatStore("conv_live", "running");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const tree = () => (
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
        {urls.map((url) => (
          <PullRequestRow key={url} id="conv_live" prUrl={url} />
        ))}
      </QueryClientProvider>
    );
    const { rerender } = render(tree());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(rowCount + 1));
    await waitFor(() => expect(qc.isFetching()).toBe(0));

    // Slow fetches: each stays in flight until released.
    fetchMock.mockClear();
    const release: Array<() => void> = [];
    fetchMock.mockImplementation(
      (input: string) =>
        new Promise<Response>((resolve) => {
          release.push(() => resolve(respond(input)));
        }),
    );
    stubChatStore("conv_live", "idle");
    rerender(tree());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1 + openCount));

    // A second turn end while those fetches are in flight joins them, not restarts them.
    stubChatStore("conv_live", "running");
    rerender(tree());
    stubChatStore("conv_live", "idle");
    rerender(tree());
    await waitFor(() => expect(qc.isFetching()).toBe(1 + openCount));
    expect(fetchMock).toHaveBeenCalledTimes(1 + openCount);

    act(() => {
      release.forEach((resolveFetch) => resolveFetch());
    });
    await waitFor(() => expect(qc.isFetching()).toBe(0));
    expect(fetchMock).toHaveBeenCalledTimes(1 + openCount);
  });
});
