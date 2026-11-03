import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionPowerToggle } from "./SessionPowerToggle";

const stopSession = vi.fn<(id: string) => Promise<unknown>>();
let session: { hostId?: string; runnerId?: string; labels?: Record<string, string> } | null;

vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session }) }));
vi.mock("@/hooks/useConversations", () => ({
  useStopSession: () => ({ mutateAsync: stopSession }),
}));

const HOSTED = { hostId: "host-1", runnerId: "runner-1", labels: {} };

beforeEach(() => {
  session = HOSTED;
  stopSession.mockReset().mockResolvedValue({});
});
afterEach(cleanup);

const toggle = () => screen.getByRole("switch");

describe("SessionPowerToggle", () => {
  it("puts a live session to sleep and stays pending until liveness flips", async () => {
    const onResume = vi.fn();
    const { rerender } = render(
      <SessionPowerToggle sessionId="s1" state="live" onResume={onResume} />,
    );
    expect(toggle()).toHaveAttribute("aria-checked", "true");
    expect(toggle()).toHaveTextContent("Live");

    fireEvent.click(toggle());
    await act(async () => {});
    expect(stopSession).toHaveBeenCalledWith("s1");
    expect(onResume).not.toHaveBeenCalled();
    // The POST resolved but liveness hasn't flipped: no flash back to "Live".
    expect(toggle()).toHaveTextContent("Sleeping…");

    rerender(<SessionPowerToggle sessionId="s1" state="asleep" onResume={onResume} />);
    expect(toggle()).toHaveAttribute("aria-checked", "false");
    expect(toggle()).toHaveTextContent("Asleep");
  });

  it("resumes an asleep session without stopping anything", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    render(<SessionPowerToggle sessionId="s1" state="asleep" onResume={onResume} />);
    fireEvent.click(toggle());
    await act(async () => {});
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(stopSession).not.toHaveBeenCalled();
    expect(toggle()).toHaveTextContent("Resuming…");
  });

  it("returns to the old state with the error when the switch fails", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("No runner is available"));
    render(<SessionPowerToggle sessionId="s1" state="asleep" onResume={onResume} />);
    fireEvent.click(toggle());
    await act(async () => {});
    expect(toggle()).toHaveTextContent("Asleep");
    expect(toggle().className).toContain("text-destructive");
  });

  it.each([
    ["a read-only viewer", { readOnly: true }, HOSTED],
    ["a running turn", { turnRunning: true }, HOSTED],
    ["a session with no host runner", {}, { labels: {} }],
  ])("does not sleep for %s", async (_case, props, row) => {
    session = row;
    render(<SessionPowerToggle sessionId="s1" state="live" onResume={vi.fn()} {...props} />);
    expect(toggle()).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(toggle());
    await act(async () => {});
    expect(stopSession).not.toHaveBeenCalled();
  });
});
