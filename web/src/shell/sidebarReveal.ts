import { useSyncExternalStore } from "react";

/**
 * A one-shot "reveal this session in the sidebar" request. Held in module
 * state (not an event) so a sidebar that mounts only after being opened still
 * picks it up. `nonce` makes repeat requests for the same session distinct.
 */
export interface SidebarRevealRequest {
  sessionId: string;
  nonce: number;
}

let pending: SidebarRevealRequest | null = null;
let nonce = 0;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Ask the sidebar to open, expand the session's section, scroll to and flash its row. */
export function requestRevealSessionInSidebar(sessionId: string) {
  nonce += 1;
  pending = { sessionId, nonce };
  emit();
}

/** Clear a handled request; ignores a stale nonce so a newer request survives. */
export function clearSidebarRevealRequest(handledNonce: number) {
  if (pending?.nonce !== handledNonce) return;
  pending = null;
  emit();
}

export function useSidebarRevealRequest(): SidebarRevealRequest | null {
  return useSyncExternalStore(
    subscribe,
    () => pending,
    () => null,
  );
}

/** CSS class that briefly outlines a revealed row (see index.css). */
export const SIDEBAR_REVEAL_FLASH_CLASS = "animate-sidebar-reveal";
const FLASH_MS = 1600;
const FIND_TIMEOUT_MS = 2000;
const SCROLL_FALLBACK_MS = 800;
// Rows loading mid-scroll can push the target off its scroll target; re-centre a few times.
const MAX_SCROLL_ATTEMPTS = 3;
const CENTRE_TOLERANCE_PX = 8;

/** How far the scroller must still move to centre the row, clamped to its scroll range. */
function remainingScrollToCentre(row: HTMLElement, container: HTMLElement | null | undefined) {
  const scroller = container ?? document.scrollingElement;
  if (!scroller) return 0;
  const rect = row.getBoundingClientRect();
  const bounds = container?.getBoundingClientRect() ?? { top: 0, bottom: window.innerHeight };
  const offset = (rect.top + rect.bottom) / 2 - (bounds.top + bounds.bottom) / 2;
  const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const target = Math.min(maxScroll, Math.max(0, scroller.scrollTop + offset));
  return target - scroller.scrollTop;
}

function isFullyVisible(row: HTMLElement, container: HTMLElement | null | undefined): boolean {
  const rect = row.getBoundingClientRect();
  const bounds = container?.getBoundingClientRect() ?? {
    top: 0,
    bottom: window.innerHeight,
  };
  return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
}

/**
 * Wait for the session's row to render (sections may be expanding / a project
 * folder fetching), then scroll it to the center and flash it. Returns a cancel fn.
 */
export function scrollToAndFlashSidebarRow(
  container: HTMLElement | null | undefined,
  sessionId: string,
): () => void {
  let frame = 0;
  const timers: ReturnType<typeof setTimeout>[] = [];
  let stopWaitingForScroll = () => {};
  const deadline = performance.now() + FIND_TIMEOUT_MS;
  const selector = `[data-sidebar-session-id="${CSS.escape(sessionId)}"]`;
  let flashedRow: HTMLElement | null = null;
  const flash = (row: HTMLElement) => {
    if (flashedRow) return;
    flashedRow = row;
    stopWaitingForScroll();
    row.classList.remove(SIDEBAR_REVEAL_FLASH_CLASS);
    // Force a reflow so a repeat reveal restarts the animation.
    void row.offsetWidth;
    row.classList.add(SIDEBAR_REVEAL_FLASH_CLASS);
    timers.push(setTimeout(() => row.classList.remove(SIDEBAR_REVEAL_FLASH_CLASS), FLASH_MS));
  };
  const findRow = () => (container ?? document).querySelector<HTMLElement>(selector);
  // Flash once the smooth scroll lands, so the pulse isn't spent mid-scroll; the
  // timer covers a browser without `scrollend`. Re-centre if the row moved meanwhile.
  const scrollAndFlash = (row: HTMLElement, attemptsLeft: number) => {
    const scroller = container ?? document;
    let landed = false;
    const onLanded = () => {
      if (landed) return;
      landed = true;
      stopWaitingForScroll();
      const current = findRow() ?? row;
      const remaining = Math.abs(remainingScrollToCentre(current, container));
      if (remaining > CENTRE_TOLERANCE_PX && attemptsLeft > 1) {
        scrollAndFlash(current, attemptsLeft - 1);
      } else {
        flash(current);
      }
    };
    scroller.addEventListener("scrollend", onLanded);
    const timer = setTimeout(onLanded, SCROLL_FALLBACK_MS);
    timers.push(timer);
    stopWaitingForScroll = () => {
      scroller.removeEventListener("scrollend", onLanded);
      clearTimeout(timer);
    };
    row.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  const tick = () => {
    const row = findRow();
    if (!row) {
      if (performance.now() < deadline) frame = requestAnimationFrame(tick);
      return;
    }
    if (isFullyVisible(row, container)) {
      flash(row);
      return;
    }
    scrollAndFlash(row, MAX_SCROLL_ATTEMPTS);
  };
  frame = requestAnimationFrame(tick);
  return () => {
    cancelAnimationFrame(frame);
    stopWaitingForScroll();
    for (const timer of timers) clearTimeout(timer);
    // A newer reveal cancels this one; its timer would have removed the ring.
    flashedRow?.classList.remove(SIDEBAR_REVEAL_FLASH_CLASS);
  };
}
