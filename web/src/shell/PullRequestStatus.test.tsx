import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PullRequestStatus } from "./PullRequestStatus";

describe("PullRequestStatus", () => {
  it("shows an issue status's name coloured by its category", () => {
    render(
      <PullRequestStatus
        state="open"
        status={{ name: "In progress", category: "active" }}
        subject="Issue"
      />,
    );
    const badge = screen.getByLabelText("Issue status: In progress");
    expect(badge.className).toContain("text-green-700");
  });

  it("falls back to the state when there is no status", () => {
    render(<PullRequestStatus state="not_planned" subject="Issue" />);
    expect(screen.getByLabelText("Issue status: Not planned")).toBeTruthy();
  });
});
