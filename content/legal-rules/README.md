# Legal rule profiles

One YAML file per legal rule that an advocate commonly invokes, listing the
**elements** the rule requires. An advocate who cites a rule has taken on its
elements; these files are what makes that auditable.

Each profile carries two things:

- **How to tell the rule was invoked.** Citation patterns and the phrases people
  use when they invoke it without citing it ("three-day notice").
- **What the rule requires.** One entry per element: what has to be shown,
  patterns that suggest the brief pleads it, and whether it needs support in the
  case record rather than only an assertion.

## Verification is part of the record

These elements are substantive law. A wrong element list is worse than none: it
tells an advocate their pleading is complete when it is not, or sends them
chasing an element the rule does not have.

- `verification: verified` — someone read the rule and the cases construing it,
  recorded the citation in `source`, and dated it in `verified_on`. Only then are
  unmet elements reported at error severity.
- `verification: unverified` — a starting point for someone who will check it.
  Findings are warnings and are labelled as coming from an unverified list.

**Every profile shipped in this repository is unverified.** They exist so the
audit runs and so the shape is clear, not so an office relies on them. Read the
statute, correct the elements, cite what you read, and mark it verified.

## Reusing the decision tables

Where a published `DecisionTable` row already encodes a rule's requirements, name
it instead of writing the list twice:

```yaml
decision_table_key: eviction_answer_issue_selection
decision_table_row: notice_defect
```

The row's `missing_facts` become elements, and the facts its conditions depend on
become elements too. Those merge with any elements declared in the file, so a
profile can add what the table does not carry.

## Managing profiles

Seeded by `.venv/bin/python backend/manage.py sync_content_library`, which never
overwrites a profile edited in Django admin (that sets `is_locally_edited`). Use
`--update-legal-rules` to intentionally apply changed files. Day-to-day editing
is in Django admin under **Legal rule profiles**.

## Schema

```yaml
schema_version: 1
slug: rc-1923-04-notice
name: Notice to leave the premises
citation: R.C. 1923.04
rule_type: statute            # statute | civil_rule | local_rule | doctrine
jurisdiction: Ohio
summary: One or two sentences on what the rule requires.

citation_patterns:            # case-insensitive regular expressions
  - "R\\.?C\\.?\\s*1923\\.04"
aliases:                      # phrases that invoke the rule without citing it
  - three-day notice

verification: unverified
source: ""
source_url: ""
verified_on: ""

elements:
  - id: notice_served
    label: A notice was served before the complaint was filed
    requirement: What has to be shown for this element to be met.
    severity: error
    needs_record_support: true   # an assertion alone does not satisfy it
    patterns:                    # suggest the brief pleads this element
      - "served .{0,40}notice"
    contradiction_patterns:      # the brief's own words ruling the element out
      - "no notice was (served|given)"
    note: Anything a reader should know about how this element is usually met.
```

`contradiction_patterns` are the opposite of `patterns`. A pleading pattern
that fails to match proves nothing; a contradiction pattern that matches is the
brief describing facts the element excludes -- "a market-rate unit with no
federal subsidy" in a brief relying on the CARES Act -- and is reported as a
finding even when a model read the element as pleaded.

A profile whose `jurisdiction` is `United States` (or `Federal`) is federal law
and is checked in a brief filed anywhere.
