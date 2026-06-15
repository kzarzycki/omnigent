import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PinnedMessageBanner } from "./PinnedMessageBanner";

afterEach(cleanup);

describe("PinnedMessageBanner", () => {
  it("renders the pinned snippet text", () => {
    render(
      <PinnedMessageBanner
        text="Review https://github.com/org/repo/pull/42"
        onJump={() => {}}
        onUnpin={() => {}}
      />,
    );

    // The snippet is the whole point of the banner — it's what tells the
    // user which message this session is anchored to.
    expect(screen.getByTestId("pinned-message-banner")).toHaveTextContent(
      "Review https://github.com/org/repo/pull/42",
    );
  });

  it("fires onJump (not onUnpin) when the banner body is clicked", () => {
    const onJump = vi.fn();
    const onUnpin = vi.fn();
    render(<PinnedMessageBanner text="pinned" onJump={onJump} onUnpin={onUnpin} />);

    fireEvent.click(screen.getByTestId("pinned-message-jump"));

    // Body click must scroll to the message; accidentally unpinning here
    // would destroy the bookmark the user just tried to use.
    expect(onJump).toHaveBeenCalledTimes(1);
    expect(onUnpin).not.toHaveBeenCalled();
  });

  it("fires onUnpin (not onJump) when the ✕ button is clicked", () => {
    const onJump = vi.fn();
    const onUnpin = vi.fn();
    render(<PinnedMessageBanner text="pinned" onJump={onJump} onUnpin={onUnpin} />);

    fireEvent.click(screen.getByRole("button", { name: "Unpin message" }));

    // The ✕ must only remove the pin; a stray onJump would yank the scroll
    // position out from under the user mid-unpin.
    expect(onUnpin).toHaveBeenCalledTimes(1);
    expect(onJump).not.toHaveBeenCalled();
  });
});
