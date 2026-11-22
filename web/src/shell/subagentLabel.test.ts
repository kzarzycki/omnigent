import { describe, expect, it } from "vitest";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";

import { childPrimaryLabel } from "./subagentLabel";

function child(overrides: Partial<ChildSessionInfo>): ChildSessionInfo {
  return {
    id: "conv_child",
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

describe("childPrimaryLabel", () => {
  it("names an ACP agent's sub-agent by its title, not its call id", () => {
    const row = child({
      title: "find callers",
      tool: "find callers",
      session_name: "toolu_01abc",
      labels: { "omnigent.acp.subagent_id": "toolu_01abc" },
    });

    expect(childPrimaryLabel(row)).toBe("find callers");
  });

  it("keeps the instance name for an orchestrated child", () => {
    const row = child({ title: "researcher:auth", tool: "researcher", session_name: "auth" });

    expect(childPrimaryLabel(row)).toBe("auth");
  });
});
