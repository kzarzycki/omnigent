import type { Meta, StoryObj } from "@storybook/react-vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { userEvent, waitFor, within } from "storybook/test";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { ChatLinkPreview } from "./ChatLinkPreview";

const CHAT_SESSION_ID = "conv_chat";
const PULL_URL = "https://github.com/acme/app/pull/42";
const SESSION_URL = `${window.location.origin}/c/conv_9`;

const VIEWER_CONTEXT = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: CHAT_SESSION_ID,
  workspaceRoot: null,
  workspaceHome: null,
};

// Fixture previews sit in the query cache, so the story needs no server.
function FixtureProviders({ children }: { children: ReactNode }) {
  const [client] = useState(() => {
    const seeded = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
    seeded.setQueryData(["github-link-preview", CHAT_SESSION_ID, "acme", "app", "pull", "42"], {
      preview: {
        kind: "pull",
        title: "Fix rendering on narrow screens",
        state: "open",
        labels: [
          { name: "bug", color: "d73a4a", description: null },
          { name: "web", color: "0e8a16", description: null },
        ],
        checks: { passed: 5, failed: 1, pending: 0 },
      },
    });
    seeded.setQueryData(["session-link-preview", "conv_9"], {
      id: "conv_9",
      agent_id: "ag_1",
      title: "Auth refactor",
      status: "idle",
    });
    return seeded;
  });
  return (
    <FileViewerContext.Provider value={VIEWER_CONTEXT}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </FileViewerContext.Provider>
  );
}

function Message({ width }: { width: number }) {
  return (
    <div className="space-y-2 rounded-xl border bg-background p-3 text-sm" style={{ width }}>
      <p>
        Fixed in{" "}
        <ChatLinkPreview href={PULL_URL}>
          <a href={PULL_URL}>app#42</a>
        </ChatLinkPreview>
        .
      </p>
      <p>
        Follow-up in{" "}
        <ChatLinkPreview href={SESSION_URL}>
          <a href={SESSION_URL}>the auth session</a>
        </ChatLinkPreview>
        .
      </p>
    </div>
  );
}

const meta = {
  title: "Blocks/ChatLinkPreview",
  decorators: [
    (Story) => (
      <FixtureProviders>
        <Story />
      </FixtureProviders>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const Desktop: Story = { render: () => <Message width={480} /> };

/** Phone width, after a tap on the status label: the card is open. */
export const PhoneCardOpenAfterTap: Story = {
  render: () => <Message width={343} />,
  parameters: { viewport: { defaultViewport: "mobile1" } },
  play: async ({ canvasElement }) => {
    const label = await waitFor(() =>
      within(canvasElement).getByRole("button", { name: "Pull request status: Open" }),
    );
    await userEvent.click(label);
  },
};

export const PhoneSessionCardOpenAfterTap: Story = {
  render: () => <Message width={343} />,
  parameters: { viewport: { defaultViewport: "mobile1" } },
  play: async ({ canvasElement }) => {
    const label = await waitFor(() =>
      within(canvasElement).getByRole("button", { name: "Session status: Idle" }),
    );
    await userEvent.click(label);
  },
};
