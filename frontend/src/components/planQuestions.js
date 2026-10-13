/** Which of a draft plan's open blanks a reviewer sees, and which hold up the draft. */

export function planQuestions(plan) {
  return (plan?.document_items || []).flatMap((item) => (
    (item.missing_information || [])
      .map((missing, index) => ({ ...missing, documentId: item.id, documentTitle: item.title, index }))
  ));
}

/**
 * Questions the draft cannot answer for itself, so generation waits on them.
 *
 * A drafting direction the template author left behind ("[describe occupants]")
 * is the model's to carry out, so leaving it blank is a choice rather than an
 * omission and it never blocks the draft. Nor does an optional blank -- no
 * other occupants, no magistrate assigned yet -- which the template reads
 * well without.
 */
export function unansweredPlanQuestions(plan) {
  return planQuestions(plan).filter((missing) => (
    !missing.answer?.trim() && !missing.not_needed && !missing.ai_completable && !missing.optional
  ));
}

/** The help shown under a question's answer box. */
export function questionHelp(question) {
  if (question.ai_completable) return "Leave blank to let the draft write this from the selected facts.";
  if (question.optional) return "Optional. Leave blank if it does not apply; the document reads correctly without it.";
  return "Add the missing detail here.";
}

/** Every blank still worth a reviewer's eye, including the drafted suggestions. */
export function planQuestionsForReview(plan) {
  return planQuestions(plan).filter((missing) => !missing.not_needed);
}
