// Per-session "pinned message" persistence, keyed by conversationId. A pin
// is a personal bookmark: one user message per session whose snippet renders
// in a banner stuck to the top of the chat viewport (PinnedMessageBanner),
// so "what is this session doing" survives an hour of scrollback.
//
// localStorage (not server state) on purpose — a pin is a per-browser
// reading aid, not shared session content other participants should see.

export interface PinnedMessage {
  /**
   * Canonical item id of the pinned user message — matches the bubble's
   * `data-user-message-id` DOM anchor used for jump-to-message.
   */
  itemId: string;
  /**
   * Text snippet shown in the banner, captured at pin time so the banner
   * renders even when the message's history page isn't loaded yet.
   * Truncated to MAX_SNIPPET_CHARS on write.
   */
  text: string;
}

const STORAGE_KEY = "omnigent:pinned-messages";
// Cap stored sessions so the store can't grow without bound. The
// least-recently-pinned entries (front of the array) are pruned first once
// the cap is exceeded.
const MAX_SESSIONS = 100;
// The banner shows a single truncated line; storing more text than fits a
// wide banner just bloats localStorage.
const MAX_SNIPPET_CHARS = 300;

/**
 * One persisted session entry. The store is an ordered array (not a keyed
 * object) so recency ordering survives serialization regardless of the id
 * format: a plain `Record` reorders integer-like keys into ascending numeric
 * order, which would break the oldest-first pruning.
 */
interface StoredEntry {
  id: string;
  pin: PinnedMessage;
}

type Store = StoredEntry[];

function sanitize(entry: unknown): PinnedMessage | null {
  if (typeof entry !== "object" || entry === null) return null;
  const record = entry as Record<string, unknown>;
  if (typeof record.itemId !== "string" || record.itemId.length === 0) return null;
  if (typeof record.text !== "string") return null;
  return { itemId: record.itemId, text: record.text };
}

function readStore(): Store {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const store: Store = [];
    for (const item of parsed) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.id !== "string") continue;
      const pin = sanitize(record.pin);
      if (pin) store.push({ id: record.id, pin });
    }
    return store;
  } catch {
    return [];
  }
}

function writeStore(store: Store): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // Storage quota/access errors must not break the chat surface — the
    // pin still works in-memory for the current page lifetime.
  }
}

/** Read one session's pinned message, or null when nothing is pinned. */
export function readPinnedMessage(conversationId: string): PinnedMessage | null {
  const entry = readStore().find((e) => e.id === conversationId);
  return entry ? entry.pin : null;
}

/**
 * Persist (or clear, with `null`) one session's pinned message. Writing a
 * pin replaces any previous pin for the session — one pin per session.
 */
export function writePinnedMessage(conversationId: string, pin: PinnedMessage | null): void {
  const store = readStore();
  const existingIdx = store.findIndex((e) => e.id === conversationId);
  if (existingIdx >= 0) store.splice(existingIdx, 1);
  if (pin) {
    // Drop-then-append so the most-recently-pinned session moves to the
    // end; pruning then evicts from the front (oldest-pinned).
    store.push({
      id: conversationId,
      pin: { itemId: pin.itemId, text: pin.text.slice(0, MAX_SNIPPET_CHARS) },
    });
    if (store.length > MAX_SESSIONS) {
      store.splice(0, store.length - MAX_SESSIONS);
    }
  }
  writeStore(store);
}
