import React from "react";
import { CheckCircle2, HelpCircle, Loader2, PenLine, Sparkles } from "lucide-react";

import { planQuestionsForReview, questionHelp, unansweredPlanQuestions } from "./planQuestions.js";
import { PanelHeading } from "./PanelHeading.jsx";

function updateDocument(plan, documentId, patch) {
  return {
    ...plan,
    document_items: (plan.document_items || []).map((item) => (
      item.id === documentId ? { ...item, ...patch } : item
    )),
  };
}

function QuestionCard({ question, onChange, onSkip }) {
  const helpId = `question-help-${question.documentId}-${question.index}`;
  const suggested = question.answer_source === "case_record" && Boolean(question.answer?.trim());
  return (
    <div className="missing-info-item">
      <div className="missing-info-item-heading">
        {question.ai_completable ? <PenLine size={15} /> : <HelpCircle size={15} />}
        <strong>{question.question}</strong>
      </div>
      {question.context && (
        <small className="muted missing-info-context">Template wording: “{question.context}”</small>
      )}
      {(question.documentTitle || question.section) && (
        <small className="muted">{[question.documentTitle, question.section].filter(Boolean).join(" · ")}</small>
      )}
      {suggested && (
        <small className="missing-info-basis">
          <Sparkles size={13} /> Drafted from the case record{question.basis ? `: ${question.basis}` : ""}. Check it before you continue.
        </small>
      )}
      {question.answer_source === "template_default" && (
        <small className="missing-info-basis">{question.basis}</small>
      )}
      <label className="field missing-info-answer">
        <span>{question.ai_completable ? "Text for the draft" : "Answer"}</span>
        <textarea className="form-control"
          aria-describedby={helpId}
          rows={4}
          value={question.answer || ""}
          onChange={(event) => onChange({ answer: event.target.value, not_needed: false, answer_source: "reviewer" })}
        />
        <small className="muted" id={helpId}>{questionHelp(question)}</small>
      </label>
      <div className="button-row compact">
        <button className="btn btn-light" type="button" onClick={onSkip}>
          {question.ai_completable
            ? "Skip — leave this to the draft"
            : question.optional ? "Skip — not needed" : "Skip — leave as placeholder"}
        </button>
      </div>
    </div>
  );
}

export function DraftQuestionsReview({ plan, busy, onPlanChange, onBack, onContinue }) {
  const questions = planQuestionsForReview(plan);
  const asked = questions.filter((question) => !question.ai_completable);
  const drafted = questions.filter((question) => question.ai_completable);
  const unansweredQuestions = unansweredPlanQuestions(plan);

  function updateMissing(documentId, index, patch) {
    const item = (plan.document_items || []).find((entry) => entry.id === documentId);
    if (!item) return;
    const missing = [...(item.missing_information || [])];
    missing[index] = { ...missing[index], ...patch };
    onPlanChange(updateDocument(plan, documentId, { missing_information: missing }));
  }

  function cardProps(question) {
    return {
      question,
      onChange: (patch) => updateMissing(question.documentId, question.index, patch),
      onSkip: () => updateMissing(question.documentId, question.index, { not_needed: true, required_for_generation: false }),
    };
  }

  return (
    <section className="step-screen">
      <div className="panel">
        <PanelHeading
          eyebrow="Drafting questions"
          title="Fill the blanks the case record could not"
          description={<>These are the template&apos;s blanks that are still open. Anything answered from the case record is shown with its basis so you can correct it; anything left unanswered appears as placeholder text (like <code>[Plaintiff Name]</code>) in the draft.</>}
        />
        {questions.length === 0 ? (
          <div className="empty-state compact">
            <CheckCircle2 size={20} />
            <strong className="empty-state-title">Nothing left to answer</strong>
            <p>You're ready to generate the draft.</p>
          </div>
        ) : (
          <>
            {asked.length > 0 && (
              <div className="missing-info-group">
                <h4>Only you can answer these</h4>
                <div className="missing-info-list">
                  {asked.map((question) => (
                    <QuestionCard key={`${question.documentId}-${question.index}`} {...cardProps(question)} />
                  ))}
                </div>
              </div>
            )}
            {drafted.length > 0 && (
              <div className="missing-info-group">
                <h4>The draft will write these</h4>
                <p className="muted">
                  Directions the template author left for whoever drafts the document. Leave them
                  blank and the draft writes them from the selected facts, or write your own text.
                </p>
                <div className="missing-info-list">
                  {drafted.map((question) => (
                    <QuestionCard key={`${question.documentId}-${question.index}`} {...cardProps(question)} />
                  ))}
                </div>
              </div>
            )}
          </>
        )}
        <div className="button-row step-actions">
          <button className="btn btn-light" type="button" onClick={onBack}>Back to plan</button>
          <button className="btn btn-primary" type="button" disabled={busy || unansweredQuestions.length > 0} onClick={onContinue}>
            {busy ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />} Continue to draft
          </button>
        </div>
      </div>
    </section>
  );
}
