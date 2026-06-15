import { afterEach, describe, expect, it } from "vitest";
import { readPinnedMessage, writePinnedMessage } from "./pinnedMessages";

const STORAGE_KEY = "omnigent:pinned-messages";
// Mirrors MAX_SESSIONS in the source; the pruning test seeds one past the cap.
const MAX_SESSIONS = 100;
// Mirrors MAX_SNIPPET_CHARS in the source.
const MAX_SNIPPET_CHARS = 300;

afterEach(() => {
  localStorage.clear();
});

describe("pinnedMessages", () => {
  it("returns null for a session with no pin", () => {
    // No stored entry must read as "nothing pinned", not an error or a
    // leftover value from another session.
    expect(readPinnedMessage("conv_unknown")).toBeNull();
  });

  it("round-trips a pin through localStorage", () => {
    writePinnedMessage("conv_a", { itemId: "item_1", text: "review PR #123" });

    // Both fields must survive serialization — itemId drives the jump
    // anchor, text drives the banner. Losing either breaks the feature.
    expect(readPinnedMessage("conv_a")).toEqual({ itemId: "item_1", text: "review PR #123" });
  });

  it("replaces the previous pin — one pin per session", () => {
    writePinnedMessage("conv_a", { itemId: "item_1", text: "old" });
    writePinnedMessage("conv_a", { itemId: "item_2", text: "new" });

    // The second write must fully replace the first. Reading item_1 back
    // would mean the store appends instead of replacing, and the banner
    // could resurrect a stale pin.
    expect(readPinnedMessage("conv_a")).toEqual({ itemId: "item_2", text: "new" });
  });

  it("clears the pin when writing null", () => {
    writePinnedMessage("conv_a", { itemId: "item_1", text: "pinned" });
    writePinnedMessage("conv_a", null);

    // Unpin must remove the entry entirely; a surviving pin means the ✕
    // button would not stick across reloads.
    expect(readPinnedMessage("conv_a")).toBeNull();
  });

  it("keeps sessions isolated by id", () => {
    writePinnedMessage("conv_a", { itemId: "item_a", text: "a" });
    writePinnedMessage("conv_b", { itemId: "item_b", text: "b" });

    // Pinning in one session must not bleed into another — a failure means
    // entries are keyed together and every session would show the same pin.
    expect(readPinnedMessage("conv_a")).toEqual({ itemId: "item_a", text: "a" });
    expect(readPinnedMessage("conv_b")).toEqual({ itemId: "item_b", text: "b" });
  });

  it("truncates the stored snippet to the cap", () => {
    const long = "x".repeat(MAX_SNIPPET_CHARS + 50);
    writePinnedMessage("conv_a", { itemId: "item_1", text: long });

    // The banner shows one truncated line; storing more than the cap just
    // bloats localStorage. A full-length read-back means the cap is dead code.
    expect(readPinnedMessage("conv_a")?.text).toHaveLength(MAX_SNIPPET_CHARS);
  });

  it("prunes the oldest-pinned session past the cap", () => {
    for (let i = 0; i < MAX_SESSIONS + 1; i++) {
      writePinnedMessage(`conv_${i}`, { itemId: `item_${i}`, text: `t${i}` });
    }

    // conv_0 was pinned first, so it is evicted when conv_100 pushes the
    // store past the cap; the newest pin must survive. Reading conv_0 back
    // means pruning didn't run (unbounded growth); losing conv_100 means
    // the wrong end was trimmed.
    expect(readPinnedMessage("conv_0")).toBeNull();
    expect(readPinnedMessage(`conv_${MAX_SESSIONS}`)).toEqual({
      itemId: `item_${MAX_SESSIONS}`,
      text: `t${MAX_SESSIONS}`,
    });
  });

  it("survives corrupted storage and keeps working", () => {
    localStorage.setItem(STORAGE_KEY, "{not json");

    // Corrupt storage must read as "no pin" (not throw) ...
    expect(readPinnedMessage("conv_a")).toBeNull();

    // ... and a subsequent write must recover by replacing the bad blob.
    writePinnedMessage("conv_a", { itemId: "item_1", text: "recovered" });
    expect(readPinnedMessage("conv_a")).toEqual({ itemId: "item_1", text: "recovered" });
  });

  it("drops malformed entries instead of returning them", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        { id: "conv_bad", pin: { itemId: "", text: "empty id" } },
        { id: "conv_ok", pin: { itemId: "item_1", text: "fine" } },
      ]),
    );

    // An empty itemId can't anchor a jump, so the sanitizer must drop the
    // entry; returning it would render a banner whose click never works.
    expect(readPinnedMessage("conv_bad")).toBeNull();
    expect(readPinnedMessage("conv_ok")).toEqual({ itemId: "item_1", text: "fine" });
  });
});
