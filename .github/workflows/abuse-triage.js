// Abuse triage for external-contributor PRs. Two tiers:
//
//   Tier 1 (deterministic): if the author is on .github/abuse-denylist, close
//   the PR with a templated comment + `likely-spam` label. The denylist is a
//   maintainer-curated, human-edited file -- this is the "ban" action.
//
//   Tier 2 (advisory): otherwise, compute soft spam signals and, if any fire,
//   add the `likely-spam` label so maintainers can triage faster. NEVER closes
//   on heuristics -- a human decides. Avoids auto-closing genuine first-timers.
//
// Trusted authors (maintainers in .github/MAINTAINER, and OWNER/MEMBER/
// COLLABORATOR by author_association) are never flagged or closed.
//
// Reads both lists from disk (the workflow sparse-checks-out .github from the
// trusted default branch, never the PR head). For hermetic unit testing, the
// `opts.maintainers` / `opts.denylist` sets may be injected to bypass the fs
// reads; production passes neither.
//
// Tunable thresholds:
const NEW_ACCOUNT_DAYS = 7; // accounts younger than this are "throwaway-ish"
const MIN_BODY_CHARS = 20; // real description shorter than this is "empty"
const FLOOD_OPEN_PRS = 5; // this many simultaneous open PRs by one author

const DENY_COMMENT = [
  "This pull request was automatically closed because its author is on the",
  "project's abuse denylist (`.github/abuse-denylist`).",
  "",
  "If you believe this is a mistake, please reach out to the maintainers.",
  "",
  "<sub>Automated by abuse-triage.</sub>",
].join("\n");

// Strip HTML comments, headings, and checkbox/template scaffolding so an
// otherwise-empty body that only contains the PR template reads as empty.
function realBodyLength(body) {
  return (body || "")
    .replace(/<!--[\s\S]*?-->/g, "") // HTML comments (template guidance)
    .replace(/^\s*#{1,6}\s.*$/gm, "") // markdown headings
    .replace(/^\s*-\s*\[[ xX]\].*$/gm, "") // task-list checkboxes
    .replace(/\s+/g, " ")
    .trim().length;
}

async function ensureLabel(github, owner, repo, core) {
  try {
    await github.rest.issues.createLabel({
      owner, repo, name: "likely-spam", color: "b60205",
      description: "Auto-flagged as possible spam/abuse; needs maintainer triage",
    });
  } catch (e) {
    if (e.status !== 422) core.warning(`Could not ensure likely-spam label: ${e.message}`);
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

  // Maintainers are never flagged/closed. Fail closed: if the list can't be
  // read, do nothing rather than risk acting on a maintainer's PR.
  const maint = opts.maintainers || readList(".github/MAINTAINER");
  if (!maint) { core.warning("Could not read .github/MAINTAINER; skipping (fail-closed)."); return; }
  if (maint.has(author)) { core.info(`@${author} is a maintainer; skipping.`); return; }

  // Repo-affiliated authors are trusted too (write access etc.).
  const assoc = pr.author_association || "";
  if (["OWNER", "MEMBER", "COLLABORATOR"].includes(assoc)) {
    core.info(`@${author} is ${assoc}; skipping.`); return;
  }

  // --- Tier 1: denylist -> close ---
  const denylist = opts.denylist || readList(".github/abuse-denylist") || new Set();
  if (denylist.has(author)) {
    core.info(`@${author} is on the abuse denylist; closing PR #${pr.number}.`);
    await ensureLabel(github, owner, repo, core);
    await github.rest.issues.addLabels({ owner, repo, issue_number: pr.number, labels: ["likely-spam"] });
    await github.rest.issues.createComment({ owner, repo, issue_number: pr.number, body: DENY_COMMENT });
    await github.rest.pulls.update({ owner, repo, pull_number: pr.number, state: "closed" });
    return;
  }

  // --- Tier 2: heuristic flag (label only, never close) ---
  const reasons = [];

  if (realBodyLength(pr.body) < MIN_BODY_CHARS) reasons.push("empty or template-only description");

  // Throwaway account: created very recently relative to this PR.
  try {
    const { data: u } = await github.rest.users.getByUsername({ username: pr.user.login });
    const ageDays = (Date.parse(pr.created_at) - Date.parse(u.created_at)) / 86400000;
    if (Number.isFinite(ageDays) && ageDays < NEW_ACCOUNT_DAYS) {
      reasons.push(`brand-new account (~${Math.max(0, Math.floor(ageDays))}d old)`);
    }
  } catch (e) { core.info(`account-age check skipped: ${e.message}`); }

  // Flood: many simultaneous open PRs from the same author.
  try {
    const open = await github.paginate(github.rest.pulls.list, { owner, repo, state: "open", per_page: 100 });
    const mine = open.filter((p) => (p.user && p.user.login || "").toLowerCase() === author).length;
    if (mine >= FLOOD_OPEN_PRS) reasons.push(`${mine} simultaneous open PRs`);
  } catch (e) { core.info(`flood check skipped: ${e.message}`); }

  if (reasons.length) {
    await ensureLabel(github, owner, repo, core);
    await github.rest.issues.addLabels({ owner, repo, issue_number: pr.number, labels: ["likely-spam"] });
    core.info(`Flagged #${pr.number} likely-spam: ${reasons.join("; ")}`);
  } else {
    core.info(`#${pr.number} shows no spam signals; not flagged.`);
  }
};

// Exposed for unit testing.
module.exports.realBodyLength = realBodyLength;
