import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  clearSidebarRevealRequest,
  requestRevealSessionInSidebar,
  SIDEBAR_REVEAL_FLASH_CLASS,
  scrollToAndFlashSidebarRow,
  useSidebarRevealRequest,
} from "./sidebarReveal";

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

  it("re-centres a row that moved during the scroll before flashing it", () => {
    vi.useFakeTimers();
    const { container, row } = setup(900);
    Object.defineProperty(container, "scrollHeight", { value: 2000 });
    Object.defineProperty(container, "clientHeight", { value: 500 });
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    // A page loaded above the row mid-scroll: it lands below the centre.
    row.getBoundingClientRect = () => ({ top: 420, bottom: 448 }) as DOMRect;
    container.dispatchEvent(new Event("scrollend"));
    expect(row.scrollIntoView).toHaveBeenCalledTimes(2);
    expect(row).not.toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
    row.getBoundingClientRect = () => ({ top: 236, bottom: 264 }) as DOMRect;
    container.dispatchEvent(new Event("scrollend"));
    expect(row).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
  });

  it("flashes after the third scroll when the row never settles at the centre", () => {
    vi.useFakeTimers();
    const { container, row } = setup(900);
    Object.defineProperty(container, "scrollHeight", { value: 2000 });
    Object.defineProperty(container, "clientHeight", { value: 500 });
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    // Rows keep loading above it, so every scroll lands below the centre.
    row.getBoundingClientRect = () => ({ top: 420, bottom: 448 }) as DOMRect;
    container.dispatchEvent(new Event("scrollend"));
    container.dispatchEvent(new Event("scrollend"));
    expect(row.scrollIntoView).toHaveBeenCalledTimes(3);
    expect(row).not.toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
    container.dispatchEvent(new Event("scrollend"));
    expect(row.scrollIntoView).toHaveBeenCalledTimes(3);
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

  it("flashes only once when both scrollend and the fallback timer fire", () => {
    vi.useFakeTimers();
    const { container, row } = setup(900);
    const add = vi.spyOn(row.classList, "add");
    scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    container.dispatchEvent(new Event("scrollend"));
    vi.advanceTimersByTime(800);
    expect(add.mock.calls.filter(([c]) => c === SIDEBAR_REVEAL_FLASH_CLASS)).toHaveLength(1);
  });

  it("clears the ring when a newer reveal cancels this one mid-flash", () => {
    vi.useFakeTimers();
    const { container, row } = setup(100);
    const cancel = scrollToAndFlashSidebarRow(container, "conv_1");
    vi.advanceTimersToNextFrame();
    expect(row).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
    cancel();
    expect(row).not.toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS);
  });
});

describe("sidebar reveal request", () => {
  it("keeps a newer request when an older one is cleared", () => {
    requestRevealSessionInSidebar("conv_old");
    const { result } = renderHook(() => useSidebarRevealRequest());
    const older = result.current!;
    requestRevealSessionInSidebar("conv_new");
    clearSidebarRevealRequest(older.nonce);
    expect(renderHook(() => useSidebarRevealRequest()).result.current).toMatchObject({
      sessionId: "conv_new",
    });
    clearSidebarRevealRequest(older.nonce + 1);
  });
});
