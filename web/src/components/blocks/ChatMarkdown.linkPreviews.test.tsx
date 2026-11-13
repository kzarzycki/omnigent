// Chat link previews may only ever ask this server, and only for github.com
// links of four shapes or this server's own session links. These tests watch
// the fetch layer itself, so a link to any other host proving request-free here
// holds for the server and the browser alike.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const CHAT_SESSION_ID = "conv_chat";
const fetchMock = vi.fn();

// Every observed element is in view at once, as if the message scrolled in.
class InViewObserver {
  private readonly callback: IntersectionObserverCallback;
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element): void {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  disconnect(): void {}
  unobserve(): void {}
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("IntersectionObserver", InViewObserver);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// The chat's own session scopes GitHub previews; none, as outside a session.
type ViewerContext = NonNullable<React.ContextType<typeof FileViewerContext>>;

function viewerContext(sessionId: string | null): ViewerContext {
  return {
    openFile: () => {},
    openGithubTab: () => {},
    isChangedPath: () => false,
    conversationId: sessionId ?? undefined,
    workspaceRoot: null,
    workspaceHome: null,
  };
}

const IN_CHAT = viewerContext(CHAT_SESSION_ID);
const OUTSIDE_A_SESSION = viewerContext(null);

function renderMarkdown(markdown: string, context: ViewerContext = IN_CHAT): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <FileViewerContext.Provider value={context}>
      <QueryClientProvider client={client}>
        <FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>
      </QueryClientProvider>
    </FileViewerContext.Provider>,
  );
  return client;
}

function requestedUrls(): URL[] {
  return fetchMock.mock.calls.map(([input]) => {
    const raw = input instanceof Request ? input.url : String(input);
    return new URL(raw, window.location.origin);
  });
}

describe("chat link previews at the fetch layer", () => {
  it("makes no request at all for links to any other host", async () => {
    renderMarkdown(
      [
        "https://attacker.example/?d=secret-token",
        "[docs](https://example.com/docs/page)",
        "https://169.254.169.254/latest/meta-data/",
        "https://github.com.attacker.example/acme/app/pull/42",
        "http://github.com/acme/app/pull/42",
        "https://github.com/acme/app/pull/42/files",
        "https://raw.githubusercontent.com/acme/app/main/README.md",
        "[a session elsewhere](https://attacker.example/c/conv_9)",
      ].join("\n\n"),
    );

    expect(await screen.findByRole("link", { name: "docs" })).toBeVisible();
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("asks only this server for a GitHub PR and shows its state, labels and checks", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        preview: {
          kind: "pull",
          title: "Fix rendering on narrow screens",
          state: "merged",
          labels: [{ name: "bug", color: "d73a4a", description: null }],
          checks: { passed: 5, failed: 1, pending: 0 },
        },
      }),
    );
    renderMarkdown("See https://github.com/acme/app/pull/42 and https://attacker.example/x");

    expect(await screen.findByLabelText("Pull request status: Merged")).toBeVisible();
    const urls = requestedUrls();
    expect(urls.map((url) => url.origin)).toEqual([window.location.origin]);
    expect(urls[0].pathname).toBe(`/v1/sessions/${CHAT_SESSION_ID}/resources/github/preview`);
    expect(Object.fromEntries(urls[0].searchParams)).toEqual({
      owner: "acme",
      repo: "app",
      kind: "pull",
      ref: "42",
    });

    await userEvent.hover(screen.getByRole("link", { name: "app#42" }));
    const card = await screen.findByTestId("github-link-preview");
    expect(card).toHaveTextContent("Fix rendering on narrow screens");
    expect(card).toHaveTextContent("bug");
    expect(card).toHaveTextContent("Checks: 1 failed, 5 passed");
  });

  it("sends no GitHub preview request outside a session", async () => {
    renderMarkdown("https://github.com/acme/app/pull/42", OUTSIDE_A_SESSION);

    expect(await screen.findByRole("link", { name: "app#42" })).toBeVisible();
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("leaves a GitHub link plain when the session host can't read it", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ preview: null }));
    renderMarkdown("https://github.com/acme/private/issues/3");

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("link", { name: "private#3" })).toBeVisible();
    expect(screen.queryByLabelText(/status:/)).toBeNull();
  });

  it("shows a session link's title only from the viewer's own session read", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "conv_9", agent_id: "ag_1", title: "Auth refactor", status: "idle" }),
    );
    renderMarkdown(`[the session](${window.location.origin}/c/conv_9)`);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = requestedUrls();
    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe("/v1/sessions/conv_9");
    await userEvent.hover(screen.getByRole("link", { name: "the session" }));
    expect(await screen.findByTestId("session-link-preview")).toHaveTextContent("Auth refactor");
  });

  it("renders a relative session link as an anchor under the base path, with a preview", async () => {
    vi.stubGlobal("window", Object.assign(window, { __OMNIGENT_BASE_PATH__: "/proxy/6767" }));
    try {
      fetchMock.mockResolvedValue(
        jsonResponse({ id: "conv_9", agent_id: "ag_1", title: "Auth refactor", status: "idle" }),
      );
      const openFile = vi.fn();
      renderMarkdown("[the new chat](/c/conv_9) and [notes](/abs/ws/notes.md)", {
        ...OUTSIDE_A_SESSION,
        openFile,
        isChangedPath: (path: string) => path === "notes.md",
        workspaceRoot: "/abs/ws",
      });

      const link = screen.getByRole("link", { name: "the new chat" });
      expect(link.getAttribute("href")).toBe("/proxy/6767/c/conv_9");
      expect(link.hasAttribute("data-omnigent-file")).toBe(false);
      await userEvent.hover(link);
      expect(await screen.findByTestId("session-link-preview")).toHaveTextContent("Auth refactor");
      // A workspace path still opens the file viewer rather than navigating.
      fireEvent.click(screen.getByRole("button", { name: "notes" }));
      expect(openFile).toHaveBeenCalledWith("notes.md");
    } finally {
      delete window.__OMNIGENT_BASE_PATH__;
    }
  });

  it("shows nothing for a session the viewer can't open", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Forbidden" }, 403));
    renderMarkdown(`[the session](${window.location.origin}/c/conv_secret)`);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await userEvent.hover(screen.getByRole("link", { name: "the session" }));
    await new Promise((resolve) => {
      setTimeout(resolve, 300);
    });
    expect(screen.queryByTestId("session-link-preview")).toBeNull();
  });

  it("drops a session preview once a later read is refused", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: "conv_9", agent_id: "ag_1", title: "Private title", status: "idle" }),
    );
    const client = renderMarkdown(`[the session](${window.location.origin}/c/conv_9)`);
    const link = screen.getByRole("link", { name: "the session" });
    await userEvent.hover(link);
    expect(await screen.findByTestId("session-link-preview")).toHaveTextContent("Private title");

    fetchMock.mockResolvedValue(jsonResponse({ detail: "Forbidden" }, 403));
    await client.refetchQueries({ queryKey: ["session-link-preview"] });

    await waitFor(() => expect(screen.queryByTestId("session-link-preview")).toBeNull());
  });
});

const PULL_PREVIEW = {
  preview: {
    kind: "pull",
    title: "Fix rendering on narrow screens",
    state: "open",
    labels: [{ name: "bug", color: "d73a4a", description: null }],
    checks: { passed: 5, failed: 1, pending: 0 },
  },
};

// A single finger tap on `target`, as a touch screen sends it.
async function tap(target: Element): Promise<void> {
  const user = userEvent.setup();
  await user.pointer([{ keys: "[TouchA>]", target }, { keys: "[/TouchA]" }]);
}

describe("chat link preview on touch and keyboard", () => {
  it("opens the card on a tap of the status label, not of the link text", async () => {
    fetchMock.mockResolvedValue(jsonResponse(PULL_PREVIEW));
    renderMarkdown("See https://github.com/acme/app/pull/42");
    const label = await screen.findByRole("button", { name: "Pull request status: Open" });

    await tap(screen.getByRole("link", { name: "app#42" }));
    expect(screen.queryByTestId("github-link-preview")).toBeNull();
    expect(screen.getByRole("link", { name: "app#42" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/42",
    );

    await tap(label);
    const card = await screen.findByTestId("github-link-preview");
    expect(card).toHaveTextContent("Fix rendering on narrow screens");
    expect(card).toHaveTextContent("bug");
    expect(card).toHaveTextContent("Checks: 1 failed, 5 passed");
  });

  it("closes the card on a tap outside it or on the label again", async () => {
    fetchMock.mockResolvedValue(jsonResponse(PULL_PREVIEW));
    renderMarkdown("See https://github.com/acme/app/pull/42");
    const label = await screen.findByRole("button", { name: "Pull request status: Open" });

    await tap(label);
    await screen.findByTestId("github-link-preview");
    await tap(label);
    await waitFor(() => expect(screen.queryByTestId("github-link-preview")).toBeNull());

    await tap(label);
    await screen.findByTestId("github-link-preview");
    await tap(document.body);
    await waitFor(() => expect(screen.queryByTestId("github-link-preview")).toBeNull());
  });

  it("gives a session link a status label whose tap opens the session card", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "conv_9", agent_id: "ag_1", title: "Auth refactor", status: "idle" }),
    );
    renderMarkdown(`[the session](${window.location.origin}/c/conv_9)`);

    const label = await screen.findByRole("button", { name: "Session status: Idle" });
    await tap(label);
    expect(await screen.findByTestId("session-link-preview")).toHaveTextContent("Auth refactor");
  });

  it("opens the card with Enter or Space on the focused label and closes it with Escape", async () => {
    fetchMock.mockResolvedValue(jsonResponse(PULL_PREVIEW));
    renderMarkdown("See https://github.com/acme/app/pull/42");
    const label = await screen.findByRole("button", { name: "Pull request status: Open" });
    const user = userEvent.setup();

    label.focus();
    await user.keyboard("{Enter}");
    await screen.findByTestId("github-link-preview");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("github-link-preview")).toBeNull());

    await user.keyboard(" ");
    await screen.findByTestId("github-link-preview");
  });

  it("keeps the card closed while the label only has keyboard focus, until Enter", async () => {
    fetchMock.mockResolvedValue(jsonResponse(PULL_PREVIEW));
    renderMarkdown("See https://github.com/acme/app/pull/42");
    const label = await screen.findByRole("button", { name: "Pull request status: Open" });
    const user = userEvent.setup();

    label.focus();
    expect(label).toHaveFocus();
    // Past the hover card's open delay, so a focus-driven open would have landed.
    await new Promise((resolve) => {
      setTimeout(resolve, 300);
    });
    expect(screen.queryByTestId("github-link-preview")).toBeNull();

    await user.keyboard("{Enter}");
    await screen.findByTestId("github-link-preview");
  });

  it("reports the card's open state on the label through aria-expanded", async () => {
    fetchMock.mockResolvedValue(jsonResponse(PULL_PREVIEW));
    renderMarkdown("See https://github.com/acme/app/pull/42");
    const label = await screen.findByRole("button", { name: "Pull request status: Open" });
    expect(label).toHaveAttribute("aria-expanded", "false");

    await tap(label);
    await screen.findByTestId("github-link-preview");
    expect(label).toHaveAttribute("aria-expanded", "true");
    await tap(label);
    await waitFor(() => expect(screen.queryByTestId("github-link-preview")).toBeNull());
    expect(label).toHaveAttribute("aria-expanded", "false");

    const user = userEvent.setup();
    label.focus();
    await user.keyboard("{Enter}");
    await screen.findByTestId("github-link-preview");
    expect(label).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("github-link-preview")).toBeNull());
    expect(label).toHaveAttribute("aria-expanded", "false");
  });
});
