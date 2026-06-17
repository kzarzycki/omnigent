// Local unit test for abuse-triage.js -- mocks the GitHub client and runs the
// real logic against the real .github/MAINTAINER (cwd must be the repo root).
// The denylist is injected via opts to stay hermetic (no real ban entry). No
// network.
const path = require("path");
const script = require(path.resolve(".github/workflows/abuse-triage.js"));

async function run({
  author = "someexternaldev",
  assoc = "FIRST_TIME_CONTRIBUTOR",
  body = "A reasonably detailed description of what this change does and why.",
  accountAgeDays = 400,
  openCount = 1,
  denylist = null,
  prCreatedAt = "2026-06-17T00:00:00Z",
} = {}) {
  const labels = [], comments = [];
  let closed = false;
  const list = () => {}; list._tag = "open";
  const accountCreated = new Date(Date.parse(prCreatedAt) - accountAgeDays * 86400000).toISOString();
  const github = {
    paginate: async (fn) =>
      fn._tag === "open" ? Array.from({ length: openCount }, () => ({ user: { login: author } })) : [],
    rest: {
      issues: {
        createLabel: async () => {},
        addLabels: async ({ labels: l }) => labels.push(...l),
        createComment: async ({ body: b }) => comments.push(b),
      },
      pulls: {
        list,
        update: async ({ state }) => { if (state === "closed") closed = true; },
      },
      users: { getByUsername: async () => ({ data: { created_at: accountCreated } }) },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: { pull_request: {
      number: 1, user: { login: author }, author_association: assoc, body, created_at: prCreatedAt,
    } },
  };
  const core = { info: () => {}, warning: (m) => console.log("WARN", m) };
  const opts = denylist ? { denylist: new Set(denylist.map((s) => s.toLowerCase())) } : {};
  await script({ github, context, core, opts });
  return { labels, comments, closed };
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

(async () => {
  // Tier 1: denylisted author -> close + label + comment.
  let r = await run({ author: "spammer123", denylist: ["spammer123"] });
  assert("denylisted author is closed + labeled + commented",
    r.closed && r.labels.includes("likely-spam") && r.comments.length === 1, JSON.stringify(r));

  // Trusted authors are never touched.
  r = await run({ author: "dhruv0811" }); // real maintainer in .github/MAINTAINER
  assert("maintainer is skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));
  r = await run({ assoc: "COLLABORATOR" });
  assert("collaborator is skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));

  // Tier 2: heuristics flag (label only, never close).
  r = await run({ body: "" });
  assert("empty body -> flagged, not closed", !r.closed && r.labels.includes("likely-spam"), JSON.stringify(r));

  r = await run({ accountAgeDays: 2 });
  assert("brand-new account -> flagged", !r.closed && r.labels.includes("likely-spam"), JSON.stringify(r));

  r = await run({ openCount: 5 });
  assert("flood (>=5 open PRs) -> flagged", !r.closed && r.labels.includes("likely-spam"), JSON.stringify(r));

  // Clean external PR: good body, old account, few PRs -> no action.
  r = await run({});
  assert("clean PR is not flagged or closed", !r.closed && r.labels.length === 0, JSON.stringify(r));

  // A denylisted author who is also a maintainer is NOT closed (trusted wins).
  r = await run({ author: "dhruv0811", denylist: ["dhruv0811"] });
  assert("maintainer on denylist is still skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));

  // realBodyLength: a template-only body reads as ~empty.
  const tmpl = "<!-- describe your change -->\n## Summary\n\n- [ ] Bug fix\n- [ ] Feature\n";
  assert("realBodyLength strips template scaffolding", script.realBodyLength(tmpl) < 20, String(script.realBodyLength(tmpl)));
})();
