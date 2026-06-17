// Local unit test for abuse-triage.js (v1: denylist only) -- mocks the GitHub
// client and runs the real logic against the real .github/MAINTAINER (cwd must
// be the repo root). The denylist is injected via opts to stay hermetic (no
// real ban entry). No network.
const path = require("path");
const script = require(path.resolve(".github/workflows/abuse-triage.js"));

async function run({
  author = "someexternaldev",
  assoc = "FIRST_TIME_CONTRIBUTOR",
  denylist = null,
} = {}) {
  const labels = [], comments = [];
  let closed = false;
  const github = {
    rest: {
      issues: {
        createLabel: async () => {},
        addLabels: async ({ labels: l }) => labels.push(...l),
        createComment: async ({ body: b }) => comments.push(b),
      },
      pulls: {
        update: async ({ state }) => { if (state === "closed") closed = true; },
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: { pull_request: { number: 1, user: { login: author }, author_association: assoc } },
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
  // Denylisted author -> close + label + comment.
  let r = await run({ author: "spammer123", denylist: ["spammer123"] });
  assert("denylisted author is closed + labeled + commented",
    r.closed && r.labels.includes("spam-check") && r.comments.length === 1, JSON.stringify(r));

  // Trusted authors are never touched.
  r = await run({ author: "dhruv0811" }); // real maintainer in .github/MAINTAINER
  assert("maintainer is skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));
  r = await run({ assoc: "COLLABORATOR" });
  assert("collaborator is skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));

  // A denylisted author who is also a maintainer is NOT closed (trusted wins).
  r = await run({ author: "dhruv0811", denylist: ["dhruv0811"] });
  assert("maintainer on denylist is still skipped", !r.closed && r.labels.length === 0, JSON.stringify(r));

  // Not on the denylist -> no action.
  r = await run({ author: "someexternaldev" });
  assert("non-denylisted author is untouched", !r.closed && r.labels.length === 0, JSON.stringify(r));
})();
