from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from django.db import transaction
from django.utils import timezone

from apps.core.content_library import content_library_dir, content_library_roots
from apps.templates_app.models import DocumentTemplate, TemplateBlock
from apps.templates_app.placeholders import convert_text


# v1 packages rebound every paragraph to an AI slot; v2 keeps the maintained
# wording and records per-block `ai_latitude`. Both load so an existing library
# keeps working until it is re-ingested.
SUPPORTED_MANIFEST_VERSIONS = {1, 2}

PREPARED_TEMPLATE_DIR = "document-templates"
TEMPLATE_OVERRIDE_DIR = "template-overrides"
GENERIC_TEMPLATE_DESCRIPTIONS = {
    "prepared from the maintained original word template.",
}


class TemplateManifestError(ValueError):
    pass


def logical_content_path(path: Path) -> str:
    for root in content_library_roots():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    raise TemplateManifestError(f"Template asset is outside configured content providers: {path}")


def resolve_content_asset(logical_path: str) -> Path:
    candidates = []
    for root in content_library_roots():
        root = root.resolve()
        candidate = (root / logical_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise TemplateManifestError(f"Content path escapes configured provider: {logical_path}") from exc
        candidates.append(candidate)
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else (content_library_dir() / logical_path).resolve()


def _checksum_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_manifest(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    required = {"schema_version", "slug", "title", "kind", "render", "blocks"}
    missing = sorted(required - set(data))
    if missing:
        raise TemplateManifestError(f"{path}: missing {', '.join(missing)}")
    if data["schema_version"] not in SUPPORTED_MANIFEST_VERSIONS:
        raise TemplateManifestError(f"{path}: unsupported schema_version {data['schema_version']}")
    if not isinstance(data["blocks"], list):
        raise TemplateManifestError(f"{path}: blocks must be a list")
    keys = [row.get("key") for row in data["blocks"]]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise TemplateManifestError(f"{path}: block keys must be present and unique")

    if data["render"].get("strategy") == "workbook":
        # A spreadsheet exhibit has no reviewable prose blocks; the advocate
        # fills rows in the workbook, so only the workbook itself is validated.
        workbook = (path.parent / (data["render"].get("xlsx") or "")).resolve()
        if not data["render"].get("xlsx") or not workbook.is_file():
            raise TemplateManifestError(f"{path}: render.xlsx does not exist")
        logical_content_path(workbook)
        return data, _checksum_bytes(raw)

    if not data["blocks"]:
        raise TemplateManifestError(f"{path}: blocks must be a non-empty list")
    render_path = data["render"].get("docx")
    prepared_docx = (path.parent / (render_path or "")).resolve()
    if not render_path or not prepared_docx.is_file():
        raise TemplateManifestError(f"{path}: render.docx does not exist")
    logical_content_path(prepared_docx)
    for row in data["blocks"]:
        block_path = row.get("docx")
        if not block_path:
            raise TemplateManifestError(f"{path}: block {row['key']} has no docx asset")
        asset = resolve_content_asset(block_path)
        if not asset.is_file():
            raise TemplateManifestError(f"{path}: block {row['key']} docx does not exist: {block_path}")
        expected_checksum = row.get("sha256")
        if expected_checksum and _checksum_bytes(asset.read_bytes()) != expected_checksum:
            raise TemplateManifestError(f"{path}: block {row['key']} checksum does not match")
    return data, _checksum_bytes(raw)


def iter_manifests():
    seen_slugs = set()
    for provider_root in content_library_roots():
        root = provider_root / PREPARED_TEMPLATE_DIR
        if not root.exists():
            continue
        for path in sorted(root.glob("*/manifest.yaml")):
            if path.parent.name in seen_slugs:
                continue
            seen_slugs.add(path.parent.name)
            yield path


def iter_template_overrides():
    seen_slugs = set()
    for provider_root in content_library_roots():
        root = provider_root / TEMPLATE_OVERRIDE_DIR
        if not root.exists():
            continue
        for path in sorted(root.glob("*.yaml")):
            data = yaml.safe_load(path.read_text()) or {}
            slug = data.get("slug")
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                yield path, data


def _template_metadata(manifest: dict) -> dict:
    return {
        "schemaVersion": manifest["schema_version"],
        "render": manifest.get("render", {}),
        "fields": manifest.get("fields", []),
        "choices": manifest.get("choices", []),
        # Highlighted wording the converter put behind a toggle. It renders
        # unless someone turns it off; see template_choice_values.
        "flags": manifest.get("flags", []),
        # Labels, questions, and kinds a reviewer wrote for the template's
        # fill-ins in its refinement file.
        "fieldSchema": manifest.get("field_schema") or {},
        "source": manifest.get("source", {}),
        # A filing that answers the other side's filing names it; see
        # apps.drafting.responding_to.
        "respondsTo": manifest.get("responds_to") or {},
        "verification": manifest.get("verification", ""),
        "tableOfAuthorities": bool(manifest.get("table_of_authorities")),
    }


def _is_generic_template_description(value: str) -> bool:
    return " ".join((value or "").split()).casefold() in GENERIC_TEMPLATE_DESCRIPTIONS


def _default_template_description(manifest: dict) -> str:
    description = manifest.get("description", "")
    if description and not _is_generic_template_description(description):
        return description
    return f"Maintained Word template for {manifest['title']}."


def _default_template_goal(manifest: dict) -> str:
    goal = manifest.get("goal", "")
    if goal:
        return goal
    title = manifest["title"]
    if manifest.get("kind") == "motion":
        if "motion" in title.casefold():
            return f"Draft {title} with case-specific facts, legal grounds, and requested relief."
        return f"Draft the {title} motion with case-specific facts, legal grounds, and requested relief."
    if manifest.get("kind") == "brief":
        return f"Draft the {title} filing with case-specific facts, legal grounds, and requested relief."
    return f"Draft the {title} document with case-specific facts and the requested relief or outcome."


def _template_defaults(path: Path, manifest: dict, checksum: str) -> dict:
    return {
        "title": manifest["title"],
        "kind": manifest["kind"],
        "description": _default_template_description(manifest),
        "goal": _default_template_goal(manifest),
        "negative_goal": manifest.get("negative_goal", ""),
        "aliases": manifest.get("aliases", []),
        "jurisdiction": manifest.get("jurisdiction", ""),
        "source_label": manifest.get("source_label", "Content library"),
        "metadata": _template_metadata(manifest),
        "source_kind": "content_library",
        "content_path": logical_content_path(path),
        "source_checksum": checksum,
        "is_active": bool(manifest.get("active", True)),
        "last_synced_at": timezone.now(),
    }


def _template_matches_defaults(template: DocumentTemplate, defaults: dict) -> bool:
    stable_fields = set(defaults) - {"last_synced_at"}
    return all(getattr(template, field) == defaults[field] for field in stable_fields)


@transaction.atomic
def sync_prepared_templates(*, deactivate_missing=False):
    """Index prepared packages without overwriting database/admin templates."""
    prepared_roots = [root / PREPARED_TEMPLATE_DIR for root in content_library_roots()]
    if not any(root.exists() for root in prepared_roots):
        # A local provider may be intentionally absent in a development checkout,
        # and a future remote provider may be temporarily unavailable. Neither
        # condition is evidence that indexed templates should be deactivated.
        return []
    results = []
    seen = set()
    for path in iter_manifests() or []:
        manifest, checksum = load_manifest(path)
        slug = manifest["slug"]
        seen.add(slug)
        existing = DocumentTemplate.objects.filter(slug=slug).first()
        if existing and existing.source_kind != "content_library":
            results.append({"slug": slug, "status": "conflict", "template": existing})
            continue
        manifest_block_keys = {row["key"] for row in manifest["blocks"]}
        defaults = _template_defaults(path, manifest, checksum)
        expected_block_bodies = {
            row["key"]: convert_text(
                row.get("body", ""), f"{manifest['slug']}_{row['key']}"
            )[0]
            for row in manifest["blocks"]
        }
        existing_block_bodies = {
            block.key: block.body
            for block in existing.blocks.all()
        } if existing else {}
        if (
            existing
            and existing.source_checksum == checksum
            and _template_matches_defaults(existing, defaults)
            and set(existing.blocks.values_list("key", flat=True)) == manifest_block_keys
            and existing_block_bodies == expected_block_bodies
        ):
            results.append({"slug": slug, "status": "unchanged", "template": existing})
            continue

        if existing:
            for field, value in defaults.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*defaults.keys(), "updated_at"])
            template = existing
            status = "updated"
        else:
            template = DocumentTemplate.objects.create(slug=slug, **defaults)
            status = "created"

        block_keys = set()
        for row in manifest["blocks"]:
            block_keys.add(row["key"])
            normalized_body, _conversion = convert_text(
                row.get("body", ""), f"{manifest['slug']}_{row['key']}"
            )
            block_defaults = {
                "label": row.get("label") or row["key"].replace("-", " ").title(),
                "block_type": row.get("type", "optional_clause"),
                "order": int(row.get("order", 0)),
                "body": normalized_body,
                "required": bool(row.get("required", True)),
                "ai_latitude": row.get("ai_latitude", "locked"),
                "ai_instructions": row.get("instructions", []),
                "ai_fill_mode": row.get("ai_fill_mode", "none"),
                "selection_rule": row.get("selection_rule", {}),
                "supporting_sources": row.get("supporting_sources", []),
                "content_path": row.get("docx", ""),
                "source_checksum": row.get("sha256", ""),
                "input_schema": {key: value for key, value in (row.get("input") or {}).items() if value is not None},
                "lexical_config": {key: value for key, value in (row.get("lexical") or {}).items() if value is not None},
                "editable": bool(row.get("editable", True)),
            }
            TemplateBlock.objects.update_or_create(template=template, key=row["key"], defaults=block_defaults)
        template.blocks.exclude(key__in=block_keys).delete()
        results.append({"slug": slug, "status": status, "template": template})

    if deactivate_missing:
        missing_active_templates = DocumentTemplate.objects.filter(
            source_kind="content_library",
            is_active=True,
        ).exclude(slug__in=seen)
        if missing_active_templates.exists():
            missing_active_templates.update(is_active=False)
    return results


@transaction.atomic
def sync_template_overrides():
    """Apply private/provider template corrections without storing them in code."""
    results = []
    template_fields = {
        "title",
        "kind",
        "description",
        "goal",
        "negative_goal",
        "aliases",
        "jurisdiction",
        "source_label",
        "is_active",
    }
    block_fields = {
        "label",
        "block_type",
        "order",
        "body",
        "required",
        "ai_fill_mode",
        "selection_rule",
        "supporting_sources",
        "input_schema",
        "lexical_config",
        "editable",
        "content_path",
    }
    for path, data in iter_template_overrides():
        if data.get("schema_version") != 1 or not data.get("slug"):
            raise TemplateManifestError(f"{path}: template override requires schema_version 1 and slug")
        template = DocumentTemplate.objects.filter(slug=data["slug"]).first()
        if not template:
            results.append({"slug": data["slug"], "status": "missing"})
            continue
        changed_fields = []
        template_values = data.get("template") or {}
        for field in template_fields:
            if field in template_values and getattr(template, field) != template_values[field]:
                setattr(template, field, template_values[field])
                changed_fields.append(field)
        if "metadata" in template_values:
            metadata = {**(template.metadata or {}), **(template_values["metadata"] or {})}
            if metadata != template.metadata:
                template.metadata = metadata
                changed_fields.append("metadata")
        if changed_fields:
            template.save(update_fields=[*changed_fields, "updated_at"])

        block_changes = 0
        for row in data.get("blocks") or []:
            key = row.get("key")
            if not key:
                raise TemplateManifestError(f"{path}: every block override requires a key")
            block = template.blocks.filter(key=key).first()
            if row.get("delete"):
                if block:
                    block.delete()
                    block_changes += 1
                continue
            if not block:
                if not row.get("create"):
                    raise TemplateManifestError(f"{path}: unknown block {key!r} for {template.slug}")
                required = {"label", "block_type", "order", "body"}
                missing = sorted(required - set(row))
                if missing:
                    raise TemplateManifestError(
                        f"{path}: new block {key!r} is missing {', '.join(missing)}"
                    )
                create_values = {
                    field: row[field]
                    for field in block_fields
                    if field in row
                }
                if "body" in create_values:
                    create_values["body"] = convert_text(
                        create_values["body"], f"{template.slug}_{key}"
                    )[0]
                block = TemplateBlock.objects.create(template=template, key=key, **create_values)
                block_changes += 1
                continue
            update_fields = []
            for field in block_fields:
                if field in row and getattr(block, field) != row[field]:
                    value = row[field]
                    if field == "body":
                        value = convert_text(value, f"{template.slug}_{key}")[0]
                    setattr(block, field, value)
                    update_fields.append(field)
            if update_fields:
                block.save(update_fields=update_fields)
                block_changes += 1
        results.append(
            {
                "slug": template.slug,
                "status": "updated" if changed_fields or block_changes else "unchanged",
                "template": template,
            }
        )
    return results


def full_template_path(template):
    if not template or template.source_kind != "content_library" or not template.content_path:
        return None
    manifest_path = resolve_content_asset(template.content_path)
    if not manifest_path.is_file():
        return None
    try:
        manifest, _checksum = load_manifest(manifest_path)
    except (OSError, TemplateManifestError, yaml.YAMLError):
        return None
    path = (manifest_path.parent / manifest["render"]["docx"]).resolve()
    resolve_content_asset(logical_content_path(path))
    return path if path.is_file() else None
