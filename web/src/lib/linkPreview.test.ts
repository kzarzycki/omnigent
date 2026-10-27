import { describe, expect, it } from "vitest";
import { githubLinkLabel, parseGithubLink, parseSessionLink, shortLinkLabel } from "./linkPreview";

describe("parseGithubLink", () => {
  it.each([
    [
      "https://github.com/acme/app/pull/42",
      { owner: "acme", repo: "app", kind: "pull", ref: "42" },
    ],
    [
      "https://github.com/acme/app/issues/7#issuecomment-1",
      { owner: "acme", repo: "app", kind: "issue", ref: "7" },
    ],
    [
      "https://github.com/acme/app/commit/abc1234",
      { owner: "acme", repo: "app", kind: "commit", ref: "abc1234" },
    ],
    [
      "https://github.com/acme/app/actions/runs/99",
      { owner: "acme", repo: "app", kind: "run", ref: "99" },
    ],
  ])("reads %s", (href, expected) => {
    expect(parseGithubLink(href)).toEqual(expected);
  });

  it.each([
    "http://github.com/acme/app/pull/42",
    "https://github.com:8443/acme/app/pull/42",
    "https://github.com.attacker.example/acme/app/pull/42",
    "https://gist.github.com/acme/app/pull/42",
    "https://github.com/acme/app/pull/42/files",
    "https://github.com/acme/app/pull/abc",
    "https://github.com/acme/app/commit/abc",
    "https://github.com/acme/../pull/1",
    "https://attacker.example/?d=secret",
    "not a url",
  ])("ignores %s", (href) => {
    expect(parseGithubLink(href)).toBeNull();
  });
});

describe("githubLinkLabel", () => {
  it("names a PR, a commit and a run briefly", () => {
    expect(githubLinkLabel({ owner: "a", repo: "app", kind: "pull", ref: "42" })).toBe("app#42");
    expect(githubLinkLabel({ owner: "a", repo: "app", kind: "commit", ref: "abc1234def" })).toBe(
      "app@abc1234",
    );
    expect(githubLinkLabel({ owner: "a", repo: "app", kind: "run", ref: "9" })).toBe("app run 9");
  });
});

describe("parseSessionLink", () => {
  const origin = "https://omni.example";

  it("reads a session link on this origin, with or without a file", () => {
    expect(parseSessionLink("/c/conv_1", origin)).toBe("conv_1");
    expect(parseSessionLink("https://omni.example/base/c/conv_1?file=a.md", origin)).toBe("conv_1");
  });

  it("ignores another origin and other paths", () => {
    expect(parseSessionLink("https://attacker.example/c/conv_1", origin)).toBeNull();
    expect(parseSessionLink("/settings", origin)).toBeNull();
  });
});

describe("shortLinkLabel", () => {
  it("keeps a short link's host and path without the scheme", () => {
    expect(shortLinkLabel("https://www.example.com/docs/page")).toBe("example.com/docs/page");
  });

  it("shortens a long path and hides a long query", () => {
    expect(shortLinkLabel("https://example.com/docs/guides/v2/install/linux-arm64")).toBe(
      "example.com/docs/…/linux-arm64",
    );
    expect(shortLinkLabel("https://attacker.example/?d=0123456789abcdef0123456789")).toBe(
      "attacker.example/…/",
    );
  });

  it("leaves a non-http link as it is", () => {
    expect(shortLinkLabel("mailto:a@example.com")).toBe("mailto:a@example.com");
  });
});
