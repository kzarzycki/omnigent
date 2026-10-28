import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { HostUpdateBanner, useOutdatedHost } from "./HostUpdateBanner";

const state = vi.hoisted(() => ({
  hosts: [] as Host[],
  mutate: vi.fn(),
  isPending: false,
  error: null as Error | null,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: state.hosts }),
  useUpdateHost: () => ({ mutate: state.mutate, isPending: state.isPending, error: state.error }),
}));
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
  state.mutate.mockReset();
  state.isPending = false;
  state.error = null;
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

  describe("with an updater on the host", () => {
    const update = { current: "1a2b3c4d5", target: "c25ddceb8", command: "update-host.sh" };

    it("offers Update now instead of the command, and asks the host on click", () => {
      state.hosts = [host({ update: { ...update, can_update: true, run: null } })];
      render(<Banner />);
      expect(screen.queryByRole("button", { name: "Copy update-host.sh" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Update now" }));
      expect(state.mutate).toHaveBeenCalledOnce();
    });

    it("shows progress while the updater runs, with no button", () => {
      state.hosts = [
        host({ update: { ...update, can_update: true, run: { state: "running", output: "" } } }),
      ];
      render(<Banner />);
      expect(screen.getByText(/Updating zarz-mbp/)).toBeTruthy();
      expect(screen.queryByRole("button", { name: /Update now|Try again/ })).toBeNull();
    });

    it.each([
      ["failed", "The update failed.", "not signed by a key in allowed_signers"],
      [
        "finished",
        "The updater finished. zarz-mbp runs 1a2b3c4d5 until it restarts.",
        "deferred: sessions mid-turn on this host",
      ],
    ] as const)(
      "after a %s run shows the outcome, the output and Try again",
      (runState, summary, output) => {
        state.hosts = [
          host({ update: { ...update, can_update: true, run: { state: runState, output } } }),
        ];
        render(<Banner />);
        expect(screen.getByText(summary)).toBeTruthy();
        expect(screen.getByText(output)).toBeTruthy();
        expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
      },
    );

    it("shows why the server refused the request", () => {
      state.hosts = [host({ update: { ...update, can_update: true, run: null } })];
      state.error = new Error("host 'zarz-mbp' is already updating");
      render(<Banner />);
      expect(screen.getByText("host 'zarz-mbp' is already updating")).toBeTruthy();
    });
  });
});
