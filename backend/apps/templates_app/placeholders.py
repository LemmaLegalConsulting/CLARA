"""Turn fill-in language into Jinja bindings without disturbing run formatting.

Advocates mark "you must supply this" three ways in the maintained originals:
square brackets, a run of underscores, and yellow highlighting. All three are
converted here so a prepared template keeps the original wording and formatting
and varies only where the author intended it to vary.

Substitution happens over the paragraph's concatenated text and is written back
run by run. Placeholders regularly straddle runs -- Word splits `[DATE] at
[TIME]` into `[`, `DATE] at [TIME]`, `.` after a spell-check pass -- so a
run-at-a-time regex would miss most of them. Rebuilding run by run is what keeps
bold, italics, underline, and superscript intact on the surrounding literal
text, which paragraph-level rewriting destroys.
"""

from __future__ import annotations

from copy import deepcopy
import re
from dataclasses import dataclass, field

from django.utils.text import slugify


BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
UNDERSCORE_RE = re.compile(r"_{3,}")
# "202__" and "20__" are year stubs; the digits are part of the placeholder.
YEAR_STUB_RE = re.compile(r"\b20\d?_{2,}")
JINJA_TOKEN_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")

EDITORIAL_BRACKETS = {"her", "his", "or", "s", "section", "x", "sic", "and"}
PLACEHOLDER_CUE_RE = re.compile(
    r"address|applicant|application|attorney|bar|case|caption|client|copy|count|court|date|deadline|"
    r"defendant|describe|document|email|eviction|fax|filing|hearing|housing|insert|judge|landlord|lease|list|magistrate|"
    r"mail|month|name|notice|number|occupant|opposing|payment|phone|plaintiff|premises|program|rent|"
    r"section|select|signature|subsidy|term|time|tenancy|title|voucher|what|when|where|who|why",
    re.I,
)
# Bracketed text that instructs the drafter rather than naming a value.
INSTRUCTION_CUE_RE = re.compile(
    r"\b(?:insert|describe|synopsis|summari[sz]e|explain|list|copy from|state|detail|anything|"
    r"what|who|when|where|how)\b",
    re.I,
)

PLACEHOLDER_ALIASES = {
    "client name": "defendant",
    "defendant": "defendant",
    "defendant name": "defendant",
    "attorney name": "advocate_name",
    "attorney name & bar no": "advocate_name_and_bar",
    "attorney name and bar no": "advocate_name_and_bar",
    "insert signature block": "advocate_signature_block",
    "signature block": "advocate_signature_block",
    "email": "advocate_email",
    "attorney email": "advocate_email",
    "phone": "advocate_phone",
    "phone number": "advocate_phone",
    "attorney telephone": "advocate_phone",
    "case number": "case_number",
    "case no.": "case_number",
    "case name": "case_caption",
    "plaintiff": "fields.plaintiff_name",
    "plaintiff's email": "fields.plaintiff_email",
    "plaintiff's address": "fields.plaintiff_address",
    "other occupants": "fields.other_occupants",
    "copy from noa": "fields.service_recipients",
    # The wrapper's "help with your [eviction/housing issue]" names what the
    # case is about, which the case record already knows.
    "eviction/housing issue": "matter_subject",
    "eviction/housing matter": "matter_subject",
    "pha": "fields.housing_authority",
    "magistrate": "fields.magistrate",
}
# "date", "time", and "address" are deliberately absent: which date or address a
# placeholder means depends on the sentence around it, so they are resolved by
# context below rather than by a fixed alias.

CONTEXT_FIELD_HINTS = (
    # Before "hearing": "the first hearing in this matter was held on [DATE]"
    # is a past hearing, not the one the case is set for.
    ("was held", "date", "fields.prior_hearing_date"),
    ("held on", "date", "fields.prior_hearing_date"),
    ("hearing", "date", "fields.hearing_date"),
    # "…is set for the virtual general call docket on [DATE]" is the hearing.
    ("docket", "date", "fields.hearing_date"),
    ("served", "date", "fields.service_date"),
    ("service", "date", "fields.service_date"),
    ("certify", "date", "fields.service_date"),
    ("move-in", "date", "fields.move_in_date"),
    ("moved in", "date", "fields.move_in_date"),
    ("filed", "date", "fields.filing_date"),
    ("filing", "date", "fields.filing_date"),
    ("terminate", "date", "fields.termination_date"),
)

# Wording that puts a blank on the signature rule rather than in a sentence.
# "/s/" is deliberately absent: it introduces the signer's name on the same
# line, so a blank after it is the name, not the whole block.
SIGNATURE_CUE_RE = re.compile(
    r"respectfully submitted|attorney for|counsel for|signature of", re.I
)

# A line a person signs by hand: an affiant, a notary, a witness. The advocate's
# own signature rule is SIGNATURE_CUE_RE above and becomes the signature block.
SIGNER_CUE_RE = re.compile(r"notary|affiant|signature|witness|sign here", re.I)
# A jurat is completed by the notary at the signing -- the day, the month, the
# year -- so its blanks stay blank lines for ink, not prompts to type ahead.
JURAT_CUE_RE = re.compile(r"sworn to|subscribed|before me", re.I)
# The fewest underscores that read as a rule to sign on rather than a gap in a
# sentence.
SIGNING_RULE_MIN = 10
# "this ___ day of ___": two blanks with one sentence, so they need two names.
DAY_BLANK_FOLLOWS_RE = re.compile(r"^\s*(?:st|nd|rd|th)?\s*day\s+of\b", re.I)
MONTH_BLANK_PRECEDES_RE = re.compile(r"(_{3,})\s*(?:st|nd|rd|th)?\s*day\s+of\s*$", re.I)
# A party's role said about someone ("I am the Defendant") is the author's
# wording, not that party's name.
PARTY_ROLES = {"defendant", "plaintiff", "tenant", "landlord", "petitioner", "respondent", "appellant", "appellee"}
ARTICLE_BEFORE_RE = re.compile(r"\b(?:the|a|an)\s*$", re.I)
AFFIANT_CUE_RE = re.compile(r"sworn|depose|affirm", re.I)

# A highlighted span this long with no fill-in cue is alternative wording the
# advocate keeps or deletes, not a value to supply.
OPTIONAL_PROSE_MIN_WORDS = 8


@dataclass
class ParagraphConversion:
    """What a paragraph contributed to the template's variable surface."""

    fields: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    instructions: list[str] = field(default_factory=list)
    changed: bool = False


# Sentence scaffolding that names nothing. "The 3-Day Notice is from ____"
# should yield `notice_from`, not `the_3_day_notice_is`.
NAME_STOPWORDS = {
    "a", "an", "and", "at", "but", "by", "for", "from", "in", "is", "it", "of",
    "on", "or", "so", "than", "that", "the", "then", "to", "was", "were", "with",
    "you", "your", "i", "we", "they", "this", "these", "there",
}


def _field_name(label: str, fallback: str) -> str:
    clean = re.sub(r"[^\w\s-]", " ", label).strip()
    name = slugify(clean).replace("-", "_") or fallback.replace("-", "_")
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = fallback.replace("-", "_")
    parts = [part for part in name.split("_") if part]
    # Drop leading filler, but never everything: a label made only of stopwords
    # still has to produce an identifier.
    meaningful = [part for part in parts if part not in NAME_STOPWORDS]
    parts = meaningful or parts
    # Long instruction text makes an unusable identifier; keep the leading terms.
    name = "_".join(parts[:4])
    return f"field_{name}" if name[:1].isdigit() else name


def field_name_for_label(label: str) -> str:
    """The `fields.<name>` a piece of fill-in text converts to.

    Exposed so a caller holding a block's recorded drafting instruction can find
    the field that instruction produced, and treat it as drafting work rather
    than as a question of fact.
    """
    return _field_name(label, "")


def _normalize(label: str) -> str:
    # Word autocorrects quotes, so alias lookups must see the straight forms.
    text = str(label or "").replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip(" .:_-[]()")


def looks_like_placeholder(label: str) -> bool:
    normalized = _normalize(label)
    lowered = normalized.casefold()
    if not normalized:
        return True
    if lowered in EDITORIAL_BRACKETS or normalized.isdigit() or len(normalized) == 1:
        return False
    if normalized.isupper() and len(normalized) <= 80:
        return True
    return bool(PLACEHOLDER_CUE_RE.search(normalized))


def is_instruction(label: str) -> bool:
    """True when bracketed text tells the drafter what to write."""
    normalized = _normalize(label)
    if not normalized:
        return False
    if "?" in normalized or "/" in normalized and len(normalized.split()) > 4:
        return True
    return bool(INSTRUCTION_CUE_RE.search(normalized)) and len(normalized.split()) >= 2


CONTEXT_LIMIT = 80
JINJA_EXPRESSION_RE = re.compile(r"\{\{\s*(?:[\w]+\.)*([\w]+)[^}]*\}\}")
JINJA_TAG_RE = re.compile(r"\{%.*?%\}")


def context_label(text: str, start: int, fallback: str) -> str:
    """The words leading up to a fill-in, used to name and type it."""
    prefix = text[:start].strip()
    # Abbreviations end sentences too ("Case No. ____"), so fall back through
    # empty trailing segments instead of losing the label to the split.
    segments = [segment.strip(" :,-–—()") for segment in re.split(r"[.;!?]", prefix)]
    for segment in reversed(segments):
        if 1 <= len(segment) <= CONTEXT_LIMIT:
            return segment
    return fallback


def clause_label(text: str, start: int) -> str:
    """The words of the blank's own clause, back to the nearest comma, bracket,
    or sentence end -- what a blank means when its sentence is too long to name
    it by. Only ever consulted where context_label found nothing, so no field
    that already has a name is renamed."""
    prefix = JINJA_TOKEN_RE.sub(",", text[:start])
    clause = re.split(r"[;!?,:()\[\]]", prefix)[-1]
    # Abbreviations: "Case No. ___" ends at the full stop the blank follows.
    parts = [part.strip() for part in re.split(r"\.(?=\s|$)", clause) if part.strip()]
    words = (parts[-1] if parts else "").split()
    if len(words) > 6 and len(parts) > 1:
        words = parts[-2].split()
    return " ".join(words[-6:]).strip(" -–—")


def date_context_label(text: str, start: int, fallback: str) -> str:
    """The words before a date, read so that they can say which date it is.

    Kept apart from context_label so that naming every other kind of fill-in is
    unchanged. Two differences: a fill-in already converted earlier in the
    sentence reads as its name -- "Served on {{ fields.plaintiff_name }} [DATE]"
    is about service, and the dots in the expression no longer split the
    sentence and lose the word that says so -- and a long sentence keeps the
    words nearest the blank instead of being discarded. "…a first cause hearing
    on the virtual general call docket on [DATE]" was read with no context at
    all, so its hearing date became the filing date.
    """
    prefix = JINJA_EXPRESSION_RE.sub(lambda match: match.group(1).replace("_", " "), text[:start])
    prefix = JINJA_TAG_RE.sub(" ", prefix).strip()
    segments = [segment.strip(" :,-–—()") for segment in re.split(r"[.;!?]", prefix)]
    for segment in reversed(segments):
        if not segment:
            continue
        if len(segment) > CONTEXT_LIMIT:
            tail = segment[-CONTEXT_LIMIT:]
            segment = tail.split(" ", 1)[1] if " " in tail else tail
        return segment
    return fallback


def following_label(text: str, end: int) -> str:
    """The words after a fill-in, up to the end of its clause."""
    suffix = JINJA_EXPRESSION_RE.sub(lambda match: match.group(1).replace("_", " "), text[end:])
    suffix = JINJA_TAG_RE.sub(" ", suffix)
    clause = re.split(r"[.;!?]", suffix, maxsplit=1)[0].strip(" :,-–—()")
    return clause[:CONTEXT_LIMIT].rsplit(" ", 1)[0] if len(clause) > CONTEXT_LIMIT else clause


def _says_something(text: str) -> bool:
    return any(word not in NAME_STOPWORDS for word in re.findall(r"[a-z0-9]+", str(text or "").lower()))


def placeholder_expression(
    label: str,
    fallback: str,
    *,
    context: str = "",
    nearby: str = "",
    following: str = "",
    date_context: str | None = None,
) -> str:
    """Map one fill-in to the Jinja path that should replace it.

    ``nearby`` is the wording of the surrounding lines. It is deliberately not
    used for naming -- a blank is named after the sentence it sits in, and
    borrowing the neighbours' words produces fields like `fields.sincerely` --
    but a blank standing alone on its own line has no sentence, and the lines
    around it are the only thing that says whether it is a signature rule.
    """
    normalized = _normalize(label).casefold()
    context_normalized = " ".join(str(context or "").lower().split())
    nearby_normalized = " ".join(str(nearby or "").lower().split())

    alias = PLACEHOLDER_ALIASES.get(normalized)
    if alias:
        return "{{ " + alias + " }}"

    if not normalized:
        if SIGNATURE_CUE_RE.search(context_normalized) or (
            context == fallback and SIGNATURE_CUE_RE.search(nearby_normalized)
        ):
            # The rule an advocate signs on, not a value anyone types.
            return "{{ advocate_signature_block }}"
        if "defendant" in context_normalized or "client" in context_normalized:
            return "{{ defendant }}"
        if "plaintiff" in context_normalized or "landlord" in context_normalized:
            return "{{ fields.plaintiff_name }}"
        if "magistrate" in context_normalized:
            return "{{ fields.magistrate }}"
        if "case no" in context_normalized or "case number" in context_normalized:
            return "{{ case_number }}"
        if "served" in context_normalized or "service" in context_normalized:
            return "{{ fields.service_date }}"
        if "attorney" in context_normalized or "counsel" in context_normalized:
            return "{{ advocate_name }}"
        if "ph." in context_normalized or "phone" in context_normalized or "tel" in context_normalized:
            return "{{ advocate_phone }}"
        return "{{ fields." + _field_name(context, fallback) + " }}"

    if normalized == "name":
        if "defendant" in context_normalized or "client" in context_normalized:
            return "{{ defendant }}"
        if "attorney" in context_normalized or "counsel" in context_normalized:
            return "{{ advocate_name }}"
        if AFFIANT_CUE_RE.search(following) and not _says_something(context if context != fallback else ""):
            # "I, [NAME], being duly sworn" -- the sentence before the blank is
            # only "I", which named the field `fields.i`.
            return "{{ fields.affiant_name }}"
        return "{{ fields." + _field_name(context, fallback) + " }}"

    if normalized in {"insert", "fill in", "blank"}:
        return "{{ fields." + _field_name(context, fallback) + " }}"

    if "address" in normalized and "attorney" not in normalized:
        if "opposing" in context_normalized or "landlord" in context_normalized or "plaintiff" in context_normalized:
            return "{{ fields.opposing_counsel_address }}"
        return "{{ fields.premises_address }}"

    if normalized == "date" or normalized.endswith(" date") or normalized.startswith("date"):
        with_time = ""
        descriptive = set(re.findall(r"[a-z]+", normalized)) - {"date", "time", "and", "of", "the"}
        if descriptive:
            # The label names its date ("[Hearing Date]", "[Date of Service]"):
            # the label, not the sentence around it, says which date it is.
            for cue, _label_cue, path in CONTEXT_FIELD_HINTS:
                if cue in normalized:
                    return "{{ " + path + " }}"
            return "{{ fields." + _field_name(label, fallback) + " }}"
        # "[DATE/TIME]" says no more than "[DATE]" does; read it like one, and
        # keep its time.
        with_time = "_time" if "time" in normalized else ""
        # Only a bracketed date is read for standalone-ness: the other paths do
        # not pass the words around it, so "nothing around it" is unknown there.
        position_known = date_context is not None
        date_context = context if date_context is None else date_context
        date_context_normalized = " ".join(date_context.lower().split())
        for context_cue, label_cue, path in CONTEXT_FIELD_HINTS:
            if context_cue in date_context_normalized and label_cue in normalized:
                return "{{ " + path + with_time + " }}"
        # A bare [DATE] with no recognized cue is its own date, named from its
        # sentence. Defaulting every one of them to the filing date printed one
        # answer as a motion's hearing, filing, move-in and loss-of-heat dates.
        # "On [DATE], CMHA issued a decision" says what the date is only after it.
        has_context = date_context != fallback and _says_something(date_context)
        if not has_context and not following:
            if position_known:
                # Alone on its line: the date the letter or memo itself carries.
                return "{{ fields.document_date }}"
            # Nothing to name it by; its position at least keeps it separate.
            return "{{ fields." + _field_name(fallback, fallback) + "_date" + with_time + " }}"
        name = _field_name(date_context if has_context else following, fallback)
        name = name if name.endswith("date") else f"{name}_date"
        return "{{ fields." + name + with_time + " }}"

    if normalized == "time" or normalized.endswith(" time"):
        return "{{ fields.hearing_time }}" if "hearing" in context_normalized else "{{ fields.time }}"

    if "case caption" in normalized:
        return "{{ fields.case_caption }}"
    if "plaintiff" in normalized:
        return "{{ fields.plaintiff_name }}"
    if "signature block" in normalized:
        return "{{ advocate_signature_block }}"

    return "{{ fields." + _field_name(label, fallback) + " }}"


def _run_spans(paragraph):
    """Return (run, start, end) for every run, over the paragraph's full text."""
    spans = []
    cursor = 0
    for run in paragraph.runs:
        length = len(run.text)
        spans.append((run, cursor, cursor + length))
        cursor += length
    return spans


def _is_highlighted(run):
    return run.font.highlight_color is not None


def _highlight_spans(spans):
    """Contiguous character ranges covered by highlighted runs."""
    ranges = []
    for run, start, end in spans:
        if start == end or not _is_highlighted(run):
            continue
        if ranges and ranges[-1][1] == start:
            ranges[-1][1] = end
        else:
            ranges.append([start, end])
    return [(start, end) for start, end in ranges]


def _clear_highlight(run):
    run.font.highlight_color = None


def _clear_highlight_over(paragraph, start, end):
    """Drop the author's "fill this in" marking without touching the text."""
    for run, run_start, run_end in _run_spans(paragraph):
        if run_end > start and run_start < end and _is_highlighted(run):
            _clear_highlight(run)


@dataclass
class _Replacement:
    start: int
    end: int
    text: str
    drop_highlight: bool = True


def _bracket_replacements(text, fallback_prefix, conversion, protected=(), nearby=""):
    replacements = []
    for index, match in enumerate(BRACKET_RE.finditer(text), start=1):
        if any(match.start() >= start and match.end() <= end for start, end in protected):
            continue
        label = match.group(1)
        if not looks_like_placeholder(label):
            continue
        fallback = f"{fallback_prefix}_{index}"
        context = context_label(text, match.start(), fallback)
        if is_instruction(label):
            conversion.instructions.append(_normalize(label))
        expression = placeholder_expression(
            label,
            fallback,
            context=context,
            nearby=nearby,
            following=following_label(text, match.end()),
            date_context=date_context_label(text, match.start(), fallback),
        )
        _record(conversion, expression)
        replacements.append(_Replacement(match.start(), match.end(), expression))
    return replacements


def _underscore_replacements(text, fallback_prefix, conversion, taken, nearby=""):
    replacements = []
    index = 0
    for pattern in (YEAR_STUB_RE, UNDERSCORE_RE):
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue
            index += 1
            fallback = f"{fallback_prefix}_blank_{index}"
            context = context_label(text, match.start(), fallback)
            if _completed_in_ink(text, match, context, fallback, nearby):
                taken.append((match.start(), match.end()))
                continue
            if context == fallback:
                # "…Housing Division, Case No. ___" is too long a sentence to be
                # named by, but its last clause still says what the blank is.
                context = clause_label(text, match.start()) or fallback
            label = "" if pattern is UNDERSCORE_RE else "year"
            if label == "year":
                expression = "{{ fields.filing_year }}"
            else:
                expression = _date_part_expression(text, match, fallback) or placeholder_expression(
                    "", fallback, context=context, nearby=nearby
                )
            _record(conversion, expression)
            replacements.append(_Replacement(match.start(), match.end(), expression))
            taken.append((match.start(), match.end()))
    return replacements


def _completed_in_ink(text, match, context, fallback, nearby):
    """A blank someone fills by hand at signing, which stays a blank line."""
    if JURAT_CUE_RE.search(text):
        return True
    if match.end() - match.start() < SIGNING_RULE_MIN:
        return False
    if context != fallback and _says_something(context):
        return False
    if SIGNATURE_CUE_RE.search(context) or SIGNATURE_CUE_RE.search(nearby or ""):
        # The advocate's own rule becomes their signature block instead.
        return False
    return bool(SIGNER_CUE_RE.search(text) or SIGNER_CUE_RE.search(nearby or ""))


def _date_part_expression(text, match, fallback):
    """Name the day and the month of "this ___ day of ___" apart."""
    if DAY_BLANK_FOLLOWS_RE.match(text[match.end():]):
        base = _field_name(context_label(text, match.start(), fallback), fallback)
        return "{{ fields." + base + "_day }}"
    day = MONTH_BLANK_PRECEDES_RE.search(text[: match.start()])
    if day:
        base = _field_name(context_label(text, day.start(1), fallback), fallback)
        return "{{ fields." + base + "_month }}"
    return None


def _flag_name(text, fallback):
    words = [word for word in re.findall(r"[A-Za-z]+", text.lower()) if len(word) > 2][:5]
    return "include_" + ("_".join(words) if words else fallback)


def _highlight_replacements(paragraph, text, fallback_prefix, conversion, taken, nearby=""):
    """Highlighted spans become values, or keep their wording behind a toggle."""
    replacements = []
    for index, (start, end) in enumerate(_highlight_spans(_run_spans(paragraph)), start=1):
        segment = text[start:end]
        stripped = segment.strip()
        if not stripped:
            continue
        if any(start < covered_end and covered_start < end for covered_start, covered_end in taken):
            # Already handled as a bracket or underscore fill-in.
            continue
        if _normalize(stripped).casefold().startswith("case caption"):
            # An editorial marker for "the filing caption goes here". Export
            # builds the real caption from it, so it must stay literal text.
            _clear_highlight_over(paragraph, start, end)
            taken.append((start, end))
            continue
        fallback = f"{fallback_prefix}_hl_{index}"
        words = stripped.split()
        if len(words) >= OPTIONAL_PROSE_MIN_WORDS:
            # A sentence this long is wording, not a value, even though it is
            # bound to mention a cue word like "plaintiff" or "date". Collapsing
            # it into one variable would silently delete the author's text.
            flag = _flag_name(stripped, fallback)
            conversion.flags.add(flag)
            leading = segment[: len(segment) - len(segment.lstrip())]
            trailing = segment[len(segment.rstrip()) :]
            expression = f"{leading}{{% if {flag} %}}{stripped}{{% endif %}}{trailing}"
            replacements.append(_Replacement(start, end, expression, drop_highlight=True))
            taken.append((start, end))
            continue
        if not looks_like_placeholder(stripped):
            continue
        if _normalize(stripped).casefold() in PARTY_ROLES and ARTICLE_BEFORE_RE.search(text[:start]):
            # "I am the Defendant": the role, kept as the author wrote it, like
            # any other highlighted wording that is not a value to supply.
            continue
        context = context_label(text, start, fallback)
        if is_instruction(stripped):
            conversion.instructions.append(_normalize(stripped))
        clause = clause_label(text, start)
        if re.search(r"\w", stripped) and not PLACEHOLDER_CUE_RE.search(stripped) and _says_something(clause):
            # A highlighted sample value ("COUNTY OF CUYAHOGA", "owes Plaintiff
            # $X") says what goes there by example; its clause says what it is.
            expression = "{{ fields." + _field_name(clause, fallback) + " }}"
        else:
            expression = placeholder_expression(stripped, fallback, context=context, nearby=nearby)
        _record(conversion, expression)
        replacements.append(_Replacement(start, end, expression))
        taken.append((start, end))
    return replacements


def _record(conversion, expression):
    inner = expression.strip("{} ").strip()
    if inner.startswith("fields."):
        conversion.fields.add(inner)


def _apply_replacements(paragraph, replacements):
    """Write substitutions back run by run, keeping every run's formatting."""
    if not replacements:
        return False
    spans = _run_spans(paragraph)
    if not spans:
        return False
    replacements = sorted(replacements, key=lambda item: item.start)

    for run, run_start, run_end in spans:
        pieces = []
        cursor = run_start
        dropped_highlight = False
        for replacement in replacements:
            if replacement.end <= run_start or replacement.start >= run_end:
                continue
            overlap_start = max(replacement.start, run_start)
            if overlap_start > cursor:
                pieces.append(run.text[cursor - run_start : overlap_start - run_start])
            if replacement.start >= run_start:
                # The run where the placeholder opens carries the whole binding.
                pieces.append(replacement.text)
                if replacement.drop_highlight:
                    dropped_highlight = True
            cursor = max(cursor, min(replacement.end, run_end))
        if cursor == run_start and not pieces:
            continue
        if cursor < run_end:
            pieces.append(run.text[cursor - run_start :])
        run.text = "".join(pieces)
        if dropped_highlight and _is_highlighted(run):
            _clear_highlight(run)
    return True


def convert_paragraph(paragraph, fallback_prefix, nearby=""):
    """Replace every fill-in in one paragraph, preserving run formatting.

    ``nearby`` carries the wording of the surrounding paragraphs, which is the
    only context a fill-in standing alone on its own line has.
    """
    conversion = ParagraphConversion()
    text = paragraph.text
    if not text.strip():
        return conversion
    # A paragraph can contain both an already-prepared binding and an older
    # bracket placeholder.  Converting only the latter is safe: the bracket
    # and underscore patterns do not match Jinja syntax.  The old early return
    # made mixed paragraphs impossible to repair on re-ingest.

    protected = [match.span() for match in JINJA_TOKEN_RE.finditer(text)]
    taken: list[tuple[int, int]] = list(protected)
    replacements = _bracket_replacements(text, fallback_prefix, conversion, protected, nearby)
    taken.extend((item.start, item.end) for item in replacements)
    replacements += _underscore_replacements(text, fallback_prefix, conversion, taken, nearby)
    replacements += _highlight_replacements(paragraph, text, fallback_prefix, conversion, taken, nearby)

    conversion.changed = _apply_replacements(paragraph, replacements)
    return conversion


def convert_text(text, fallback_prefix, nearby=""):
    """Placeholder conversion for plain strings (block `body` previews)."""
    conversion = ParagraphConversion()
    if not text:
        return text, conversion
    protected = [match.span() for match in JINJA_TOKEN_RE.finditer(text)]
    replacements = _bracket_replacements(text, fallback_prefix, conversion, protected, nearby)
    taken = [*protected, *((item.start, item.end) for item in replacements)]
    replacements += _underscore_replacements(text, fallback_prefix, conversion, taken, nearby)
    pieces = []
    cursor = 0
    for replacement in sorted(replacements, key=lambda item: item.start):
        if replacement.start < cursor:
            continue
        pieces.append(text[cursor : replacement.start])
        pieces.append(replacement.text)
        cursor = replacement.end
    pieces.append(text[cursor:])
    converted = "".join(pieces)
    conversion.changed = converted != text
    return converted, conversion


def convert_editor_state(state, fallback_prefix):
    """Convert legacy bracket fill-ins inside a Lexical JSON document.

    Catalogs created before mixed Jinja/bracket repair can carry the old text
    in their saved editor state even after the plain body is normalized.  Walk
    text nodes only, so formatting, paragraph spacing, and every other Lexical
    property remain unchanged.
    """
    if not isinstance(state, dict) or not isinstance(state.get("root"), dict):
        return state, ParagraphConversion()

    converted_state = deepcopy(state)
    conversion = ParagraphConversion()
    text_index = 0

    def visit(node):
        nonlocal text_index
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            text_index += 1
            converted, node_conversion = convert_text(
                node.get("text", ""), f"{fallback_prefix}_{text_index}"
            )
            node["text"] = converted
            conversion.fields.update(node_conversion.fields)
            conversion.flags.update(node_conversion.flags)
            conversion.instructions.extend(node_conversion.instructions)
            conversion.changed = conversion.changed or node_conversion.changed
        for child in node.get("children") or []:
            visit(child)

    visit(converted_state["root"])
    return converted_state, conversion
