// Which chat links get a preview, decided from the URL alone. Only github.com
// links of four shapes and this server's own session links qualify; every
// other URL is shown shortened and never fetched by the server or the browser.

/** A github.com link the server can preview through the GitHub API. */
export interface GithubLinkRef {
  owner: string;
  repo: string;
  kind: "pull" | "issue" | "commit" | "run";
  /** The PR or issue number, commit SHA or run id. */
  ref: string;
}

const GITHUB_PATH_RE =
  /^\/([A-Za-z0-9._-]+)\/([A-Za-z0-9._-]+)\/(pull|issues|commit|actions\/runs)\/([0-9a-fA-F]+)\/?$/;

/**
 * Parse an `https://github.com/<owner>/<repo>/{pull,issues,commit,actions/runs}/<ref>`
 * link; any other URL, sub-page or host (lookalikes included) is `null`.
 *
 * @param href - The link's destination, e.g. `"https://github.com/acme/app/pull/42"`.
 * @returns The parts the preview route takes, or `null`.
 */
export function parseGithubLink(href: string | undefined): GithubLinkRef | null {
  if (!href) return null;
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.hostname !== "github.com" || url.port) return null;
  const match = url.pathname.match(GITHUB_PATH_RE);
  if (!match) return null;
  const [, owner, repo, segment, ref] = match;
  if (owner.includes("..") || repo.includes("..")) return null;
  const kind =
    segment === "pull"
      ? "pull"
      : segment === "issues"
        ? "issue"
        : segment === "commit"
          ? "commit"
          : "run";
  const valid = kind === "commit" ? /^[0-9a-fA-F]{7,40}$/.test(ref) : /^[0-9]{1,20}$/.test(ref);
  return valid ? { owner, repo, kind, ref } : null;
}

/**
 * The short label a bare GitHub link shows: `app#42`, `app@abc1234` or `app run 99`.
 *
 * @param link - A parsed GitHub link.
 */
export function githubLinkLabel(link: GithubLinkRef): string {
  if (link.kind === "commit") return `${link.repo}@${link.ref.slice(0, 7)}`;
  if (link.kind === "run") return `${link.repo} run ${link.ref}`;
  return `${link.repo}#${link.ref}`;
}

/**
 * The session id of a link to a session on this server (`/c/<id>`, with any
 * query such as `?file=`), or `null` for any other origin or path.
 *
 * @param href - The link's destination, already rebased under the base path.
 * @param origin - This page's origin, e.g. `window.location.origin`.
 */
export function parseSessionLink(href: string | undefined, origin: string): string | null {
  if (!href) return null;
  let url: URL;
  try {
    url = new URL(href, origin);
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;
  const match = url.pathname.match(/(?:^|\/)c\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

const SHORT_PATH_MAX = 24;

/**
 * Show a bare link as its host plus a shortened path, so a long URL stays
 * readable and its destination stays clear: `example.com/docs/…/page`.
 *
 * @param href - An absolute http(s) URL; anything else is returned unchanged.
 */
export function shortLinkLabel(href: string): string {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return href;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return href;
  const host = url.host.replace(/^www\./, "");
  const path = `${url.pathname}${url.search}${url.hash}`.replace(/^\/$/, "");
  if (path.length <= SHORT_PATH_MAX) return `${host}${path}`;
  const segments = url.pathname.split("/").filter(Boolean);
  const last = segments.at(-1) ?? "";
  const first = segments.length > 1 ? `/${segments[0]}` : "";
  const tail = last.length > SHORT_PATH_MAX ? `${last.slice(0, SHORT_PATH_MAX - 1)}…` : last;
  return `${host}${first}/…/${tail}`;
}
