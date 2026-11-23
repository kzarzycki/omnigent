// Per-device preference: keep thinking sections that have text open after the
// model finishes, and after a reload. Off by default, so the chat folds thinking
// once it settles unless the reader opts in.

const STORAGE_KEY = "omnigent:keep-thinking-expanded";

export const DEFAULT_KEEP_THINKING_EXPANDED = false;

/** Read the stored preference; only the exact string "true" turns it on. Never throws. */
export function readKeepThinkingExpanded(): boolean {
  if (typeof window === "undefined") return DEFAULT_KEEP_THINKING_EXPANDED;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return DEFAULT_KEEP_THINKING_EXPANDED;
  }
}

/** Persist the preference, swallowing quota/access errors. */
export function writeKeepThinkingExpanded(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
