import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_KEEP_THINKING_EXPANDED,
  readKeepThinkingExpanded,
  writeKeepThinkingExpanded,
} from "./keepThinkingExpandedPreferences";

const STORAGE_KEY = "omnigent:keep-thinking-expanded";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("keepThinkingExpandedPreferences", () => {
  it("defaults to off when nothing is stored", () => {
    expect(DEFAULT_KEEP_THINKING_EXPANDED).toBe(false);
    expect(readKeepThinkingExpanded()).toBe(false);
  });

  it("reads the stored strings 'true' and 'false'", () => {
    localStorage.setItem(STORAGE_KEY, "true");
    expect(readKeepThinkingExpanded()).toBe(true);

    localStorage.setItem(STORAGE_KEY, "false");
    expect(readKeepThinkingExpanded()).toBe(false);
  });

  it("reads any other stored value as off", () => {
    for (const raw of ["1", "yes", "TRUE", " true", ""]) {
      localStorage.setItem(STORAGE_KEY, raw);
      expect(readKeepThinkingExpanded()).toBe(false);
    }
  });

  it("round-trips both boolean values", () => {
    writeKeepThinkingExpanded(true);
    expect(readKeepThinkingExpanded()).toBe(true);

    writeKeepThinkingExpanded(false);
    expect(readKeepThinkingExpanded()).toBe(false);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeKeepThinkingExpanded(true)).not.toThrow();
    expect(readKeepThinkingExpanded()).toBe(false);
  });
});
