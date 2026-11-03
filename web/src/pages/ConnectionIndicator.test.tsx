import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ConnectionIndicator } from "./ChatIndicators";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";

/**
 * Build a TerminalFirstContextValue with sensible defaults so each test
 * overrides only the fields it exercises. `terminalStartingUp` defaults to
 * false (the steady state).
 */
function makeCtx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
  return {
    isClaudeNative: true,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: vi.fn(),
    terminalsAvailable: true,
    terminalStartingUp: false,
    ...overrides,
  };
}

function renderWithContext(
  liveness: SessionLiveness,
  ctx: TerminalFirstContextValue | null,
  onShowReconnectHelp = vi.fn(),
  onResume?: () => Promise<void>,
) {
  const indicator = (
    <ConnectionIndicator
      liveness={liveness}
      onShowReconnectHelp={onShowReconnectHelp}
      onResume={onResume}
    />
  );
  return render(
    <TooltipProvider>
      {ctx ? (
        <TerminalFirstContextProvider value={ctx}>{indicator}</TerminalFirstContextProvider>
      ) : (
        indicator
      )}
    </TooltipProvider>,
  );
}

const ONLINE: SessionLiveness = { kind: "online" };

afterEach(cleanup);

describe("ConnectionIndicator", () => {
  it("renders nothing for an online non-terminal-first session", () => {
    // With status moved to the sidebar, an online non-CN session has
    // nothing to show in the chat band — the composer sits below.
    const { container } = renderWithContext(ONLINE, null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for an online + non-terminal-first context", () => {
    const { container } = renderWithContext(
      ONLINE,
      makeCtx({ isClaudeNative: false, isTerminalFirst: false, terminalsAvailable: false }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for online terminal-first sessions (toggle lives in the header)", () => {
    // The Chat/Terminal switcher moved to the header (ViewModeToggle); this
    // band no longer renders the in-page pill for reachable terminal-first
    // sessions.
    const { container } = renderWithContext(ONLINE, makeCtx());
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("group", { name: /view mode/i })).toBeNull();
  });

  it("renders nothing while a shell owns the main view (isShellView)", () => {
    const { container } = renderWithContext(
      ONLINE,
      makeCtx({ isShellView: true, view: "terminal" }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the reconnect button for a local_stranded session, regardless of context", () => {
    const onShow = vi.fn();
    renderWithContext({ kind: "local_stranded" }, makeCtx(), onShow);
    const button = screen.getByTestId("disconnected-indicator");
    expect(button).toBeInTheDocument();
    fireEvent.click(button);
    expect(onShow).toHaveBeenCalledTimes(1);
  });

  it("keeps the host-offline banner in the terminal-first TERMINAL view (no composer badge there)", () => {
    // In the terminal view the PTY owns the surface — the composer's host
    // badge isn't on screen, so the banner still carries the reconnect copy.
    renderWithContext({ kind: "host_offline", isOwner: true }, makeCtx({ view: "terminal" }));
    const button = screen.getByTestId("disconnected-indicator");
    expect(button).toHaveTextContent(/host is offline/i);
  });

  it("suppresses the host-offline banner in the chat view (composer badge owns it)", () => {
    // The chat view shows the composer, whose host badge becomes the clickable
    // reconnect affordance — so this band must not double up the banner.
    const { container } = renderWithContext(
      { kind: "host_offline", isOwner: true },
      makeCtx({ view: "chat" }),
    );
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the asleep row with Resume, not a disconnect banner, for runner_asleep", async () => {
    // The composer stays open and the next message wakes the runner; Resume
    // wakes it without sending anything.
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderWithContext({ kind: "runner_asleep" }, null, vi.fn(), onResume);
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
    expect(screen.getByTestId("asleep-indicator")).toHaveTextContent(/session is asleep/i);
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("button", { name: "Resume" })).toBeEnabled();
  });

  it("names the sandbox for host_asleep and shows a failed resume", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("No runner is available"));
    renderWithContext({ kind: "host_asleep" }, makeCtx({ view: "chat" }), vi.fn(), onResume);
    expect(screen.getByTestId("asleep-indicator")).toHaveTextContent(/sandbox is asleep/i);
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText(/couldn't resume: no runner is available/i)).toBeInTheDocument();
  });

  it("omits Resume for a read-only viewer", () => {
    renderWithContext({ kind: "runner_asleep" }, null);
    expect(screen.getByTestId("asleep-indicator")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("leaves the asleep state to the terminal's own Resume in the terminal view", () => {
    const { container } = renderWithContext(
      { kind: "runner_asleep" },
      makeCtx({ view: "terminal" }),
      vi.fn(),
      vi.fn(),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while liveness is unknown (pre-poll)", () => {
    const { container } = renderWithContext({ kind: "unknown" }, null);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the passive Connecting… band for a non-terminal-first starting session", () => {
    // A freshly-created regular session whose runner is spinning up gets a
    // muted, non-interactive heartbeat — NOT a banner, NOT a button.
    renderWithContext({ kind: "starting" }, null);
    const indicator = screen.getByTestId("connecting-indicator");
    expect(indicator).toBeInTheDocument();
    expect(indicator).toHaveTextContent(/connecting/i);
    expect(indicator.tagName).toBe("DIV");
    expect(indicator.querySelector(".animate-spin")).not.toBeNull();
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
  });

  it("does NOT show the Connecting band for a terminal-first starting session", () => {
    // Terminal-first sessions surface startup elsewhere (header toggle +
    // RunnerStartingIndicator), so the generic Connecting band must not take
    // over this band.
    const { container } = renderWithContext(
      { kind: "starting" },
      makeCtx({ terminalsAvailable: false, terminalStartingUp: true }),
    );
    expect(screen.queryByTestId("connecting-indicator")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });
});
