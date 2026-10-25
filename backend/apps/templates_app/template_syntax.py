"""Pure normalization of Word template syntax, shared by restricted workers."""
import re

DOCXTPL_PREFIXES = ("p", "tr", "tc", "tbl", "r", "sectPr")
BLOCK_PREFIX_RE = re.compile(r"\{%\s*(?:" + "|".join(DOCXTPL_PREFIXES) + r")\b", re.IGNORECASE)
EXPR_PREFIX_RE = re.compile(r"(\{\{\s*)(?:" + "|".join(DOCXTPL_PREFIXES) + r")\s+", re.IGNORECASE)
NUMERIC_FIELD_RE = re.compile(r"\b(fields)\.([0-9][A-Za-z0-9_]*)")
BARE_DEFENDANT_RE = re.compile(r"\bDefendant\s+NAME\b", re.IGNORECASE)
BARE_CLIENT_NAME_RE = re.compile(r"\bM(?:x|s|r)\.\s+NAME\b", re.IGNORECASE)

def normalize_docxtpl_blocks(text):
    text = BLOCK_PREFIX_RE.sub("{%", text)
    text = EXPR_PREFIX_RE.sub(r"\1", text)
    text = NUMERIC_FIELD_RE.sub(lambda match: f'{match.group(1)}["{match.group(2)}"]', text)
    text = BARE_DEFENDANT_RE.sub("Defendant {{ defendant }}", text)
    text = BARE_CLIENT_NAME_RE.sub("{{ defendant }}", text)
    # Word types curly quotes into a tag ("{{ fields[“x”] }}"), which Jinja
    # cannot parse. Only the tags are straightened: replacing quotes across the
    # whole document turned every “Premises” and Defendant’s in an exported
    # filing into typewriter quotes.
    return JINJA_SPAN_RE.sub(lambda match: _straight_quotes(match.group(0)), text)


JINJA_SPAN_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def _straight_quotes(text):
    return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
