import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionStatusLine } from "./SessionStatusLine";

const T0 = Date.UTC(2026, 9, 20, 12, 0, 0);
const at = (secondsAgo: number) => String(T0 / 1000 - secondsAgo);

function renderLine(labels: Record<string, string>) {
  return render(<SessionStatusLine conversation={{ labels }} testId="line" />);
}

describe("SessionStatusLine", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => vi.useRealTimers());

  it("ages from status.at on the browser clock and turns stale past 10 minutes", () => {
    renderLine({ "status.line": "waits on CI", "status.at": at(60) });
    const line = screen.getByTestId("line");
    expect(line).toHaveTextContent("waits on CI · 1m");
    expect(line).not.toHaveAttribute("data-stale");

    act(() => void vi.advanceTimersByTime(4 * 60_000));
    expect(line).toHaveTextContent("waits on CI · 5m");
    expect(line).not.toHaveAttribute("data-stale");

    act(() => void vi.advanceTimersByTime(6 * 60_000));
    expect(line).toHaveTextContent("waits on CI · 11m (stale)");
    expect(line).toHaveAttribute("data-stale", "true");
  });

  it("never marks the server's recap stale: it holds until the session runs again", () => {
    renderLine({ "status.line": "Fixed the bug", "status.at": at(3600), "status.by": "recap" });
    const line = screen.getByTestId("line");
    expect(line).toHaveTextContent(/^Fixed the bug · 1h$/);
    expect(line).not.toHaveAttribute("data-stale");
  });

  it("shows the line with no age without status.at", () => {
    renderLine({ "status.line": "waits on CI" });
    expect(screen.getByTestId("line")).toHaveTextContent(/^waits on CI$/);
  });

  it("renders nothing for a blank line", () => {
    renderLine({ "status.line": "  ", "status.at": at(5) });
    expect(screen.queryByTestId("line")).toBeNull();
  });
});
