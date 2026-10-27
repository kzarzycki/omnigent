// Chat link previews may only ever ask this server, and only for github.com
// links of four shapes or this server's own session links. These tests watch
// the fetch layer itself, so a link to any other host proving request-free here
// holds for the server and the browser alike.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

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

function renderMarkdown(markdown: string): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>
    </QueryClientProvider>,
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
        connected: true,
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
    expect(urls[0].pathname).toBe("/v1/connections/github/repos/acme/app/previews/pull/42");

    await userEvent.hover(screen.getByRole("link", { name: "app#42" }));
    const card = await screen.findByTestId("github-link-preview");
    expect(card).toHaveTextContent("Fix rendering on narrow screens");
    expect(card).toHaveTextContent("bug");
    expect(card).toHaveTextContent("Checks: 1 failed, 5 passed");
  });

  it("leaves a GitHub link plain when the viewer's connection can't read it", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ connected: true, preview: null }));
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
