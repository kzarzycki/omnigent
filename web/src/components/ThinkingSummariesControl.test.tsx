import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { ThinkingSummariesControl } from "./ThinkingSummariesControl";
import { getThinkingSummaries, saveThinkingSummaries } from "@/lib/thinkingSummariesApi";

vi.mock("@/lib/thinkingSummariesApi", () => ({
  getThinkingSummaries: vi.fn(),
  saveThinkingSummaries: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const getMock = vi.mocked(getThinkingSummaries);
const saveMock = vi.mocked(saveThinkingSummaries);

async function loadedToggle(): Promise<HTMLElement> {
  const toggle = await screen.findByTestId("thinking-summaries-toggle");
  await waitFor(() => expect(toggle).toBeEnabled());
  return toggle;
}

beforeEach(() => {
  getMock.mockResolvedValue({ enabled: false });
  saveMock.mockImplementation(async (enabled) => ({ enabled }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ThinkingSummariesControl", () => {
  it.each([true, false])("shows the stored value (%s)", async (stored) => {
    getMock.mockResolvedValue({ enabled: stored });
    render(<ThinkingSummariesControl />);

    const toggle = await loadedToggle();

    expect(toggle).toHaveAccessibleName("Ask for thinking summaries");
    expect(toggle).toHaveAttribute("aria-checked", String(stored));
  });

  it("writes each toggle to the server", async () => {
    render(<ThinkingSummariesControl />);
    const toggle = await loadedToggle();

    fireEvent.click(toggle);
    await waitFor(() => expect(saveMock).toHaveBeenCalledWith(true));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));

    fireEvent.click(await loadedToggle());
    await waitFor(() => expect(saveMock).toHaveBeenLastCalledWith(false));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
  });

  it("keeps the stored value and reports a failed save", async () => {
    saveMock.mockRejectedValue(new Error("server unavailable"));
    render(<ThinkingSummariesControl />);
    const toggle = await loadedToggle();

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Couldn't save thinking summaries: server unavailable",
      ),
    );
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("disables the switch after a failed load and retries", async () => {
    getMock.mockRejectedValueOnce(new Error("offline"));
    render(<ThinkingSummariesControl />);

    expect(await screen.findByTestId("thinking-summaries-status")).toHaveTextContent(
      "Couldn't load your thinking summaries setting.",
    );
    expect(screen.getByTestId("thinking-summaries-toggle")).toBeDisabled();

    getMock.mockResolvedValue({ enabled: true });
    fireEvent.click(screen.getByTestId("thinking-summaries-retry"));

    expect(await loadedToggle()).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByTestId("thinking-summaries-status")).toBeNull();
  });
});
