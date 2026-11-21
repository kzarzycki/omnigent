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
});
