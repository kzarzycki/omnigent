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

/**
 * Wait for the session's row to render (sections may be expanding / a project
 * folder fetching), then scroll it to the center and flash it. Returns a cancel fn.
 */
export function scrollToAndFlashSidebarRow(
  container: HTMLElement | null | undefined,
  sessionId: string,
): () => void {
  let frame = 0;
  let flashTimer: ReturnType<typeof setTimeout> | undefined;
  const deadline = performance.now() + FIND_TIMEOUT_MS;
  const selector = `[data-sidebar-session-id="${CSS.escape(sessionId)}"]`;
  const tick = () => {
    const row = (container ?? document).querySelector<HTMLElement>(selector);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.remove(SIDEBAR_REVEAL_FLASH_CLASS);
      // Force a reflow so a repeat reveal restarts the animation.
      void row.offsetWidth;
      row.classList.add(SIDEBAR_REVEAL_FLASH_CLASS);
      flashTimer = setTimeout(() => row.classList.remove(SIDEBAR_REVEAL_FLASH_CLASS), FLASH_MS);
      return;
    }
    if (performance.now() < deadline) frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return () => {
    cancelAnimationFrame(frame);
    if (flashTimer) clearTimeout(flashTimer);
  };
}
