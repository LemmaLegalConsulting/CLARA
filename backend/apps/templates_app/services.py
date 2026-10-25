import re

from django.utils.text import slugify

from apps.templates_app.models import DocumentTemplate, TemplateBlock


SECTION_RE = re.compile(r"^(facts|argument|prayer|relief|signature|certificate|caption)\b", re.IGNORECASE)


def build_template_from_example(*, title, example_text, jurisdiction=""):
    slug_base = slugify(title) or "example-template"
    slug = slug_base
    counter = 2
    while DocumentTemplate.objects.filter(slug=slug).exists():
        slug = f"{slug_base}-{counter}"
        counter += 1

    template = DocumentTemplate.objects.create(
        title=title,
        slug=slug,
        kind="shell",
        description="Generated from an example document. Review blocks before production use.",
        jurisdiction=jurisdiction,
        source_label="User example",
        created_from_example=True,
        is_active=False,
        metadata={"builder": "heuristic-outline"},
    )

    sections = []
    current_label = "Body"
    current_lines = []
    for line in example_text.splitlines():
        clean = line.strip()
        if clean and SECTION_RE.match(clean) and current_lines:
            sections.append((current_label, "\n".join(current_lines).strip()))
            current_label = clean[:80]
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_label, "\n".join(current_lines).strip()))

    if not sections:
        sections = [("Body", example_text.strip() or "Template body")]

    for index, (label, body) in enumerate(sections, start=1):
        key = slugify(label) or f"section-{index}"
        TemplateBlock.objects.create(
            template=template,
            key=key[:120],
            label=label,
            block_type="argument" if label.lower() == "body" else "optional_clause",
            order=index * 10,
            body=body,
            ai_fill_mode="none",
        )
    return template
