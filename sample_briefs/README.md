# Sample Eviction Defense Briefs for Argument Gym Testing & Validation

This directory contains four realistic Microsoft Word (`.docx`) briefs representing residential eviction defense filings across different Ohio municipal jurisdictions. They are designed for testing and validating the **CLARA Argument Gym** adversarial review pipeline and deterministic drafting validation checks.

---

## Summary of Documents

| File | Jurisdiction | Type | Quality | Key Target Checks / Expected Gym Findings |
| :--- | :--- | :--- | :--- | :--- |
| [`01_good_motion_to_dismiss_notice_defect_franklin_county.docx`](file:///home/quinten/agentic_housing_drafting/sample_briefs/01_good_motion_to_dismiss_notice_defect_franklin_county.docx) | Franklin County Municipal Court (Columbus) | Motion to Dismiss (Civ.R. 12(B)(1)) | **Good (Compliant)** | • Proper caption, 12pt Times New Roman, double-spaced.<br>• Full pleading of R.C. 1923.04 notice defect elements.<br>• Detailed computation under R.C. 1.14 & Civ.R. 6(A).<br>• 0 form errors, valid certificate of service. |
| [`02_good_trial_brief_retaliation_and_habitability_cleveland.docx`](file:///home/quinten/agentic_housing_drafting/sample_briefs/02_good_trial_brief_retaliation_and_habitability_cleveland.docx) | Cleveland Municipal Court, Housing Division | Tenant Trial Brief | **Good (Compliant)** | • Full pleading of R.C. 5321.02 (retaliatory eviction) and R.C. 5321.04 (habitability breach).<br>• Concrete record dates, inspector citations, and temporal proximity.<br>• 0 form errors, robust adversarial posture. |
| [`03_defective_brief_unsupported_retaliation_and_habitability_hamilton.docx`](file:///home/quinten/agentic_housing_drafting/sample_briefs/03_defective_brief_unsupported_retaliation_and_habitability_hamilton.docx) | Hamilton County Municipal Court (Cincinnati) | Brief in Opposition | **Defective (Substantive & Rule Support)** | • Invokes R.C. 5321.02 but fails to plead protected activity, landlord knowledge, or timeline.<br>• Invokes R.C. 5321.04 with vague generalities, no notice, no repair window.<br>• Open to fatal opponent attack (rent withholding without R.C. 5321.07 escrow). |
| [`04_defective_brief_procedural_and_form_errors_akron.docx`](file:///home/quinten/agentic_housing_drafting/sample_briefs/04_defective_brief_procedural_and_form_errors_akron.docx) | Akron Municipal Court (Summit County) | Motion to Dismiss | **Defective (Form, Language & Citations)** | • 8+ unresolved placeholders (`[Case Number TBD]`, `[Insert Date...]`, `____`).<br>• Paragraph numbering jump (1, 2 → 5).<br>• 10+ legal typos ("statue", "judgement", "plaintif", "breech", "landlorde", "defendent").<br>• Missing Certificate of Service; cites California code instead of Ohio law. |

---

## Detailed Breakdown of Each Sample Brief

### 1. Good Brief: Motion to Dismiss for Notice Defect (`01_good_motion_to_dismiss_notice_defect_franklin_county.docx`)
* **Court**: Franklin County Municipal Court, Environmental Division (*Oakwood Manor Apartments, LLC v. Marcus Vance*, Case No. 2024 CVG 014892).
* **Legal Ground**: Civ.R. 12(B)(1) motion to dismiss for lack of subject-matter jurisdiction due to defective and premature R.C. 1923.04 notice.
* **Why it is "Good"**:
  1. **Strict Ohio Authority**: Cites controlling Ohio precedents (*Bella Vista Apts. v. Herzner*, *Voyager Village Ltd. v. Lehman*, *Mastics v. McGrew*, *Showe Mgt. Corp. v. Hazelbaker*).
  2. **Rule Audit Compliance**: Fully satisfies every element of R.C. 1923.04 (identifies notice, quotes omission of statutory forfeiture language, provides exact service and filing dates).
  3. **Procedural Rigor**: Demonstrates proper application of Civ.R. 6(A) and R.C. 1.14 excluding the service date and weekend days to prove premature filing.
  4. **Court Formatting**: Standard Ohio two-column caption, double line spacing, 1-inch margins, complete signature block with Supreme Court bar registration number, and formal Certificate of Service.

### 2. Good Brief: Trial Brief on Retaliation & Habitability (`02_good_trial_brief_retaliation_and_habitability_cleveland.docx`)
* **Court**: Cleveland Municipal Court, Housing Division (*Superior Property Management Co. v. Elena Rostova*, Case No. 2024-CVG-008912).
* **Legal Ground**: Trial brief asserting statutory affirmative defense of retaliatory eviction under R.C. 5321.02 and breach of landlord obligations under R.C. 5321.04(A)(1), (A)(2), (A)(4) and Cleveland Codified Ordinances.
* **Why it is "Good"**:
  1. **Complete Retaliation Elements**: Establishes all 4 prongs of R.C. 5321.02: (1) protected activity (complaint to Cleveland Dept. of Building and Housing), (2) landlord knowledge (city inspector notice), (3) adverse action (eviction notice), and (4) temporal causation (eviction notice served 6 days after code violation notice).
  2. **Complete Habitability Elements**: Identifies specific statutory subsections, describes exact defects (raw sewage and lack of heat), alleges prior written notices, accounts for reasonable time to repair, and requests authorized relief (rent abatement).
  3. **High Adversarial Resilience**: Anticipates opponent counter-arguments, anchors assertions in referenced exhibits (city inspection report and certified letters), and includes a valid Certificate of Service.

### 3. Defective Brief: Substantive Support Failure (`03_defective_brief_unsupported_retaliation_and_habitability_hamilton.docx`)
* **Court**: Hamilton County Municipal Court (*Riverfront Holdings LP v. Jordan Blake*, Case No. 24CV-19342).
* **Legal Ground**: Brief in opposition attempting to claim retaliation (R.C. 5321.02) and bad conditions (R.C. 5321.04).
* **Deliberate Defects Introduced for Argument Gym**:
  1. **Unmet Statutory Elements in Rule Audit (`rule_elements`)**:
     - Under R.C. 5321.02: Merely alleges defendant "was vocal in the neighborhood", failing to plead any protected statutory act (complaint to code agency, written notice, or tenant union membership), fails to plead landlord knowledge of protected activity, and provides no timeline.
     - Under R.C. 5321.04: Vaguely alleges the space was "unsatisfactory and unpleasant" without identifying specific code duties, fails to allege prior notice was given to the landlord, and fails to allege an opportunity to repair.
  2. **Severe Adversarial Vulnerability (`adversarial`)**:
     - Alleges tenant voluntarily withheld rent as self-help. Under Ohio law (*Smith v. Wright*), self-help rent withholding is barred unless deposited into escrow under R.C. 5321.07. Opposing counsel will easily dispose of this defense.
  3. **Unanchored Factual Claims**: Mentions "inspections were done" without citing a date, agency, report number, or attaching any record document.

### 4. Defective Brief: Form, Typos, Placeholders & Citation Defects (`04_defective_brief_procedural_and_form_errors_akron.docx`)
* **Court**: Akron Municipal Court (*Apex Residential LLC v. Keisha Taylor*).
* **Legal Ground**: Purported motion to dismiss.
* **Deliberate Defects Introduced for Argument Gym**:
  1. **Unfilled Placeholders (`pleading_form` / E1040)**:
     - Contains multiple unresolved brackets and placeholders: `[Case Number TBD]`, `[Insert Date of Notice]`, `____ days after service`, `[Attorney Name]`, `TBD`, `XXX-XXXX`.
  2. **Numbering & Sequence Gap (`pleading_form` / W1010)**:
     - Numbered paragraphs jump from `1.` and `2.` directly to `5.`.
  3. **Missing Mandatory Elements (`court_formatting` & `pleading_form`)**:
     - **Missing Certificate of Service** at the end.
     - Non-compliant formatting (10pt font, single line spacing).
     - References "Exhibit A" and "Exhibit B" without attaching them.
  4. **Legal Spelling and Confused Words (`confused_words` & `grammar`)**:
     - Contains common legal errors caught by `legal-language.yaml`: `"statue"` (for statute), `"judgement"` (for judgment), `"plaintif"`, `"defendent"`, `"tenent"`, `"landlorde"`, `"breech"` (for breach), `"ARGUEMENT"`.
     - Doubled words (`"The the landlord"`), missing space after periods (`"§ 1161.The plaintif"`).
  5. **Invalid Legal Authority & Internal Contradictions**:
     - Cites California Civil Code (`Cal. Civ. Code § 1161`) rather than Ohio Revised Code.
     - Directly contradicts itself (claims in Paragraph 1 that tenant never received notice, then claims in Paragraph 5 that a 3-day notice was served on a specific date).

---

## How to Test and Run Against the Argument Gym

You can test these documents using either the Python test suite, backend CLI commands, or by uploading them directly into the Argument Gym UI workspace:

### 1. Running Automated Ingestion and Validation via Python
```bash
.venv/bin/python -c "
from pathlib import Path
from apps.argument_gym import ingestion, rule_audit
from apps.validation import pleading_form, language

for path in sorted(Path('sample_briefs').glob('*.docx')):
    print(f'=== Testing {path.name} ===')
    res = ingestion.ingest_upload(path.read_bytes(), filename=path.name)
    print('Formatting:', res['metadata']['formatting'])
    print('Units:', len(res['metadata']['units']))
    audits, _ = rule_audit.run_rule_audit(res['text'], [], jurisdiction='Ohio')
    print('Rule Audits:', [f\"{a['citation']}: {a['verdict']}\" for a in audits])
    form_findings = pleading_form.check_pleading_form(res['text'], pleading_type='motion')
    print('Form findings:', len(form_findings))
    lang_findings = language.check_language(res['text'])
    print('Language findings:', len(lang_findings))
"
```

### 2. Testing via the Argument Gym Web Interface
1. Launch the backend (`.venv/bin/python backend/manage.py runserver`) and frontend (`npm start` in `frontend/`).
2. Navigate to **Argument Gym** -> **New Workspace**.
3. Upload any of the `.docx` files from `sample_briefs/`.
4. Run the Gym pipeline to observe the Opponent attacks, Judge rulings, Coach recommendations, Rule Audit element breakdowns, and deterministic filing-format / language findings.

### 3. Regenerating Sample Briefs
To re-generate or modify the sample briefs, run:
```bash
.venv/bin/python scripts/generate_sample_briefs.py
```
