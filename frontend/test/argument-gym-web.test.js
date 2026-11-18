import assert from "node:assert/strict";
import test from "node:test";

import {
  applicabilityView,
  checkKindLabel,
  groupChecks,
  overviewLenses,
  sourceTrustLabel,
  splitChallengesByBasis,
  webLookupView,
} from "../src/components/argumentGym.js";

test("a web check is badged as web search, not as plain AI", () => {
  assert.equal(checkKindLabel("model"), "AI");
  assert.equal(checkKindLabel("web"), "AI + web search");
  assert.equal(checkKindLabel("deterministic"), "");
  const groups = groupChecks([{ id: "web_lookup", category: "web" }]);
  assert.match(groups[0].label, /less reliable/);
});

test("web challenges are split from the main review and kept out of the overview", () => {
  const challenges = [
    { id: 1, basis: "library", category: "legal_authority", severity: "high", importance: 80, disposition: "open" },
    { id: 2, basis: "web", category: "legal_authority", severity: "medium", importance: 55, disposition: "open" },
    { id: 3, category: "framing", severity: "low", importance: 20, disposition: "open" },
  ];
  const { library, web } = splitChallengesByBasis(challenges);
  assert.deepEqual(library.map((item) => item.id), [1, 3]);
  assert.deepEqual(web.map((item) => item.id), [2]);
  const lenses = overviewLenses({ challenges, checksRun: [], checkResults: {} }, []);
  const ids = lenses.flatMap((lens) => (lens.challenges || []).map((item) => item.id));
  assert.ok(!ids.includes(2));
});

test("a web source names its trust tier; a library source does not claim one", () => {
  assert.equal(sourceTrustLabel({ basis: "web", sourceLabel: "Legal aid or nonprofit legal reference website (web search)" }),
    "Web search · Legal aid or nonprofit legal reference website (web search)");
  assert.equal(sourceTrustLabel({ sourceLabel: "Ohio Statutes" }), "Ohio Statutes");
});

test("off, could not run, and ran stay three different states", () => {
  assert.equal(webLookupView({ checksRun: [{ id: "web_lookup", status: "off" }] }).state, "off");
  const unavailable = webLookupView({ checksRun: [{ id: "web_lookup", status: "unavailable", reason: "Web search is not turned on." }] });
  assert.equal(unavailable.state, "unavailable");
  assert.match(unavailable.summary, /could not run: Web search is not turned on/);
  const ran = webLookupView({
    checksRun: [{ id: "web_lookup", status: "on" }],
    webLookup: {
      status: "ran",
      summary: "Web lookup asked 1 question(s).",
      questions: [{ question: "Does the CARES Act notice apply to a market-rate unit?" }],
      withheld: [{ reason: "It contains a street address." }],
      searches: [{ question: "Does the CARES Act notice apply to a market-rate unit?", status: "complete", citations: [] }],
      findings: [{ kind: "corroborates", finding: "Notice period confirmed" }, { kind: "challenge", finding: "x" }],
    },
  }, [{ id: 9 }]);
  assert.equal(ran.state, "ran");
  assert.equal(ran.withheld.length, 1);
  assert.equal(ran.corroborations.length, 1);
  assert.equal(ran.challengeCount, 1);
});

test("an applicability reading shows its quote only when the quote was confirmed in the text", () => {
  const view = applicabilityView({
    checksRun: [{ id: "applicability", status: "on" }],
    applicability: {
      summary: "1 authority read against its own scope text.",
      authorities: [{ authority: "15 U.S.C. 9058", held: true }, { authority: "42 U.S.C. 1437z-9", held: false }],
      assessments: [
        { authority: "15 U.S.C. 9058", verdict: "does_not_apply", grounded: true, quote: "covered property means", source: { citation: "15 U.S.C. § 9058" } },
        { authority: "24 C.F.R. 966.4", verdict: "unclear_on_facts", grounded: false, ungroundedVerdict: "applies", quote: "invented" },
      ],
    },
  });
  assert.equal(view.rows[0].verdictLabel, "May not reach these facts");
  assert.equal(view.rows[0].quote, "covered property means");
  assert.equal(view.rows[1].quote, "");
  assert.equal(view.rows[1].unconfirmed, true);
  assert.deepEqual(view.notInLibrary, ["42 U.S.C. 1437z-9"]);
});

test("the selector says whose defaults a selection matches", async () => {
  const { checkDefaultsStatus } = await import("../src/components/argumentGym.js");
  assert.deepEqual(checkDefaultsStatus(["a", "b"], ["b", "a"], true), { matches: true, text: "These are your saved defaults." });
  assert.equal(checkDefaultsStatus(["a"], ["a", "b"], false).text, "These differ from CLARA's defaults.");
});
