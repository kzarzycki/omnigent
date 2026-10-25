import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// "Show in sidebar" expands the sections hiding the current session, then
// scrolls to and flashes its row.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import {
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
} from "@/shell/sidebarNav";
import { requestRevealSessionInSidebar, SIDEBAR_REVEAL_FLASH_CLASS } from "./sidebarReveal";

const { pinnedRef } = vi.hoisted(() => ({ pinnedRef: { current: [] as unknown[] } }));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    usePinnedConversations: () => ({
      data: { conversations: pinnedRef.current, filterHonored: true },
      isSuccess: true,
    }),
    useProjects: () => ({ data: [{ id: "p_alpha", name: "Alpha" }] }),
    useProjectSessions: vi.fn(),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations, useProjectSessions } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const useProjectSessionsMock = vi.mocked(useProjectSessions);

function conv(id: string, project?: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: project ? { omni_project: project } : {},
    permission_level: null,
    agent_name: null,
  } as unknown as Conversation;
}

function mockConversations(convs: Conversation[]) {
  useConvMock.mockReturnValue(conversationPage(convs, { first_id: null, last_id: null }));
}

function renderAt(initialEntry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <Routes>
              <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  // ProjectFolder fetches its own paginated list when expanded. The tests
  // above already seed the top-level list; mirror it for the folder query so
  // an expanded folder actually renders rows.
  useProjectSessionsMock.mockReturnValue({
    data: {
      pages: [
        { data: [conv("sibling_b", "Alpha")], first_id: null, last_id: null, has_more: false },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useProjectSessions>);
  pinnedRef.current = [];
  localStorage.clear();
});

afterEach(cleanup);

describe("reveal session in sidebar", () => {
  it("expands a collapsed Projects section and folder, then flashes the row", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Projects"]));
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    mockConversations([conv("sibling_b", "Alpha")]);

    renderAt("/c/sibling_b");
    // The Projects section stays collapsed on plain navigation.
    expect(screen.queryByRole("link", { name: /sibling_b/ })).not.toBeInTheDocument();

    act(() => requestRevealSessionInSidebar("sibling_b"));

    const link = await screen.findByRole("link", { name: /sibling_b/ });
    await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
    expect(
      JSON.parse(localStorage.getItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY) ?? "[]"),
    ).toEqual([]);
    expect(JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY) ?? "[]")).toEqual(
      ["Alpha"],
    );
  });

  it("expands a collapsed Pinned section for a pinned session", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    pinnedRef.current = [conv("pinned_a")];
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Pinned"]));
    mockConversations([conv("pinned_a")]);

    renderAt("/c/pinned_a");
    expect(screen.queryByRole("link", { name: /pinned_a/ })).not.toBeInTheDocument();

    act(() => requestRevealSessionInSidebar("pinned_a"));

    const link = await screen.findByRole("link", { name: /pinned_a/ });
    await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
  });
});
