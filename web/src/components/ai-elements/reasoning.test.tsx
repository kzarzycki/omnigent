import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { writeKeepThinkingExpanded } from "@/lib/keepThinkingExpandedPreferences";
import { Reasoning, ReasoningContent, ReasoningTrigger } from "./reasoning";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderReasoning(isStreaming: boolean, expandable = true) {
  return render(
    <Reasoning isStreaming={isStreaming} expandable={expandable}>
      <ReasoningTrigger />
      <ReasoningContent>Some reasoning text</ReasoningContent>
    </Reasoning>,
  );
}

describe("Reasoning — auto-expand", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(
      <Reasoning isStreaming={true}>
        <ReasoningTrigger />
        <ReasoningContent>{"![leak](https://attacker.example/pixel.png)"}</ReasoningContent>
      </Reasoning>,
    );

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });

  it("renders explicit inline TeX delimiters inside reasoning content", async () => {
    const { container } = render(
      <Reasoning isStreaming={true}>
        <ReasoningTrigger />
        <ReasoningContent>{String.raw`Check \(\sqrt{x + 1}\).`}</ReasoningContent>
      </Reasoning>,
    );

    await waitFor(() => expect(container.querySelector(".katex")).not.toBeNull());
    const katex = container.querySelector(".katex") as HTMLElement;
    expect(katex.querySelector(".sqrt")).not.toBeNull();
    expect(katex.textContent).toContain("x");
    expect(katex.textContent).toContain("1");
  });

  it("renders the trigger button in the open state when isStreaming=true on mount", () => {
    renderReasoning(true);
    const trigger = screen.getByRole("button");
    expect(trigger.getAttribute("data-state")).toBe("open");
  });

  it("shows the shimmer 'Thinking...' label while streaming", () => {
    renderReasoning(true);
    expect(screen.getByText("Thinking...").className).toContain("text-shimmer");
  });

  it("renders the settled 'Thought for...' label without the shimmer", () => {
    renderReasoning(false);
    expect(screen.getByText("Thought for a few seconds").className).not.toContain("text-shimmer");
  });

  it("renders the trigger in the closed state when isStreaming=false on mount", () => {
    renderReasoning(false);
    const trigger = screen.getByRole("button");
    expect(trigger.getAttribute("data-state")).toBe("closed");
  });
});

describe("Reasoning — non-expandable (nothing to show)", () => {
  it("renders no interactive trigger when expandable=false", () => {
    // A settled, empty reasoning section: the header must not be a
    // clickable/focusable collapsible trigger. If this fails, the
    // `expandable=false` path is still rendering a CollapsibleTrigger
    // button — the dead "expands into nothing" affordance we removed.
    renderReasoning(false, false);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not render the reasoning content when expandable=false", () => {
    // Content is suppressed entirely (ReasoningContent returns null), so
    // there is no empty collapsible region under the flat header. A
    // failure here means the content body leaked into the DOM despite
    // there being nothing meaningful to expand.
    renderReasoning(false, false);
    expect(screen.queryByText("Some reasoning text")).toBeNull();
  });

  it("still shows the 'Thought for...' label as a flat header", () => {
    // The header text stays — the user should still see that the model
    // produced a (content-less) reasoning step; only the expand
    // affordance is gone.
    renderReasoning(false, false);
    expect(screen.getByText("Thought for a few seconds")).toBeTruthy();
  });
});

function section(isStreaming: boolean, revealed = false, expandable = true) {
  return (
    <Reasoning isStreaming={isStreaming} expandable={expandable} revealed={revealed}>
      <ReasoningTrigger />
      <ReasoningContent>Some reasoning text</ReasoningContent>
    </Reasoning>
  );
}

const triggerState = () => screen.getByRole("button").getAttribute("data-state");

describe("Reasoning — keep thinking expanded", () => {
  it("opens a finished section on first render when the preference is on", () => {
    writeKeepThinkingExpanded(true);
    render(section(false));
    expect(triggerState()).toBe("open");
    expect(screen.getByText("Some reasoning text")).toBeTruthy();
  });

  it("keeps a section open while streaming and after it finishes when on", () => {
    writeKeepThinkingExpanded(true);
    const view = render(section(true));
    expect(triggerState()).toBe("open");
    view.rerender(section(false));
    expect(triggerState()).toBe("open");
  });

  it("folds a finished section when off (the default)", () => {
    const view = render(section(true));
    expect(triggerState()).toBe("open");
    view.rerender(section(false));
    expect(triggerState()).toBe("closed");
  });

  it("lets one click fold an expanded section, and it stays folded on re-render", () => {
    writeKeepThinkingExpanded(true);
    const view = render(section(false));
    fireEvent.click(screen.getByRole("button"));
    expect(triggerState()).toBe("closed");
    view.rerender(section(false));
    expect(triggerState()).toBe("closed");
  });

  it("keeps a section with nothing to show locked and folded when on", () => {
    writeKeepThinkingExpanded(true);
    render(section(false, false, false));
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText("Some reasoning text")).toBeNull();
    expect(screen.getByText("Thought for a few seconds")).toBeTruthy();
  });

  it("applies a preference turned on after the section mounted, without a remount", () => {
    const view = render(section(false));
    expect(triggerState()).toBe("closed");
    writeKeepThinkingExpanded(true);
    view.rerender(section(false));
    expect(triggerState()).toBe("open");
  });

  it.each([
    [false, "closed"],
    [true, "open"],
  ])("clears a manual fold when a new stream starts (preference %s)", (keep, settled) => {
    writeKeepThinkingExpanded(keep);
    const view = render(section(true));
    fireEvent.click(screen.getByRole("button"));
    expect(triggerState()).toBe("closed");
    view.rerender(section(false));
    expect(triggerState()).toBe("closed");
    view.rerender(section(true));
    expect(triggerState()).toBe("open");
    view.rerender(section(false));
    expect(triggerState()).toBe(settled);
  });

  it.each([false, true])("opens a folded section while find reveals it (preference %s)", (keep) => {
    writeKeepThinkingExpanded(keep);
    const view = render(section(false));
    if (keep) fireEvent.click(screen.getByRole("button"));
    expect(triggerState()).toBe("closed");
    view.rerender(section(false, true));
    expect(triggerState()).toBe("open");
    view.rerender(section(false));
    expect(triggerState()).toBe("closed");
  });
});
