"""Recognizing a citation in a research query and saying what it could look like in text.

Citation search has to be predictable: an advocate who types ``R.C. 5321.04``
expects every section that carries it, and one who types ``2020-Ohio-1234``
expects that decision, not decisions that merely share the year. Neither is a
relevance judgement, so neither is left to ranking.

A citation is therefore parsed into a canonical form plus the literal spellings
the same authority is written under. The variants are what retrieval searches
for, because a corpus written by many hands spells one section
``R.C. 5321.04``, ``O.R.C. 5321.04``, ``Ohio Rev. Code 5321.04`` and
``Section 5321.04`` on different pages. Matching the canonical form alone would
report the missing spellings as an absence of law.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Citation:
    """One citation, and the two different things it asks for.

    ``variants`` is the requirement: the spellings of the authority itself,
    any of which a document must literally contain to be returned.

    ``refinements`` is a preference, not a requirement, and the distinction is
    the honest answer to a question the code used to get wrong quietly. A
    reader who types ``R.C. 5321.04(A)(2)`` is pointing at one division of a
    section -- but the section's own text prints the number once in its
    heading and the division separately as ``(A)(2)``, so requiring the
    joined-up string would exclude the statute the reader is asking for.
    Requiring the section and ranking the division higher returns that section
    first and the paragraphs discussing the division after it. What must not
    happen is claiming the narrower thing was required when it was not, so the
    parse reports both.
    """

    kind: str
    normalized: str
    matched_text: str
    variants: tuple = field(default_factory=tuple)
    subdivision: str = ""
    refinements: tuple = field(default_factory=tuple)

    def to_dict(self):
        return {
            "kind": self.kind,
            "normalized": self.normalized,
            "matchedText": self.matched_text,
            "variants": list(self.variants),
            "subdivision": self.subdivision,
            # Spelt out because "citations are requirements" is the promise
            # this endpoint makes, and for a subdivision it is only half true.
            "required": list(self.variants[:1]) or [self.normalized],
            "ranksHigher": list(self.refinements),
        }


def _unique(values):
    return tuple(dict.fromkeys(value for value in values if value))


def _revised_code(match):
    section = match.group("section")
    subdivision = (match.group("subdivision") or "").replace(" ", "")
    normalized = f"R.C. {section}{subdivision}"
    # How a division is written where it is discussed rather than enacted.
    refinements = _unique([
        f"{section}{subdivision}",
        f"R.C. {section}{subdivision}",
        f"division {subdivision} of section {section}",
        subdivision,
    ]) if subdivision else ()
    return Citation(
        kind="revised_code",
        normalized=normalized,
        matched_text=match.group(0).strip(),
        subdivision=subdivision,
        refinements=refinements,
        # The bare section number is a variant because statutory text cites its
        # neighbours as "division (B) of section 5321.04", with no reporter
        # prefix anywhere on the page.
        variants=_unique([
            f"R.C. {section}",
            f"R.C. §{section}",
            f"R.C. § {section}",
            f"O.R.C. {section}",
            f"Ohio Rev. Code {section}",
            f"Ohio Revised Code {section}",
            f"Revised Code {section}",
            f"section {section}",
            section,
        ]),
    )


def _ohio_public_domain(match):
    year, number = match.group("year"), match.group("number").lstrip("0") or "0"
    normalized = f"{year}-Ohio-{number}"
    return Citation(
        kind="ohio_public_domain",
        normalized=normalized,
        matched_text=match.group(0).strip(),
        variants=_unique([normalized, f"{year} Ohio {number}", f"{year}-ohio-{number}"]),
    )


def _reporter(match):
    volume, series, page = match.group("volume"), match.group("series"), match.group("page")
    reporter = re.sub(r"\s+", " ", match.group("reporter").strip())
    edition = (series or "").strip()
    canonical = f"{volume} {reporter}{edition} {page}" if edition else f"{volume} {reporter} {page}"
    return Citation(
        kind="reporter",
        normalized=re.sub(r"\s+", " ", canonical),
        matched_text=match.group(0).strip(),
        variants=_unique([
            canonical,
            f"{volume} {reporter} {edition} {page}".replace("  ", " ") if edition else canonical,
            f"{volume} {reporter.replace('. ', '.')}{edition} {page}",
        ]),
    )


def _federal_code(match):
    title, section = match.group("title"), match.group("section")
    body = match.group("body").upper().replace(" ", "")
    label = {"USC": "U.S.C.", "U.S.C.": "U.S.C.", "CFR": "C.F.R.", "C.F.R.": "C.F.R."}.get(body, body)
    normalized = f"{title} {label} {section}"
    return Citation(
        kind="federal_code",
        normalized=normalized,
        matched_text=match.group(0).strip(),
        variants=_unique([
            normalized,
            f"{title} {label} § {section}",
            f"{title} {label} §{section}",
            f"{title} {label.replace('.', '')} {section}",
        ]),
    )


def _statutory_cross_reference(match):
    """``section 12491(a) of title 34`` -- how the U.S. Code cites itself.

    The subdivision is a refinement, not part of the requirement: the section's
    own text prints its number once, in its heading, and the subdivision
    separately as ``(a)``.
    """
    title = match.group("title")
    section = match.group("section").replace("\u2013", "-").replace("\u2014", "-")
    subdivision = (match.group("subdivision") or "").replace(" ", "")
    normalized = f"{title} U.S.C. {section}"
    return Citation(
        kind="federal_code",
        normalized=normalized,
        matched_text=match.group(0).strip(),
        subdivision=subdivision,
        refinements=_unique([f"{normalized}{subdivision}", subdivision]) if subdivision else (),
        variants=_unique([
            normalized,
            f"{title} U.S.C. § {section}",
            f"{title} U.S.C. §{section}",
            f"{title} USC {section}",
            f"section {section} of title {title}",
        ]),
    )


def _administrative_code(match):
    """``Ohio Adm.Code 4112-5-07`` and the spellings briefs and agencies use for it."""
    rule = match.group("rule")
    subdivision = (match.group("subdivision") or "").replace(" ", "")
    normalized = f"Ohio Adm.Code {rule}"
    return Citation(
        kind="administrative_code",
        normalized=normalized,
        matched_text=match.group(0).strip(),
        subdivision=subdivision,
        refinements=_unique([f"{normalized}{subdivision}", subdivision]) if subdivision else (),
        variants=_unique([
            normalized,
            f"Ohio Adm. Code {rule}",
            f"Ohio Administrative Code {rule}",
            f"O.A.C. {rule}",
            f"OAC {rule}",
            f"rule {rule}",
            rule,
        ]),
    )


# Ordered most specific first: a public-domain cite contains a bare year, and a
# reporter cite contains a bare volume number, so the loose patterns must not
# see the text first.
_PATTERNS = (
    (
        re.compile(
            r"\b(?:ohio\s+adm(?:in(?:istrative)?)?\.?\s*code|o\.\s?a\.\s?c\.|oac|adm\.\s*code)\s*(?:§+\s*)?"
            r"(?P<rule>\d{1,4}(?::\d{1,2})?-\d{1,2}(?:\.\d+)?-\d{1,3}(?:\.\d+)?)"
            r"(?P<subdivision>(?:\s*\([A-Za-z0-9]{1,3}\))*)",
            re.I,
        ),
        _administrative_code,
    ),
    (
        re.compile(
            r"\bsection\s+(?P<section>\d+[a-z]{0,2}(?:[-\u2013\u2014]\d+[a-z]?)?)"
            r"(?P<subdivision>(?:\([A-Za-z0-9]{1,3}\))*)\s+of\s+title\s+(?P<title>\d{1,2})\b",
            re.I,
        ),
        _statutory_cross_reference,
    ),
    (re.compile(r"\b(?P<year>(?:19|20)\d{2})[-\s]ohio[-\s](?P<number>\d{1,6})\b", re.I), _ohio_public_domain),
    (
        re.compile(
            r"\b(?:o\.?\s?r\.?\s?c\.?|ohio\s+rev(?:ised)?\.?\s+code(?:\s+ann\.?)?|r\.\s?c\.)\s*"
            r"(?:§+\s*)?(?P<section>\d{1,4}\.\d{1,3})(?P<subdivision>(?:\s*\([A-Za-z0-9]{1,3}\))*)",
            re.I,
        ),
        _revised_code,
    ),
    (
        re.compile(
            r"\b(?P<title>\d{1,2})\s+(?P<body>u\.?\s?s\.?\s?c\.?|c\.?\s?f\.?\s?r\.?)\s*"
            r"(?:§+\s*)?(?P<section>\d+[a-z]?(?:\.\d+)?(?:\([a-z0-9]{1,3}\))*)",
            re.I,
        ),
        _federal_code,
    ),
    (
        re.compile(
            r"\b(?P<volume>\d{1,4})\s+(?P<reporter>ohio\s+st\.?|ohio\s+app\.?|ohio\s+misc\.?|ohio|"
            r"n\.\s?e\.|f\.\s?supp\.?|f\.|u\.\s?s\.)\s*(?P<series>\d\s?d|\d\s?th)?\s+(?P<page>\d{1,5})\b",
            re.I,
        ),
        _reporter,
    ),
)


def _matches(text):
    """Each citation with its ``(start, end)``, longest match first, without overlaps."""
    consumed = []
    for pattern, build in _PATTERNS:
        for match in pattern.finditer(str(text or "")):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in consumed):
                continue
            consumed.append(span)
            yield build(match), span


def find_citations(text):
    """Every citation in ``text``, longest match first, without overlaps."""
    return [citation for citation, _span in _matches(text)]


def find_citation_spans(text):
    """Every citation in ``text`` with its ``(start, end)``, in reading order."""
    return sorted(_matches(text), key=lambda item: item[1][0])


def strip_citations(text, citations):
    """Remove matched citation text so its digits do not also rank as loose terms."""
    remaining = str(text or "")
    for citation in citations:
        remaining = remaining.replace(citation.matched_text, " ")
    return remaining
