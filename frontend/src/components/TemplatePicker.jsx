import React from "react";

import { alternativeDefaultLabel, alternativeOptions, templateChoices } from "./templateChoices.js";

function fieldLabel(path) {
  const key = path.replace(/^fields\./, "");
  if (/^placeholder_\d+_\d+$/.test(key)) return "Additional template detail";
  return key.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function TemplatePicker({
  templates,
  selectedTemplateId,
  selectedBlockKeys,
  templateData,
  onTemplateChange,
  onBlockChange,
  onTemplateDataChange,
}) {
  const selectedTemplate = templates.find((template) => template.id === Number(selectedTemplateId));
  const templateFields = selectedTemplate?.metadata?.fields || [];
  const templateAlternatives = selectedTemplate?.metadata?.choices || [];

  function toggleBlock(key) {
    if (selectedBlockKeys.includes(key)) {
      onBlockChange(selectedBlockKeys.filter((item) => item !== key));
    } else {
      onBlockChange([...selectedBlockKeys, key]);
    }
  }

  return (
    <div className="template-picker">
      <label className="field">
        <span>Document template</span>
        <select className="form-select" value={selectedTemplateId || ""} onChange={(event) => onTemplateChange(event.target.value)}>
          {templateChoices(templates).map((choice) => (
            <option key={choice.value || "placeholder"} value={choice.value}>
              {choice.placeholder ? choice.label : `${choice.label} · ${choice.jurisdiction || "Any jurisdiction"}`}
            </option>
          ))}
        </select>
      </label>

      {templateAlternatives.length > 0 && (
        <div className="template-field-list">
          <h4>Either/or clauses</h4>
          <p className="muted">The template offers alternative wording here. The first option is used unless you choose another.</p>
          {templateAlternatives.map((choice) => (
            <label className="field" key={choice.name}>
              <span>{choice.label || choice.name}</span>
              <select className="form-select"
                value={templateData?.[choice.name] ?? ""}
                onChange={(event) => onTemplateDataChange({ ...templateData, [choice.name]: event.target.value })}
              >
                <option value="">Use the default ({alternativeDefaultLabel(choice)})</option>
                {alternativeOptions(choice).map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}

      {templateFields.length > 0 && (
        <div className="template-field-list">
          <h4>Template details</h4>
          <p className="muted">Unfilled details remain visibly bracketed in the draft so missing information is not silently omitted.</p>
          {selectedTemplate?.metadata?.applicability?.summary && (
            <p className="muted"><strong>Use when:</strong> {selectedTemplate.metadata.applicability.summary}</p>
          )}
          {templateFields.map((path) => {
            const key = path.replace(/^fields\./, "");
            const schema = selectedTemplate?.metadata?.fieldSchema?.[key] || {};
            const value = templateData?.[key] ?? (schema.type === "list" ? [] : "");
            const label = schema.label || fieldLabel(path);
            if (schema.type === "pronouns") {
              return (
                <label className="field" key={path}>
                  <span>{label}</span>
                  <select className="form-select"
                    value={value}
                    onChange={(event) => onTemplateDataChange({ ...templateData, [key]: event.target.value })}
                  >
                    <option value="">Use the client’s name</option>
                    <option value="she/her/hers">she/her/hers</option>
                    <option value="he/him/his">he/him/his</option>
                    <option value="they/them/theirs">they/them/theirs</option>
                    <option value="ze/zir/zirs">ze/zir/zirs</option>
                  </select>
                </label>
              );
            }
            if (schema.type === "list") {
              return (
                <label className="field" key={path}>
                  <span>{label}</span>
                  <textarea
                    className="form-control"
                    aria-describedby={`${key}-help`}
                    value={Array.isArray(value) ? value.join("\n") : value}
                    onChange={(event) => onTemplateDataChange({
                      ...templateData,
                      [key]: event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
                    })}
                  />
                  <small className="muted" id={`${key}-help`}>{schema.help || "One item per line."}</small>
                </label>
              );
            }
            if (schema.type === "select" && Array.isArray(schema.options)) {
              return (
                <label className="field" key={path}>
                  <span>{label}</span>
                  <select className="form-select"
                    value={value}
                    onChange={(event) => onTemplateDataChange({ ...templateData, [key]: event.target.value })}
                  >
                    <option value="">Select…</option>
                    {schema.options.map((option) => (
                      <option key={typeof option === "string" ? option : option.value} value={typeof option === "string" ? option : option.value}>
                        {typeof option === "string" ? option : option.label}
                      </option>
                    ))}
                  </select>
                </label>
              );
            }
            return (
              <label className="field" key={path}>
                <span>{label}</span>
                <input className="form-control"
                  value={value}
                  onChange={(event) => onTemplateDataChange({ ...templateData, [key]: event.target.value })}
                />
              </label>
            );
          })}
        </div>
      )}

      <div className="block-list">
        {selectedTemplate?.blocks?.map((block) => (
          <label key={block.key} className={`block-row ${block.required ? "required" : ""}`}>
            <input
              type="checkbox"
              checked={selectedBlockKeys.includes(block.key)}
              disabled={block.required}
              onChange={() => toggleBlock(block.key)}
            />
            <span>
              <strong>{block.label}</strong>
              <em>{block.required ? "Required" : "Optional"} · {block.aiFillMode.replaceAll("_", " ")}</em>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}
