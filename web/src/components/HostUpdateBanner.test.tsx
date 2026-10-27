import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { HostUpdateBanner, useOutdatedHost } from "./HostUpdateBanner";

const state = vi.hoisted(() => ({ hosts: [] as Host[] }));

vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: state.hosts }) }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "laptop",
    name: "zarz-mbp",
    owner: "me",
    status: "online",
    version: "0.18.2",
    update_version: "0.19.0",
    ...overrides,
  };
}

function Banner({ hostId = "laptop" }: { hostId?: string }) {
  const update = useOutdatedHost(hostId);
  return update.host ? (
    <HostUpdateBanner host={update.host} onDismiss={() => update.dismissToday(update.host!)} />
  ) : null;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 27, 10, 0));
  localStorage.clear();
  state.hosts = [host()];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("HostUpdateBanner", () => {
  it("names the machine, both versions and the upgrade command", () => {
    render(<Banner />);
    expect(screen.getByText("Omnigent 0.19.0 is available for zarz-mbp.")).toBeTruthy();
    expect(screen.getByText(/It runs 0\.18\.2/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy omni upgrade" })).toBeTruthy();
  });

  it.each([
    ["is current", host({ update_version: null })],
    ["is offline", host({ status: "offline" })],
    ["is another host", host({ host_id: "other" })],
  ])("stays hidden when the session's host %s", (_label, listed) => {
    state.hosts = [listed];
    render(<Banner />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("'Not now' hides it for the day and for that version only", () => {
    const { rerender } = render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("status")).toBeNull();

    state.hosts = [host({ update_version: "0.20.0" })];
    rerender(<Banner />);
    expect(screen.getByText("Omnigent 0.20.0 is available for zarz-mbp.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    vi.setSystemTime(new Date(2026, 9, 28, 10, 0));
    rerender(<Banner />);
    expect(screen.getByRole("status")).toBeTruthy();
  });
});
