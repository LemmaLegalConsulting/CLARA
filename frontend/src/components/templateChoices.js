// Shared option model for the template <select> controls.
//
// The controls are React-controlled with an empty string for "nothing chosen".
// Without an option carrying that value the browser falls back to displaying
// option[0] while state stays empty, so choosing the first template fires no
// change event and the step cannot be completed.

export const TEMPLATE_PLACEHOLDER_VALUE = "";
export const TEMPLATE_PLACEHOLDER_LABEL = "Choose a template…";

export function templateChoices(templates, { excludeShells = false } = {}) {
  const usable = (templates || []).filter(
    (template) => template && (!excludeShells || template.kind !== "shell"),
  );
  return [
    { value: TEMPLATE_PLACEHOLDER_VALUE, label: TEMPLATE_PLACEHOLDER_LABEL, placeholder: true },
    ...usable.map((template) => ({
      value: String(template.id),
      label: template.title,
      jurisdiction: template.jurisdiction || "",
      placeholder: false,
    })),
  ];
}

export function isTemplateChosen(selectedTemplateId) {
  return String(selectedTemplateId ?? "") !== TEMPLATE_PLACEHOLDER_VALUE;
}

// An either/or clause's options as {value, label}. Manifests write an option
// either as a bare snake_case value ("email") or, where a reviewer named it,
// as {value, label}; rendering the object form directly printed
// "[object Object]" in the select.
export function alternativeOptions(choice) {
  return (choice?.options || []).map((option) => {
    if (option && typeof option === "object") {
      const value = String(option.value ?? "");
      return { value, label: option.label || readableValue(value) };
    }
    const value = String(option ?? "");
    return { value, label: readableValue(value) };
  });
}

export function alternativeDefaultLabel(choice) {
  const match = alternativeOptions(choice).find((option) => option.value === String(choice?.default ?? ""));
  return match ? match.label : readableValue(String(choice?.default ?? ""));
}

function readableValue(value) {
  const words = value.replaceAll("_", " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : words;
}
