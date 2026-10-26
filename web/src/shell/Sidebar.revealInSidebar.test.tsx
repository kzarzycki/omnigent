import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// "Show in sidebar" expands the sections hiding the current session, then
// scrolls to and flashes its row.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import * as identity from "@/lib/identity";
import {
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
} from "@/shell/sidebarNav";
import {
  requestRevealSessionInSidebar,
  SIDEBAR_REVEAL_FLASH_CLASS,
  useSidebarRevealRequest,
} from "./sidebarReveal";

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

function renderAt(initialEntry: string, info?: ServerInfo) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={info ?? "loading"}>
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
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

const GROUPING_UI_INFO: ServerInfo = {
  ...FALLBACK_SERVER_INFO,
  features: { ...FALLBACK_SERVER_INFO.features, sidebar_grouping_ui: true },
};

function collapsedSections(): string[] {
  return JSON.parse(localStorage.getItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY) ?? "[]");
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  // ProjectFolder fetches its own paginated list when expanded; seed it so an
  // expanded folder actually renders rows.
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
    // The request is one-shot: a list that mounts later must not replay it.
    expect(renderHook(() => useSidebarRevealRequest()).result.current).toBeNull();
  });

  it("expands Projects and the folder for a session filed by project_id", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Projects"]));
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    // Move to project files a session by id and blanks the legacy label.
    const filed = { ...conv("sibling_b"), project_id: "p_alpha", labels: { omni_project: "" } };
    mockConversations([filed as Conversation]);

    renderAt("/c/sibling_b");
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

  // The flat list is titled "Sessions" but stores its collapsed state as "Chats".
  it("expands a collapsed Sessions list for an unfiled session", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Chats"]));
    mockConversations([conv("chat_a")]);

    renderAt("/c/chat_a");
    expect(screen.queryByRole("link", { name: /chat_a/ })).not.toBeInTheDocument();
    act(() => requestRevealSessionInSidebar("chat_a"));

    const link = await screen.findByRole("link", { name: /chat_a/ });
    await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
    expect(collapsedSections()).toEqual([]);
  });

  it("expands the collapsed group holding the session", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem("omnigent:sidebar-view", JSON.stringify({ grouping: "created" }));
    localStorage.setItem(
      COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
      JSON.stringify(["Created earlier"]),
    );
    mockConversations([conv("chat_old")]);

    renderAt("/c/chat_old", GROUPING_UI_INFO);
    expect(screen.getByText("Created earlier")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /chat_old/ })).not.toBeInTheDocument();
    act(() => requestRevealSessionInSidebar("chat_old"));

    const link = await screen.findByRole("link", { name: /chat_old/ });
    await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
    expect(collapsedSections()).toEqual([]);
  });

  it("expands a collapsed Shared section for a session another user owns", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const viewer = vi.spyOn(identity, "getCurrentUserId").mockReturnValue("viewer@example.com");
    try {
      localStorage.setItem("omnigent:session-filter", "all");
      localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Shared"]));
      const shared = { ...conv("shared_a"), owner: "other@example.com" };
      mockConversations([shared as Conversation]);

      renderAt("/c/shared_a", GROUPING_UI_INFO);
      expect(screen.getByRole("button", { name: "Shared" })).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /shared_a/ })).not.toBeInTheDocument();
      act(() => requestRevealSessionInSidebar("shared_a"));

      const link = await screen.findByRole("link", { name: /shared_a/ });
      await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
      expect(collapsedSections()).toEqual([]);
    } finally {
      viewer.mockRestore();
    }
  });

  it("keeps the folder of a pinned, filed session shut", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    pinnedRef.current = [conv("pinned_filed", "Alpha")];
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Projects"]));
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    mockConversations([conv("pinned_filed", "Alpha")]);

    renderAt("/c/pinned_filed");
    act(() => requestRevealSessionInSidebar("pinned_filed"));

    const link = await screen.findByRole("link", { name: /pinned_filed/ });
    await waitFor(() => expect(link.closest("li")).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));
    expect(collapsedSections()).toEqual(["Projects"]);
    expect(JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY) ?? "[]")).toEqual(
      [],
    );
  });

  it("drops an in-flight reveal when another session opens", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    mockConversations([conv("chat_a"), conv("chat_b")]);

    renderAt("/c/chat_a");
    act(() => requestRevealSessionInSidebar("chat_a"));
    const rowA = (await screen.findByRole("link", { name: /chat_a/ })).closest("li");
    await waitFor(() => expect(rowA).toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS));

    fireEvent.click(screen.getByRole("link", { name: /chat_b/ }));
    // Well inside the ring's own 1.6 s lifetime.
    await waitFor(() => expect(rowA).not.toHaveClass(SIDEBAR_REVEAL_FLASH_CLASS), {
      timeout: 200,
    });
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
