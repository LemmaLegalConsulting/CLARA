# Demo briefs

Synthetic briefs for showing a gym check working in a live installation. Each
file is only the brief, so it can be uploaded as is; this README says what a
working run should show. The parties, addresses, and dates are invented.

They are not a benchmark. The answer to the question each one raises may still
be changing, which is the point: they exercise what the library cannot answer.
For known-answer comparisons, use `seeded-flaws.yaml`.

## `hud-30-day-notice-web-lookup.md` -- web lookup

A public housing nonpayment defense that relies on the 30-day notice in
24 C.F.R. 966.4(l)(3)(i)(A), then says HUD "reaffirmed and strengthened" it at
91 Fed. Reg. 9453 (February 2026) and has not altered it since.

The library holds 24 C.F.R. 966.4. Its eCFR notice names an amendment at
91 FR 9453 and a delay at 91 FR 12301, but the library does not hold either
Federal Register document. As reported by NAHRO, LeadingAge, NLIHC, and
the Federal Register as of spring 2026, 91 FR 9453 is HUD's interim rule
*revoking* the nonpayment notice requirement. Its effective date was then
postponed, and the change was reissued as a proposed rule. If that is still
true, the brief's conclusion may survive (the 30-day text is in force) while
its description of the 2026 rule is wrong. Confirm the current status before
relying on any of this.

To run it: create a gym workspace, upload the file as the brief, leave the
default checks on, and run.

What a working run shows:

- The **Applicability** section lists 91 Fed. Reg. 9453 (and any other
  authority the argument map names that the library lacks) as not held.
- The **Web lookup** section reports that it ran, not "could not run".
  - It lists the questions it sent. None of them carries the defendant's name,
    the address, the unit, or a date from the facts.
  - Any question that does is listed as withheld, with the reason.
- Web findings appear under a "from web search" label, each with a source
  trust tier (official government, legal aid / nonprofit reference, or other
  web). They are absent from the main challenges, the coverage counts, and the
  judge's verdict.
- A finding resting only on legal-aid or other-web sources is marked as a lead
  to verify, not a conclusion.

If the section says "could not run", either the installation has
`AI_WEB_SEARCH_ENABLED` off or the author turned the check off for the session.
The run's check list says which.
