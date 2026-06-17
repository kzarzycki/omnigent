// Abuse triage for external-contributor PRs (v1: denylist only).
//
// If the PR author is on .github/abuse-denylist, close the PR with a templated
// comment + `spam-check` label. The denylist is a maintainer-curated, human-
// edited file -- adding a name is the deliberate "ban" action, and it only
// takes effect on the banned author's *next* PR (it's a reactive guard; first
// offenses are handled by GitHub's first-time-approval gate + a maintainer
// closing the PR). Complements GitHub's native "Block user".
//
// Trusted authors (maintainers in .github/MAINTAINER, and OWNER/MEMBER/
// COLLABORATOR by author_association) are never closed, even if mistakenly
// listed.
//
// Reads both lists from disk (the workflow sparse-checks-out .github from the
// trusted default branch, never the PR head). For hermetic unit testing,
// `opts.maintainers` / `opts.denylist` sets may be injected to bypass the fs
// reads; production passes neither.
//
// (Heuristic flagging and an LLM content judge were designed but deferred --
// see git history -- to keep v1 deterministic and false-positive-free.)

const DENY_COMMENT = [
  "This pull request was automatically closed because its author is on the",
  "project's abuse denylist (`.github/abuse-denylist`).",
  "",
  "If you believe this is a mistake, please reach out to the maintainers.",
  "",
  "<sub>Automated by abuse-triage.</sub>",
].join("\n");

async function ensureLabel(github, owner, repo, core) {
  try {
    await github.rest.issues.createLabel({
      owner, repo, name: "spam-check", color: "b60205",
      description: "Auto-flagged as possible spam/abuse; needs maintainer triage",
    });
  } catch (e) {
    if (e.status !== 422) core.warning(`Could not ensure spam-check label: ${e.message}`);
  }
}

module.exports = async ({ github, context, core, opts = {} }) => {
  const fs = require("fs");
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;
  if (!pr) { core.info("No PR in payload; nothing to do."); return; }

  const readList = (p) => {
    try {
      return new Set(
        fs.readFileSync(p, "utf8").split("\n")
          .map((l) => l.replace(/#.*/, "").trim().toLowerCase())
          .filter(Boolean)
      );
    } catch (e) { return null; }
  };

  const author = (pr.user && pr.user.login ? pr.user.login : "").toLowerCase();

  // Maintainers are never closed. Fail closed: if the list can't be read, do
  // nothing rather than risk acting on a maintainer's PR.
  const maint = opts.maintainers || readList(".github/MAINTAINER");
  if (!maint) { core.warning("Could not read .github/MAINTAINER; skipping (fail-closed)."); return; }
  if (maint.has(author)) { core.info(`@${author} is a maintainer; skipping.`); return; }

  // Repo-affiliated authors are trusted too (write access etc.).
  const assoc = pr.author_association || "";
  if (["OWNER", "MEMBER", "COLLABORATOR"].includes(assoc)) {
    core.info(`@${author} is ${assoc}; skipping.`); return;
  }

  const denylist = opts.denylist || readList(".github/abuse-denylist") || new Set();
  if (denylist.has(author)) {
    core.info(`@${author} is on the abuse denylist; closing PR #${pr.number}.`);
    await ensureLabel(github, owner, repo, core);
    await github.rest.issues.addLabels({ owner, repo, issue_number: pr.number, labels: ["spam-check"] });
    await github.rest.issues.createComment({ owner, repo, issue_number: pr.number, body: DENY_COMMENT });
    await github.rest.pulls.update({ owner, repo, pull_number: pr.number, state: "closed" });
    return;
  }

  core.info(`@${author} is not on the abuse denylist; nothing to do.`);
};
