import { describe, expect, it } from "vitest";

import { splitSessionMessageOrigin } from "./sessionMessageOrigin";

describe("splitSessionMessageOrigin", () => {
  it("splits the sender id off a sys_session_message delivery", () => {
    expect(
      splitSessionMessageOrigin(
        "[Message from session 9b8b26c8edfd40d781c0d13a04fa5c0b]\nhi\nthere",
      ),
    ).toEqual({ sourceId: "9b8b26c8edfd40d781c0d13a04fa5c0b", label: null, body: "hi\nthere" });
  });

  it("ignores the prefix anywhere but the start, or without its newline", () => {
    expect(splitSessionMessageOrigin("hi [Message from session abc]\nthere")).toBeNull();
    expect(splitSessionMessageOrigin("[Message from session abc] hi")).toBeNull();
    expect(splitSessionMessageOrigin("plain text")).toBeNull();
  });

  it("splits a sessionless sender's name off a named delivery", () => {
    expect(splitSessionMessageOrigin("[Message from Supervisor]\nact: b-1\nmore")).toEqual({
      sourceId: null,
      label: "Supervisor",
      body: "act: b-1\nmore",
    });
    expect(splitSessionMessageOrigin("[Message from session abc]\nhi")?.sourceId).toBe("abc");
    expect(splitSessionMessageOrigin("[Message from session abc def]\nhi")).toBeNull();
    expect(splitSessionMessageOrigin("[Message from ]\nhi")).toBeNull();
  });

  it("trims a padded label and rejects a blank one", () => {
    expect(splitSessionMessageOrigin("[Message from  Supervisor ]\nx")?.label).toBe("Supervisor");
    expect(splitSessionMessageOrigin("[Message from   ]\nx")).toBeNull();
  });

  it("bounds a named label to 1-64 characters without brackets or newlines", () => {
    const body = "\nx";
    expect(splitSessionMessageOrigin(`[Message from ${"a"}]${body}`)?.label).toBe("a");
    expect(
      splitSessionMessageOrigin(`[Message from ${"a".repeat(64)}]${body}`)?.label,
    ).toHaveLength(64);
    for (const label of ["a".repeat(65), "a[b", "a]b", "a\nb", "a\rb"]) {
      expect(splitSessionMessageOrigin(`[Message from ${label}]${body}`)).toBeNull();
    }
  });
});
