export function fieldText(field) {
  if (field.value === null || field.value === undefined) return "";
  if (["json", "control", "boolean"].includes(field.kind)) return JSON.stringify(field.value, null, 2);
  return String(field.value);
}

export function parseAnswers(fields, edits) {
  return Object.fromEntries(fields.filter((field) => Object.hasOwn(edits, field.key)).map((field) => {
    const text = edits[field.key];
    if (text === "") return [field.key, ""];
    if (["json", "control", "boolean"].includes(field.kind)) {
      let value;
      try { value = JSON.parse(text); }
      catch { throw new Error(`${field.label}: enter valid JSON (for example true, false, [], or a quoted value).`); }
      if (field.kind === "json" && (value === null || typeof value !== "object")) {
        throw new Error(`${field.label}: enter a JSON list or object.`);
      }
      return [field.key, value];
    }
    return [field.key, text];
  }));
}

function isEmpty(value) {
  return value === "" || value === null || value === undefined;
}

function currentText(field, edits) {
  return Object.hasOwn(edits, field.key) ? edits[field.key] : fieldText(field);
}

// A blank text field becomes a highlighted prompt in the DOCX; a blank control,
// collection, or condition input stops the export, so it must be answered here.
export function mustAnswer(field) {
  return Boolean(field.required) || ["json", "control"].includes(field.kind);
}

// Groups come from the saved session, not from edits, so a field does not jump
// to another group while someone is typing in it.
export const FIELD_GROUPS = [
  { id: "needed", title: "Answer before download", note: "The template uses these to decide what to include, so the DOCX cannot be prepared without them." },
  { id: "blank", title: "Blank — type now or finish in Word", note: "Anything left blank becomes a yellow-highlighted prompt in the downloaded DOCX." },
  { id: "filled", title: "Filled in", note: "Check these values. Change any that are wrong for this document." },
];

export function groupFields(fields) {
  const groups = Object.fromEntries(FIELD_GROUPS.map((group) => [group.id, []]));
  for (const field of fields) {
    if (!isEmpty(field.value)) groups.filled.push(field);
    else groups[mustAnswer(field) ? "needed" : "blank"].push(field);
  }
  return FIELD_GROUPS.map((group) => ({ ...group, fields: groups[group.id] })).filter((group) => group.fields.length);
}

export function fieldStatus(field, edits = {}) {
  const text = currentText(field, edits);
  if (isEmpty(text)) return mustAnswer(field) ? { tone: "needed", label: "Needed before download" } : { tone: "blank", label: "Blank — prompt in Word" };
  if (Object.hasOwn(edits, field.key) && text !== fieldText(field)) return { tone: "edited", label: "Changed, not saved" };
  return { tone: "filled", label: "Filled" };
}

export function fillSummary(fields, edits = {}) {
  const summary = { filled: 0, blank: 0, needed: 0, edited: 0 };
  for (const field of fields) {
    const { tone } = fieldStatus(field, edits);
    summary[tone === "edited" ? "filled" : tone] += 1;
    if (tone === "edited") summary.edited += 1;
  }
  return summary;
}

// The server labels a blank the converter could not name by where it sits
// ("text after “Case No.”"). Sessions prepared before that still carry the
// invented name, which says less than nothing.
export function fieldLabel(field) {
  return /^placeholder \d+( |$)/.test(field.label || "") ? "Unnamed blank" : field.label;
}

// The paragraph a field appears in, with this field's blank marked and every
// other blank showing what it currently holds -- a value, or its name.
export function contextPieces(field, fields, edits = {}) {
  if (!Array.isArray(field.context)) return field.context ? [{ kind: "text", text: field.context }] : [];
  const byKey = Object.fromEntries(fields.map((item) => [item.key, item]));
  return field.context.map((segment) => {
    if (segment.break) return { kind: "break", text: "" };
    if (!segment.fields) return { kind: "text", text: segment.text };
    if (segment.fields.includes(field.key)) return { kind: "this", text: currentText(field, edits) || "this blank" };
    const other = byKey[segment.fields[0]];
    const text = other ? currentText(other, edits) : "";
    return text ? { kind: "other", text } : { kind: "empty", text: other ? fieldLabel(other) : "blank" };
  });
}

// A short one-line text blank can be typed straight into its sentence. Longer
// passages, multi-line answers, choices and JSON keep the side-by-side layout.
export const INLINE_LINE_LIMIT = 200;

export function canShowInline(field) {
  if (field.kind !== "text" || field.choices?.length || !Array.isArray(field.context)) return false;
  if (!field.context.some((segment) => segment.fields?.includes(field.key))) return false;
  const length = field.context.reduce((total, segment) => total + (segment.text?.length || 0) + (segment.fields ? 12 : 0), 0);
  return length <= INLINE_LINE_LIMIT;
}

function* markedSegments(blocks = []) {
  for (const block of blocks) {
    if (block.kind === "table") for (const row of block.rows) for (const cell of row) yield* markedSegments(cell);
    else for (const segment of block.segments || []) if (segment.key && (segment.prompt !== undefined || segment.filled !== undefined)) yield segment;
  }
}

// The next blank the reader meets after this field, in document order, so a
// preview can be filled top to bottom without going back to the list.
export function nextBlankKey(preview, key) {
  if (!preview) return null;
  const segments = [...markedSegments(preview.header), ...markedSegments(preview.body), ...markedSegments(preview.footer)];
  const start = segments.findIndex((segment) => segment.key === key);
  return segments.slice(start + 1).find((segment) => segment.prompt !== undefined && segment.key !== key)?.key ?? null;
}
