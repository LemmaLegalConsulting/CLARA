"""Plain-data template field defaults, usable in isolated rendering workers."""
import re

LEGACY_LITERAL_FIELDS = {
    "216": "[216]",
    "her": "[her]",
    "or": "[or]",
    "s": "[s]",
    "x": "[x]",
}


# Field names the Word conversion produced by accident rather than from the
# template author's intent: an unlabelled run of underscores becomes
# "placeholder_14_blank_1", and one named after the words in front of it becomes
# "law_argument_32_blank". Neither names a value anybody can supply.
UNUSABLE_FIELD_KEY_RES = (
    re.compile(r"^placeholder_\d+"),
    re.compile(r"_blank(_\d+)?$"),
    re.compile(r"^field_\d"),
)
# A blank named after a citation to an exhibit is naming the sentence it sits
# in: "See, Exhibit [X], Defendant's Gas Bill" is not a question.
UNUSABLE_FIELD_KEY_TOKENS = {"exhibit", "see", "supra", "infra", "ibid"}

# What an unusable placeholder renders as. The Word original showed the advocate
# a blank line here, and a blank line is honest; "[Law Argument 32 Blank]" reads
# like a field somebody forgot to fill.
UNUSABLE_FIELD_MARKER = "__________"


def is_unusable_field_key(key) -> bool:
    """True when a field name is conversion debris, not something to ask about."""
    name = str(key or "").strip().casefold()
    if not name:
        return True
    if any(pattern.search(name) for pattern in UNUSABLE_FIELD_KEY_RES):
        return True
    return bool(UNUSABLE_FIELD_KEY_TOKENS.intersection(name.split("_")))


def signature_lines(name, bar_number="", contact=""):
    """The block an advocate signs a filing with, and its first two lines.

    The maintained originals say "[INSERT SIGNATURE BLOCK]" and draw a rule to
    sign on. Both once rendered the office's contact lines alone, so a filing
    named no attorney and no bar number -- the two things the block is for.
    Returns ``(block, signature)``: the whole block, and the "/s/" line with the
    name and bar number for a certificate of service or a brief's closing.
    """
    named = f"{name} ({bar_number})" if bar_number else name
    signature = "\n".join([f"/s/ {name}", named])
    return "\n".join(line for line in (signature, contact) if line), signature


def template_field_label(key):
    return re.sub(r"\s+", " ", str(key or "").replace("_", " ")).strip().title() or "Required field"


class TemplateFieldValues(dict):
    """Render missing prepared-template fields visibly instead of deleting text."""

    def __missing__(self, key):
        if key in LEGACY_LITERAL_FIELDS:
            return LEGACY_LITERAL_FIELDS[key]
        if is_unusable_field_key(key):
            return UNUSABLE_FIELD_MARKER
        return f"[{template_field_label(key)}]"


def template_field_values(values=None):
    return TemplateFieldValues(values or {})
