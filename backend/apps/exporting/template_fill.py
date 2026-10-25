"""Render a snapshotted fill session with highlighted, editable missing values."""
import copy
import io
import re
import uuid
import zipfile

from docxtpl import DocxTemplate
from jinja2 import ChainableUndefined, UndefinedError
from lxml import etree

from apps.templates_app.fill_paths import write_path
from apps.templates_app.jinja_filters import fill_environment
from apps.templates_app.template_syntax import normalize_docxtpl_blocks

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def highlight_markers(content, markers):
    if not markers:
        return content
    pattern = re.compile("(" + "|".join(re.escape(key) for key in markers) + ")", re.I)
    labels = {key.lower(): label for key, label in markers.items()}
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                root = etree.fromstring(data)
                for run in list(root.iter(W + "r")):
                    if not any(pattern.search(text.text or "") for text in run.findall(W + "t")):
                        continue
                    original_props = run.find(W + "rPr")
                    for child in run:
                        if child.tag == W + "rPr":
                            continue
                        segments = pattern.split(child.text or "") if child.tag == W + "t" else [None]
                        for segment in segments:
                            if segment == "":
                                continue
                            new_run = etree.Element(W + "r", attrib=dict(run.attrib))
                            if original_props is not None:
                                new_run.append(copy.deepcopy(original_props))
                            if segment is None:
                                new_run.append(copy.deepcopy(child))
                            else:
                                if segment.lower() in labels:
                                    props = new_run.find(W + "rPr")
                                    if props is None:
                                        props = etree.SubElement(new_run, W + "rPr")
                                    for old in list(props.findall(W + "highlight")):
                                        props.remove(old)
                                    etree.SubElement(props, W + "highlight").set(W + "val", "yellow")
                                    segment = f"[Enter {labels[segment.lower()]}]"
                                text = etree.SubElement(new_run, W + "t")
                                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                                text.text = segment
                            run.addprevious(new_run)
                    run.getparent().remove(run)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            target.writestr(info, data)
    return output.getvalue()


def _render_fill_in_worker(content, state, *, preview=False):
    """Render the session's answers into its template snapshot.

    With ``preview`` the DOCX comes back unhighlighted, together with a map of
    the tokens left in its text: one per prompt, and one per value the author
    supplied, so a preview can show which words came from the form."""
    markers, marked, assumed = {}, {}, []

    class Blank(str):
        pass

    def marker(label, key=None):
        token = "FILL" + uuid.uuid4().hex.upper() + "END"
        markers[token] = label
        marked[token] = {"kind": "prompt", "label": label, "key": key}
        return Blank(token)

    def filled(value, field):
        token = "FILV" + uuid.uuid4().hex.upper() + "END"
        marked[token] = {"kind": "filled", "label": field["label"], "key": field["key"], "text": value}
        return Blank(token)

    # Values that feed template logic (conditions, loops, choices) stay as they
    # are; a token in their place would change which branch renders.
    def markable(field, value):
        return preview and isinstance(value, str) and not field.get("required") and not field.get("choices") \
            and field["kind"] not in {"json", "control", "boolean"}

    class Missing(ChainableUndefined):
        def __str__(self):
            return marker(str(self._undefined_name or "value").replace("_", " "))

        def _control(self, *args, **kwargs):
            if preview:
                # A preview reads an unanswered condition as off and says so;
                # an export refuses to guess.
                name = str(self._undefined_name or "value").replace("_", " ")
                if name not in assumed:
                    assumed.append(name)
                return False
            raise UndefinedError(f"Choose a value for {self._undefined_name} before using it in a condition.")

        __bool__ = __len__ = __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _control

        def __iter__(self):
            if preview:
                self._control()
                return iter(())
            raise UndefinedError(f"Supply the collection {self._undefined_name} (or [] for none).")

    context = copy.deepcopy(state["context"])
    answers = state.get("answers", {})
    for field in state["fields"]:
        key = field["key"]
        value = answers[key] if key in answers else field.get("value")
        missing = value is None or value == ""
        if missing:
            value = Missing(name=field["label"]) if field.get("required") else marker(field["label"], key)
        if field.get("blockKey"):
            parts = str(value).splitlines() or [str(value)]
            if not missing and markable(field, value):
                parts, value = [filled(part, field) for part in parts], filled(value, field)
            context["blocks"][field["blockKey"]].update(body=value, revision=value, paragraphs=parts, items=parts, numbered_items=parts)
        elif not missing and markable(field, value):
            write_path(context, field["path"], filled(value, field))
        else:
            write_path(context, field["path"], value)

    class FillDocx(DocxTemplate):
        def patch_xml(self, source):
            return normalize_docxtpl_blocks(super().patch_xml(source))

    environment = fill_environment(undefined=Missing)
    # A date/number/truncation filter must not swallow or damage a missing
    # value's prompt. Only real values are formatted.
    def preserve_blank(function):
        injected = getattr(function, "jinja_pass_arg", None)
        def wrapped(*args, **kwargs):
            index = 1 if injected else 0
            if len(args) > index and isinstance(args[index], (Blank, Missing)):
                value = args[index]
                return value
            return function(*args, **kwargs)
        if injected:
            wrapped.jinja_pass_arg = injected
        return wrapped
    environment.filters = {name: preserve_blank(function) for name, function in environment.filters.items()}
    def require_resolved(function):
        def wrapped(*args, **kwargs):
            if any(isinstance(value, Missing) for value in args):
                if preview:
                    return next(value for value in args if isinstance(value, Missing))._control()
                raise UndefinedError("Resolve the missing value before using it in template logic.")
            return function(*args, **kwargs)
        return wrapped
    environment.tests = {name: require_resolved(function) for name, function in environment.tests.items()}
    environment.globals = {name: require_resolved(function) for name, function in environment.globals.items()}
    doc = FillDocx(io.BytesIO(content))
    try:
        doc.render(context, jinja_env=environment)
    except UndefinedError as error:
        raise ValueError(str(error)) from error
    output = io.BytesIO()
    doc.save(output)
    if preview:
        return output.getvalue(), marked, assumed
    return highlight_markers(output.getvalue(), markers)


def render_fill(content, state, *, preview=False):
    from apps.core.constrained_worker import run_fill
    return run_fill(content, state, preview=preview)
