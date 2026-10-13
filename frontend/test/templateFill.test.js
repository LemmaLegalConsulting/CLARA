import test from "node:test";
import assert from "node:assert/strict";
import { canShowInline, contextPieces, fieldLabel, nextBlankKey, fieldStatus, fieldText, fillSummary, groupFields, parseAnswers } from "../src/components/templateFill.js";

test("mapped zero and false stay answered; clearing is an explicit answer", () => {
  const fields = [{ key: "count", value: 0, kind: "text" }, { key: "attend", value: false, kind: "control" }, { key: "missing", value: null }];
  assert.equal(fieldText(fields[0]), "0");
  assert.equal(fieldText(fields[1]), "false");
  assert.deepEqual(fillSummary(fields), { filled: 2, blank: 1, needed: 0, edited: 0 });
  assert.deepEqual(fillSummary(fields, { count: "" }), { filled: 1, blank: 2, needed: 0, edited: 0 });
  assert.deepEqual(parseAnswers(fields, { count: "" }), { count: "" });
});

test("structured values round trip and bad JSON never reaches the API", () => {
  const fields = [{ key: "people", label: "People", kind: "json", value: [{ name: "Alex" }] }, { key: "attend", label: "Attend", kind: "control" }];
  assert.deepEqual(parseAnswers(fields, { people: fieldText(fields[0]), attend: "false" }), { people: [{ name: "Alex" }], attend: false });
  assert.throws(() => parseAnswers(fields, { people: "[unfinished" }), /People/);
  assert.throws(() => parseAnswers(fields, { people: '"text"' }), /list or object/);
  assert.deepEqual(parseAnswers(fields, { people: "[]" }), { people: [] });
});

test("fields group by what still has to happen, and stay put while typed in", () => {
  const fields = [
    { key: "client", label: "Client", kind: "text", value: "Sample Client", state: "mapped" },
    { key: "hearing", label: "Hearing", kind: "text", value: null },
    { key: "tenants", label: "Tenants", kind: "json", value: null },
    { key: "attend", label: "Attend", kind: "control", value: null, required: true },
  ];
  assert.deepEqual(groupFields(fields).map((group) => [group.id, group.fields.map((field) => field.key)]),
    [["needed", ["tenants", "attend"]], ["blank", ["hearing"]], ["filled", ["client"]]]);
  const edits = { hearing: "May 1", client: "Sample Client" };
  assert.equal(groupFields(fields).find((group) => group.id === "blank").fields[0].key, "hearing");
  assert.equal(fieldStatus(fields[1], edits).tone, "edited");
  assert.equal(fieldStatus(fields[0], edits).tone, "filled", "retyping the saved value is not a change");
  assert.equal(fieldStatus(fields[0], { client: "" }).tone, "blank");
  assert.equal(fieldStatus(fields[3]).tone, "needed");
  assert.deepEqual(fillSummary(fields, edits), { filled: 2, blank: 0, needed: 2, edited: 1 });
});

test("the context marks this field's blank and shows what the others hold", () => {
  const sentence = [{ text: "I am the " }, { fields: ["role"] }, { text: " in " }, { fields: ["caption"] }, { text: ", Case No. " }, { fields: ["fields['placeholder_6_blank_1']"] }];
  const fields = [
    { key: "role", label: "role", kind: "text", value: "defendant", context: sentence },
    { key: "caption", label: "case caption", kind: "text", value: null, context: sentence },
    { key: "fields['placeholder_6_blank_1']", path: "fields.placeholder_6_blank_1", label: "placeholder 6 blank 1", kind: "text", value: null, context: sentence },
  ];
  assert.deepEqual(contextPieces(fields[1], fields).map((piece) => [piece.kind, piece.text]), [
    ["text", "I am the "], ["other", "defendant"], ["text", " in "], ["this", "this blank"], ["text", ", Case No. "], ["empty", "Unnamed blank"],
  ]);
  assert.equal(contextPieces(fields[1], fields, { caption: "Smith v. Jones" })[3].text, "Smith v. Jones");
  assert.equal(fieldLabel(fields[0]), "role");
  assert.equal(fieldLabel({ path: "fields.placeholder_6_blank_1", label: "text after “Case No.”" }), "text after “Case No.”");
  assert.deepEqual(contextPieces({ key: "old", context: "Saved before contexts were structured" }, []), [{ kind: "text", text: "Saved before contexts were structured" }]);
});

test("only short one-line text blanks go inline", () => {
  const short = [{ text: "COUNTY OF " }, { fields: ["county"] }, { text: " :" }];
  assert.equal(canShowInline({ key: "county", kind: "text", context: short }), true);
  assert.equal(canShowInline({ key: "county", kind: "multiline", context: short }), false);
  assert.equal(canShowInline({ key: "county", kind: "text", choices: ["a"], context: short }), false);
  assert.equal(canShowInline({ key: "other", kind: "text", context: short }), false, "the sentence must contain this blank");
  assert.equal(canShowInline({ key: "county", kind: "text", context: "Saved before contexts were structured" }), false);
  assert.equal(canShowInline({ key: "county", kind: "text", context: [{ text: "x".repeat(250) }, { fields: ["county"] }] }), false);
});

test("next blank follows the document, skipping filled values and repeats", () => {
  const preview = { header: [{ kind: "p", segments: [{ prompt: "Court", key: "court" }] }], footer: [], body: [
    { kind: "p", segments: [{ text: "I, " }, { prompt: "name", key: "name" }, { text: " in " }, { filled: "Smith", key: "caption" }] },
    { kind: "table", rows: [[[{ kind: "p", segments: [{ prompt: "name", key: "name" }, { prompt: "date", key: "date" }] }]]] },
  ] };
  assert.equal(nextBlankKey(preview, "court"), "name");
  assert.equal(nextBlankKey(preview, "name"), "date", "a second use of the same field is not the next blank");
  assert.equal(nextBlankKey(preview, "caption"), "name");
  assert.equal(nextBlankKey(preview, "date"), null);
  assert.equal(nextBlankKey(preview, "unknown"), "court");
  assert.equal(nextBlankKey(null, "court"), null);
});
