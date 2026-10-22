import { act, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatFindBar } from "./ChatFindBar";

const searchSessionItems = vi.hoisted(() => vi.fn());
vi.mock("@/lib/sessionsApi", () => ({ searchSessionItems }));

const matches = [
  { id: "item_a", response_id: "resp_a" },
  { id: "item_b", response_id: "resp_b" },
  { id: "item_c", response_id: "resp_c" },
];

async function renderBar(start: "newest" | "oldest") {
  const onTarget = vi.fn();
  render(
    <ChatFindBar
      conversationId="conv_1"
      query="zorblat"
      onQueryChange={() => {}}
      start={start}
      inputRef={createRef<HTMLInputElement>()}
      onTarget={onTarget}
      onClose={() => {}}
    />,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300);
  });
  return onTarget;
}

describe("ChatFindBar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    searchSessionItems.mockResolvedValue({ matches, hasMore: false });
  });
  afterEach(() => vi.useRealTimers());

  it("starts ⌘F at the newest match and steps through every match with the counter", async () => {
    const onTarget = await renderBar("newest");
    expect(searchSessionItems).toHaveBeenCalledWith("conv_1", "zorblat", expect.anything());
    expect(screen.getByText("3 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_c", "resp_c"] }),
    );

    const input = screen.getByRole("textbox", { name: "Find in chat" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("1 / 3")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(screen.getByText("3 / 3")).toBeTruthy();
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_b", "resp_b"] }),
    );
  });

  it("starts a search result at the oldest match", async () => {
    const onTarget = await renderBar("oldest");
    expect(screen.getByText("1 / 3")).toBeTruthy();
    expect(onTarget).toHaveBeenLastCalledWith(
      expect.objectContaining({ candidates: ["item_a", "resp_a"] }),
    );
  });
});
