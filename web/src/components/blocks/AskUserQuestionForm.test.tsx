import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { expectMarkdownQuestionCard, MARKDOWN_QUESTIONS } from "@/test/markdownQuestionFixture";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

afterEach(cleanup);

const noop = () => undefined;

function renderForm(question: string, description: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AskUserQuestionForm
        questions={[
          {
            id: "q1",
            question,
            multiSelect: false,
            options: [
              { label: "Ship it", description },
              { label: "Wait", description: "Plain text." },
            ],
          },
        ]}
        onSubmit={noop}
        onReject={noop}
      />
    </QueryClientProvider>,
  );
}

describe("AskUserQuestionForm markdown", () => {
  it("renders the shared Markdown fixture, single- and multi-select", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AskUserQuestionForm questions={MARKDOWN_QUESTIONS} onSubmit={noop} onReject={noop} />
      </QueryClientProvider>,
    );
    await expectMarkdownQuestionCard();
  });

  it("renders a list and a link in the question and a link in an option description", () => {
    const { container } = renderForm(
      "Pick one:\n\n- **Fast** path\n- Safe path\n\nSee [the plan](https://example.com/plan).",
      "Details in [the doc](https://example.com/doc).",
    );
    const legend = container.querySelector("legend") as HTMLElement;
    expect(legend.querySelectorAll("li")).toHaveLength(2);
    expect(legend.textContent).toContain("Fast path");
    expect(legend.querySelector("a")?.getAttribute("href")).toBe("https://example.com/plan");
    const descriptionLink = screen
      .getAllByTestId("ask-user-question-description")[0]!
      .querySelector("a");
    expect(descriptionLink?.getAttribute("href")).toBe("https://example.com/doc");
  });

  it("does not render raw HTML from the question or a description", () => {
    const { container } = renderForm(
      'Question <img src="x" onerror="alert(1)"> <b>bold</b>',
      "<script>alert(1)</script><i>italic</i>",
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("legend b")).toBeNull();
    expect(container.querySelector("i")).toBeNull();
    expect(container.querySelector("legend")?.textContent).toContain("<b>bold</b>");
  });

  it("keeps option labels as plain text", () => {
    renderForm("Which?", "desc");
    expect(screen.getByText("Ship it")).toBeDefined();
  });
});
