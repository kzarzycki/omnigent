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
    update: { current: "1a2b3c4d5", target: "c25ddceb8", command: "update-host.sh" },
    ...overrides,
  };
}

function Banner({ hostId = "laptop" }: { hostId?: string }) {
  const { outdated, dismissToday } = useOutdatedHost(hostId);
  return outdated ? (
    <HostUpdateBanner outdated={outdated} onDismiss={() => dismissToday(outdated)} />
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
  it("names the machine, both builds and the server's update command", () => {
    render(<Banner />);
    expect(screen.getByText("Omnigent c25ddceb8 is available for zarz-mbp.")).toBeTruthy();
    expect(screen.getByText(/It runs 1a2b3c4d5/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy update-host.sh" })).toBeTruthy();
  });

  it("shows no command when the deploy sets none", () => {
    state.hosts = [host({ update: { current: "1a2b3c4d5", target: "c25ddceb8", command: null } })];
    render(<Banner />);
    expect(screen.getByText(/Update zarz-mbp to get the latest fixes/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Copy / })).toBeNull();
  });

  it.each([
    ["is current", host({ update: null })],
    ["is offline", host({ status: "offline" })],
    ["is another host", host({ host_id: "other" })],
  ])("stays hidden when the session's host %s", (_label, listed) => {
    state.hosts = [listed];
    render(<Banner />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("'Not now' hides it for the day and for that target only", () => {
    const { rerender } = render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("status")).toBeNull();

    state.hosts = [host({ update: { current: "1a2b3c4d5", target: "d00dfeed1", command: "x" } })];
    rerender(<Banner />);
    expect(screen.getByText("Omnigent d00dfeed1 is available for zarz-mbp.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    vi.setSystemTime(new Date(2026, 9, 28, 10, 0));
    rerender(<Banner />);
    expect(screen.getByRole("status")).toBeTruthy();
  });
});
