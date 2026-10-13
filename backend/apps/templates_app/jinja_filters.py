"""Small, deterministic legal-template phrasing helpers.

The public API follows the useful parts of Docassemble/AssemblyLine's language
helpers without depending on Docassemble's interview object model. Both source
projects are MIT licensed; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from jinja2 import ChainableUndefined, Environment, Undefined


KNOWN_PRONOUNS = {
    "she/her/hers": {
        "subjective": "she",
        "objective": "her",
        "determiner": "her",
        "possessive": "hers",
        "reflexive": "herself",
    },
    "he/him/his": {
        "subjective": "he",
        "objective": "him",
        "determiner": "his",
        "possessive": "his",
        "reflexive": "himself",
    },
    "they/them/theirs": {
        "subjective": "they",
        "objective": "them",
        "determiner": "their",
        "possessive": "theirs",
        "reflexive": "themself",
    },
    "ze/zir/zirs": {
        "subjective": "ze",
        "objective": "zir",
        "determiner": "zir",
        "possessive": "zirs",
        "reflexive": "zirself",
    },
}

TEMPLATE_HELPERS_GUIDE = """Available Jinja helpers for maintained/custom blocks:
- {{ people | comma_and_list }} or comma_and_list(people, and_string="or", oxford=False)
- {{ client | pronoun_subjective }}, pronoun_objective, pronoun_possessive("home"), pronoun_reflexive
- {{ people | does_verb("live") }}, {{ people | did_verb("be") }}
- {{ people | as_noun("occupant", "occupants") }} and {{ name | possessive }}
- {{ amount | currency }} prints "$1,250.00" from "1250" or "$1,250"
Pronouns must come from client.pronouns/template data; never infer them from a name."""


def listify(value):
    """Normalize a list field while treating an ordinary string as one item."""
    if value is None or isinstance(value, Undefined):
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if "\n" in stripped or ";" in stripped:
            return [item.strip() for item in re.split(r"\r?\n|;", stripped) if item.strip()]
        return [stripped]
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Iterable):
        return [item for item in value if item is not None and str(item).strip()]
    return [value]


def answered_items(value):
    """The answered items of a list field, as a list.

    For a list where an empty answer means "none" -- the other occupants, the
    other witnesses -- so a sentence can join it with fixed items:
    `{{ (["Plaintiff"] + fields.witnesses|answered_items)|comma_and_list }}`.
    """
    return [item for item in listify(value) if answered(item)]


def answered(value):
    if value is None or isinstance(value, Undefined):
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped and not re.fullmatch(r"\[[^\]]+\]", stripped))
    return bool(value)


def currency(value):
    """A dollar amount as a filing writes it: "$1,250.00".

    An answer typed as "1250", "$1,250", or "1250.00" reads the same, so a
    template can write `{{ amount|currency }}` without the "$" doubling when an
    advocate typed one. Anything that is not a plain amount -- "the entirety of
    the contract rent", an unanswered "[Rent]" -- passes through unchanged.
    """
    if value is None or isinstance(value, Undefined):
        return ""
    text = str(value).strip()
    try:
        amount = Decimal(text.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return text
    return f"${amount:,.2f}"


def _display(value):
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("label") or value.get("text") or "").strip()
    return str(value).strip()


def comma_and_list(value, *extra, and_string="and", oxford=True, comma_string=", "):
    items = [_display(item) for item in [*listify(value), *extra]]
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {and_string} {items[1]}"
    final_comma = comma_string.strip() if oxford else ""
    return f"{comma_string.join(items[:-1])}{final_comma} {and_string} {items[-1]}"


def possessive(value):
    text = _display(value)
    if not text:
        return "[Name]"
    return f"{text}'" if text.casefold().endswith("s") else f"{text}'s"


def _party(value):
    if isinstance(value, Mapping):
        return value
    items = listify(value)
    if len(items) > 1:
        return {"name": comma_and_list(items), "pronouns": "they/them/theirs", "plural": True}
    item = items[0] if items else value
    if isinstance(item, Mapping):
        return item
    text = _display(item)
    if "/" in text:
        return {"name": "", "pronouns": text}
    return {"name": text, "pronouns": ""}


def _pronoun_set(value):
    party = _party(value)
    raw = party.get("pronouns") or ""
    if isinstance(raw, Mapping):
        raw = next((key for key, selected in raw.items() if selected), "")
    normalized = str(raw).strip().casefold()
    if normalized in KNOWN_PRONOUNS:
        return KNOWN_PRONOUNS[normalized]
    parts = [part.strip() for part in normalized.split("/") if part.strip()]
    if len(parts) >= 3:
        return {
            "subjective": parts[0],
            "objective": parts[1],
            "determiner": parts[2] if len(parts) >= 4 else parts[1],
            "possessive": parts[3] if len(parts) >= 4 else parts[2],
            "reflexive": parts[4] if len(parts) >= 5 else f"{parts[1]}self",
        }
    return None


def _pronoun(value, kind, *, capitalize=False, default=""):
    party = _party(value)
    pronouns = _pronoun_set(party)
    if pronouns:
        output = pronouns[kind]
    elif default:
        output = default
    else:
        name = _display(party.get("name"))
        if kind == "determiner":
            output = possessive(name)
        elif kind == "possessive":
            output = possessive(name)
        elif kind == "reflexive":
            output = f"{name} personally" if name else "[Reflexive Pronoun]"
        else:
            output = name or f"[{kind.title()} Pronoun]"
    return output[:1].upper() + output[1:] if capitalize and output else output


def pronoun_subjective(value, capitalize=False, default=""):
    return _pronoun(value, "subjective", capitalize=capitalize, default=default)


def pronoun_objective(value, capitalize=False, default=""):
    return _pronoun(value, "objective", capitalize=capitalize, default=default)


def pronoun_possessive(value, target="", capitalize=False, default=""):
    determiner = _pronoun(value, "determiner", capitalize=capitalize, default=default)
    return f"{determiner} {target}".strip()


def pronoun_reflexive(value, capitalize=False, default=""):
    return _pronoun(value, "reflexive", capitalize=capitalize, default=default)


def _uses_plural_verb(value):
    if isinstance(value, Mapping):
        pronouns = _pronoun_set(value)
        return bool(pronouns and pronouns["subjective"] in {"they", "we", "you"})
    items = listify(value)
    if len(items) > 1:
        return True
    pronouns = _pronoun_set(value)
    return bool(pronouns and pronouns["subjective"] in {"they", "we", "you"})


def does_verb(value, verb):
    verb = str(verb).strip()
    if _uses_plural_verb(value):
        return {"is": "are", "has": "have", "does": "do"}.get(verb.casefold(), verb)
    irregular = {"be": "is", "are": "is", "have": "has", "do": "does", "go": "goes"}
    lowered = verb.casefold()
    if lowered in irregular:
        return irregular[lowered]
    if re.search(r"(?:s|x|z|ch|sh|o)$", lowered):
        return f"{verb}es"
    if re.search(r"[^aeiou]y$", lowered):
        return f"{verb[:-1]}ies"
    return f"{verb}s"


def did_verb(value, verb):
    verb = str(verb).strip()
    lowered = verb.casefold()
    if lowered in {"be", "is", "are", "was", "were"}:
        return "were" if _uses_plural_verb(value) else "was"
    irregular = {"do": "did", "have": "had", "go": "went", "pay": "paid", "make": "made", "say": "said"}
    if lowered in irregular:
        return irregular[lowered]
    if lowered.endswith("e"):
        return f"{verb}d"
    if re.search(r"[^aeiou]y$", lowered):
        return f"{verb[:-1]}ied"
    return f"{verb}ed"


def as_noun(value, singular, plural=None):
    count = len(listify(value))
    if count == 1:
        return singular
    if plural:
        return plural
    if str(singular).endswith("y"):
        return f"{str(singular)[:-1]}ies"
    if str(singular).endswith("s"):
        return f"{singular}es"
    return f"{singular}s"


JINJA_FILTERS = {
    "answered": answered,
    "answered_items": answered_items,
    "as_list": listify,
    "as_noun": as_noun,
    "comma_and_list": comma_and_list,
    "currency": currency,
    "did_verb": did_verb,
    "does_verb": does_verb,
    "possessive": possessive,
    "pronoun": pronoun_objective,
    "pronoun_objective": pronoun_objective,
    "pronoun_possessive": pronoun_possessive,
    "pronoun_reflexive": pronoun_reflexive,
    "pronoun_subjective": pronoun_subjective,
}


def template_environment(*, undefined=ChainableUndefined):
    environment = Environment(undefined=undefined, autoescape=False)
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_FILTERS)
    return environment
