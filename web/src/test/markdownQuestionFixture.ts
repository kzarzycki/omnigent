// Fixture + assertions shared by the question-card Markdown tests (the form,
// the Inbox page and the approve page), so all three prove the same rendering.

import { fireEvent, screen } from "@testing-library/react";
import { expect } from "vitest";

const PREVIEW = "- not *a* list [x](https://example.com/x)";

/** Two questions: single-select with a preview, then multi-select. Fixture text only. */
export const MARKDOWN_QUESTIONS = [
  {
    id: "path",
    multiSelect: false,
    question:
      "Pick a path:\n\n- **Fast** path\n- Safe path\n\nSee [the plan](https://example.com/plan), <b>raw</b> html and [bad](javascript:alert(1)).",
    options: [
      {
        label: "**Canary**",
        description: "Read [the runbook](https://example.com/runbook).",
        preview: PREVIEW,
      },
      { label: "All at once", description: "Plain description." },
    ],
  },
  {
    id: "checks",
    question: "Which checks?",
    multiSelect: true,
    options: [
      { label: "Lint", description: "Rules in [the style guide](https://example.com/style)." },
      { label: "Tests", description: "Plain description." },
    ],
  },
];

/** The payload shape the server sends as `ask_user_question`. */
export const MARKDOWN_QUESTION_PAYLOAD = { questions: MARKDOWN_QUESTIONS };

function expectNewTabLink(anchor: Element | null, href: string) {
  expect(anchor?.getAttribute("href")).toBe(href);
  expect(anchor?.getAttribute("target")).toBe("_blank");
}

/**
 * Assert the card in `root` (showing question 1) renders Markdown, then walk
 * to the multi-select question and assert the same there. Waits for the card.
 */
export async function expectMarkdownQuestionCard(root: HTMLElement = document.body) {
  const legend = (await screen.findByTestId("ask-user-question-section")).querySelector("legend")!;
  expect(legend.querySelectorAll("li")).toHaveLength(2);
  expect(legend.textContent).toContain("Fast path");
  expectNewTabLink(
    legend.querySelector('a[href="https://example.com/plan"]'),
    "https://example.com/plan",
  );
  // Raw HTML shows as typed; an unsafe scheme never becomes a working link.
  expect(legend.textContent).toContain("<b>raw</b>");
  expect(root.querySelector("legend b")).toBeNull();
  expect(root.querySelector('a[href^="javascript"]')).toBeNull();
  expect(legend.textContent).toContain("bad");

  const descriptions = screen.getAllByTestId("ask-user-question-description");
  expectNewTabLink(descriptions[0]!.querySelector("a"), "https://example.com/runbook");

  // Labels and previews stay plain text.
  expect(screen.getByText("**Canary**")).toBeTruthy();
  fireEvent.click(screen.getAllByRole("radio")[0]!);
  const preview = screen.getByTestId("ask-user-question-previews");
  expect(preview.textContent).toBe(PREVIEW);
  expect(preview.querySelector("a, li")).toBeNull();

  // Multi-select question.
  fireEvent.click(screen.getByTestId("ask-user-question-next"));
  expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(1);
  const multiDescriptions = screen.getAllByTestId("ask-user-question-description");
  expectNewTabLink(multiDescriptions[0]!.querySelector("a"), "https://example.com/style");
}
