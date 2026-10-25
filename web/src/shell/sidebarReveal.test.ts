import { afterEach, describe, expect, it, vi } from "vitest";
import { SIDEBAR_REVEAL_FLASH_CLASS, scrollToAndFlashSidebarRow } from "./sidebarReveal";

function setup(rowTop: number) {
  const container = document.createElement("nav");
  const row = document.createElement("li");
  row.dataset.sidebarSessionId = "conv_1";
  container.append(row);
  document.body.append(container);
  container.getBoundingClientRect = () => ({ top: 0, bottom: 500 }) as DOMRect;
  row.getBoundingClientRect = () => ({ top: rowTop, bottom: rowTop + 28 }) as DOMRect;
  row.scrollIntoView = vi.fn();
  return { container, row };
}

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("scrollToAndFlashSidebarRow", () => {
  it("flashes a visible row at once without scrolling", () => {
    vi.useFakeTimers();
    const { container, row } = setup(100);
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    expect(row.scrollIntoView).not.toHaveBeenCalled();
    expect(row).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
  });

  it("scrolls an off-screen row and flashes it once the scroll ends", () => {
    vi.useFakeTimers();
    const { container, row } = setup(900);
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    expect(row.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(row).not.toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
    container.dispatchEvent(new Event("scrollend"));
    expect(row).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
  });

  it("falls back to a timer when no scrollend arrives", () => {
    vi.useFakeTimers();
    const { container, row } = setup(900);
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    vi.advanceTimersByTime(800);
    expect(row).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
  });
});
