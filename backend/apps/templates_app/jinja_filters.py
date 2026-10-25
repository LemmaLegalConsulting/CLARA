"""Small, deterministic legal-template phrasing helpers.

The public API follows the useful parts of Docassemble/AssemblyLine's language
helpers without depending on Docassemble's interview object model. Both source
projects are MIT licensed; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
from functools import wraps
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from jinja2 import ChainableUndefined, Template, Undefined, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError
from jinja2.visitor import NodeTransformer


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
    if not amount.is_finite() or abs(amount.adjusted()) > 18:
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


def validate_output_value(value):
    # Count repeated container references before Python can stringify a compact
    # nested list into an exponentially larger single output chunk.
    remaining = [1024 * 1024, 20_000]
    def visit(item, depth):
        remaining[1] -= 1
        if depth > 12 or remaining[1] < 0:
            raise SecurityError("Template value exceeds structural limits")
        if isinstance(item, str):
            remaining[0] -= len(item) * 8 + 8
        elif isinstance(item, dict):
            for key, nested in item.items():
                visit(key, depth + 1)
                visit(nested, depth + 1)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested, depth + 1)
        else:
            remaining[0] -= 64
        if remaining[0] < 0:
            raise SecurityError("Template value exceeds size limit")
    visit(value, 0)
    return value


MAX_EXPRESSION_CHARS = 1024 * 1024


def bounded_join(value, separator="", attribute=None):
    if attribute is not None:
        raise SecurityError("Attribute joins are prohibited")
    parts = []
    size = 0
    separator = str(separator)
    for item in value:
        item = str(item)
        size += len(item) + (len(separator) if parts else 0)
        if size > MAX_EXPRESSION_CHARS:
            raise SecurityError("Template expression exceeds size limit")
        parts.append(item)
    return separator.join(parts)


def bounded_replace(value, old, new, count=None):
    value, old, new = str(value), str(old), str(new)
    occurrences = value.count(old)
    if count is not None and int(count) >= 0:
        occurrences = min(occurrences, int(count))
    if len(value) + occurrences * max(0, len(new) - len(old)) > MAX_EXPRESSION_CHARS:
        raise SecurityError("Template expression exceeds size limit")
    return value.replace(old, new, -1 if count is None else int(count))


class BoundConcatenation(NodeTransformer):
    def visit_Concat(self, node, *args, **kwargs):
        self.generic_visit(node, *args, **kwargs)
        return nodes.Filter(nodes.List(node.nodes), "_bounded_concat", [], [], None, None).set_lineno(node.lineno)


class BoundedTemplate(Template):
    def generate(self, *args, **kwargs):
        size = 0
        for chunk in super().generate(*args, **kwargs):
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise SecurityError("Rendered template exceeds output limit")
            yield chunk

    def render(self, *args, **kwargs):
        return "".join(self.generate(*args, **kwargs))


class RestrictedTemplateEnvironment(ImmutableSandboxedEnvironment):
    """Only data lookups and explicitly registered language helpers are callable."""

    template_class = BoundedTemplate
    intercepted_binops = frozenset({"+"})

    def call_binop(self, context, operator, left, right):
        if isinstance(left, (str, list, tuple)) and isinstance(right, type(left)):
            if len(left) + len(right) > 100_000:
                raise SecurityError("Template expression exceeds size limit")
        return super().call_binop(context, operator, left, right)

    def is_safe_attribute(self, obj, attr, value):
        return False

    def is_safe_callable(self, obj):
        return any(obj is helper for helper in self.globals.values())

    def _parse(self, source, name, filename):
        if len(source) > 2_000_000:
            raise SecurityError("Template is too large")
        tree = super()._parse(source, name, filename)
        for node in tree.find_all(nodes.Node):
            if isinstance(node, (nodes.Import, nodes.FromImport, nodes.Include, nodes.Extends,
                                 nodes.Macro, nodes.CallBlock, nodes.Pow, nodes.Mul, nodes.Mod,
                                 nodes.AssignBlock)):
                raise SecurityError("Unsupported template operation")
            if isinstance(node, nodes.For) and (node.recursive or list(node.find_all(nodes.For))):
                raise SecurityError("Nested or recursive template loops are prohibited")
            if isinstance(node, nodes.Filter) and node.name not in self.filters:
                raise SecurityError("Attribute filters are prohibited")
            if isinstance(node, nodes.Getattr) and node.attr.startswith("_"):
                raise SecurityError("Private template attributes are prohibited")
            if isinstance(node, nodes.Call) and (
                not isinstance(node.node, nodes.Name) or node.node.name not in JINJA_FILTERS
            ):
                raise SecurityError("Only approved template helpers may be called")
        return BoundConcatenation().visit(tree)


def checked_template_function(function):
    @wraps(function)
    def checked(*args, **kwargs):
        # Filters and helpers may stringify before finalize gets a chance to run.
        for value in (*args, *kwargs.values()):
            validate_output_value(value)
        return validate_output_value(function(*args, **kwargs))
    return checked


def configure_template_environment(environment):
    environment.finalize = validate_output_value
    # No allocating width/format/attribute filters from stock Jinja.
    allowed = {"default", "d", "lower", "upper", "title", "capitalize", "trim",
               "length", "first", "last", "int", "float", "string", "escape", "e", "safe", "truncate"}
    environment.filters = {key: value for key, value in environment.filters.items() if key in allowed}
    environment.filters.update({"join": bounded_join, "replace": bounded_replace, "_bounded_concat": bounded_join})
    environment.filters.update(JINJA_FILTERS)
    environment.globals.clear()
    environment.globals.update(JINJA_FILTERS)
    environment.filters = {name: checked_template_function(function) for name, function in environment.filters.items()}
    environment.globals = {name: checked_template_function(function) for name, function in environment.globals.items()}
    return environment


def template_environment(*, undefined=ChainableUndefined):
    return configure_template_environment(RestrictedTemplateEnvironment(undefined=undefined, autoescape=False))


class FillEnvironment(RestrictedTemplateEnvironment):
    def is_safe_callable(self, obj):
        return any(obj is function for function in self.globals.values())


def fill_environment(**kwargs):
    env = FillEnvironment(autoescape=True, **kwargs)
    configure_template_environment(env)
    return env
