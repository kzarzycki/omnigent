import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { ModelEffortFields as ModelEffortFieldsType } from "./ModelEffortFields";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

/** Harness settings offering the Claude aliases and the full effort ladder. */
const SETTINGS = {
  harness: "claude-native",
  supportsModel: true,
  models: [
    { id: "opus", displayName: "Opus" },
    { id: "sonnet", displayName: "Sonnet" },
  ],
  efforts: [
    { value: "high", label: "High" },
    { value: "max", label: "Max" },
  ],
  effortsForModel: () => [],
  permissions: [],
  catalogMessage: null,
  blockedReason: null,
} as unknown as ComponentProps<typeof ModelEffortFieldsType>["settings"];

async function renderWithBlocked(info: Record<string, string[]>) {
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => info })),
  );
  const { resolveServerInfo } = await import("@/lib/capabilities");
  await resolveServerInfo();
  const { ModelEffortFields } = await import("./ModelEffortFields");
  render(
    <ModelEffortFields
      model=""
      effort=""
      permissionMode=""
      settings={SETTINGS}
      onModelChange={() => {}}
      onEffortChange={() => {}}
      onPermissionModeChange={() => {}}
    />,
  );
}

async function optionNames(trigger: string): Promise<string[]> {
  fireEvent.keyDown(screen.getByTestId(trigger), { key: "Enter" });
  const options = await screen.findAllByRole("option");
  const names = options.map((option) => option.textContent ?? "");
  fireEvent.keyDown(options[0], { key: "Escape" });
  return names;
}

describe("ModelEffortFields with a server block list", () => {
  it("leaves blocked models out of the model list", async () => {
    await renderWithBlocked({ blocked_models: ["opus"] });
    const names = await optionNames("task-model-trigger");
    expect(names).not.toContain("Opus");
    expect(names).toContain("Sonnet");
  });

  it("leaves blocked efforts out of the effort list", async () => {
    await renderWithBlocked({ blocked_efforts: ["max"] });
    const names = await optionNames("task-effort-trigger");
    expect(names).not.toContain("Max");
    expect(names).toContain("High");
  });

  it("offers every built-in entry when nothing is blocked", async () => {
    await renderWithBlocked({});
    expect(await optionNames("task-model-trigger")).toContain("Opus");
  });
});
