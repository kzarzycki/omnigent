import { describe, expect, it } from "vitest";

import { hostBuildLabel } from "./HostLabel";

describe("hostBuildLabel", () => {
  const host = { host_id: "h", name: "n", owner: "o", status: "online" as const };

  it("shows version and short commit, version alone, or nothing", () => {
    expect(hostBuildLabel({ ...host, version: "0.19.0", commit: "3609fea2a" })).toBe(
      "v0.19.0 · 3609fea",
    );
    expect(hostBuildLabel({ ...host, version: "0.19.0", commit: null })).toBe("v0.19.0");
    expect(hostBuildLabel(host)).toBeNull();
  });
});
