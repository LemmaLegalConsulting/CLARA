import assert from "node:assert/strict";
import test from "node:test";

import {
  alternativeDefaultLabel,
  alternativeOptions,
  TEMPLATE_PLACEHOLDER_VALUE,
  isTemplateChosen,
  templateChoices,
} from "../src/components/templateChoices.js";

const TEMPLATES = [
  { id: 1, title: "Answer and Counterclaims", kind: "answer_counterclaims", jurisdiction: "Cleveland" },
  { id: 2, title: "Motion to Continue", kind: "motion", jurisdiction: "" },
  { id: 3, title: "Novel Motion Shell", kind: "shell", jurisdiction: "" },
];

test("always offers an option for the empty controlled value", () => {
  // Without this the browser displays option[0] while state stays empty, so
  // picking the first template fires no change event and the step dead-ends.
  const choices = templateChoices(TEMPLATES);
  assert.equal(choices[0].value, TEMPLATE_PLACEHOLDER_VALUE);
  assert.equal(choices[0].placeholder, true);
});

test("every template is reachable as a distinct option value", () => {
  const values = templateChoices(TEMPLATES).map((choice) => choice.value);
  assert.deepEqual(values, ["", "1", "2", "3"]);
  assert.equal(new Set(values).size, values.length);
});

test("excludes shell templates only when asked", () => {
  const titles = templateChoices(TEMPLATES, { excludeShells: true }).map((choice) => choice.label);
  assert.ok(!titles.includes("Novel Motion Shell"));
  assert.ok(templateChoices(TEMPLATES).map((choice) => choice.label).includes("Novel Motion Shell"));
});

test("tolerates a missing or empty template list", () => {
  assert.deepEqual(templateChoices(undefined).map((choice) => choice.value), [""]);
  assert.deepEqual(templateChoices([]).map((choice) => choice.value), [""]);
});

test("the placeholder does not count as a chosen template", () => {
  assert.equal(isTemplateChosen(null), false);
  assert.equal(isTemplateChosen(undefined), false);
  assert.equal(isTemplateChosen(""), false);
  assert.equal(isTemplateChosen("1"), true);
  assert.equal(isTemplateChosen(1), true);
});

test("either/or options read the same whether named or bare", () => {
  const choice = {
    name: "lease_form",
    default: "written",
    options: [{ value: "written", label: "Written" }, "month_to_month"],
  };
  assert.deepEqual(alternativeOptions(choice), [
    { value: "written", label: "Written" },
    { value: "month_to_month", label: "Month to month" },
  ]);
  assert.equal(alternativeDefaultLabel(choice), "Written");
});
