#!/usr/bin/env python3
"""Which fix catches a seeded flaw: compare the gym with each one on and off.

Runs every brief in content/argument-gym/seeded-flaws.yaml through the full
Argument Gym pipeline under several arms, in a throwaway test database, and
reports per variant how often the flaw was found -- and, for a control brief,
how often something was wrongly flagged. Findings are split by basis, so a
web-only catch is never counted as a library catch.

Arms:
  baseline        no federal corpus, no rule profile, no applicability, no web
  corpus          federal-housing-law corpus searchable by the adversarial research
  rule_profile    the maintained rule profile (e.g. cares-4024-notice) only
  applicability   federal corpus + the general applicability check
  web             web lookup only (federal corpus hidden, so the cited section is "not in library")
  all             everything on

"Found" means a challenge's text contains one of the flaw's detection_terms.
That is a screen, not a grade: every matching challenge is written to the
report so a person can confirm it is the flaw and not a neighbour of it.

Without --live the model is off and only deterministic checks run, which is the
offline floor. With --live it uses the configured provider and costs money:
roughly 10-14 model calls per brief per arm per run, plus web searches in the
web and all arms (AI_WEB_SEARCH_ENABLED must be on for those to do anything).

  .venv/bin/python scripts/benchmark_seeded_flaws.py
  .venv/bin/python scripts/benchmark_seeded_flaws.py --live --web --runs 3 --output /tmp/seeded.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import yaml  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test.runner import DiscoverRunner  # noqa: E402
from django.test.utils import override_settings, setup_test_environment  # noqa: E402

FEDERAL_SLUG = "federal-housing-law"
ARMS = {
    "baseline": {"corpus": False, "profile": False, "checks": []},
    "corpus": {"corpus": True, "profile": False, "checks": []},
    "rule_profile": {"corpus": False, "profile": True, "checks": ["rule_elements"]},
    "applicability": {"corpus": True, "profile": False, "checks": ["applicability"]},
    "web": {"corpus": False, "profile": False, "checks": ["web_lookup"]},
    "all": {"corpus": True, "profile": True, "checks": ["rule_elements", "applicability", "web_lookup"]},
}
ALWAYS = ["adversarial", "record_audit"]


def hide_federal_corpus(stack):
    """Make the federal corpus invisible to retrieval for the length of an arm."""
    from apps.sources import library
    from apps.sources.connectors.rag import ContentLibraryTreatiseConnector
    from apps.sources.research import index as research_index_module

    original = library.manifest_paths
    original_rag = ContentLibraryTreatiseConnector._manifest_paths

    def without_federal():
        return [path for path in original() if path.parent.name != FEDERAL_SLUG]

    def rag_without_federal(self):
        return [path for path in original_rag(self) if path.parent.name != FEDERAL_SLUG]

    for target in ("apps.sources.library.manifest_paths", "apps.sources.research.corpus.manifest_paths",
                   "apps.sources.research.index.manifest_paths"):
        try:
            stack.enter_context(mock.patch(target, without_federal))
        except AttributeError:
            pass
    stack.enter_context(mock.patch.object(ContentLibraryTreatiseConnector, "_manifest_paths", rag_without_federal))
    research_index_module.reset_index()


def found(challenge, terms):
    text = " ".join([challenge.finding, challenge.opponent_argument, challenge.why_it_matters]).casefold()
    return any(term.casefold() in text for term in terms)


def run_variant(flaw, variant, arm_name, arm, *, user):
    from apps.argument_gym import ingestion
    from apps.argument_gym.models import GymChallenge, GymDocument, GymRun, GymWorkspace
    from apps.argument_gym.pipeline import execute_run
    from apps.rules.legal_rules import ensure_legal_rule_profiles
    from apps.rules.models import LegalRuleProfile
    from apps.sources.research.index import reset_index

    ensure_legal_rule_profiles()
    LegalRuleProfile.objects.filter(slug=flaw["rule_slug"]).update(active=arm["profile"])
    workspace = GymWorkspace.objects.create(
        owner=user, title=f"{flaw['id']}/{variant['id']}/{arm_name}", jurisdiction="Ohio",
        enabled_checks=[*ALWAYS, *arm["checks"]],
    )
    ingested = ingestion.ingest_upload(variant["brief"].encode("utf-8"), filename="brief.txt", content_type="text/plain")
    brief = GymDocument.objects.create(
        workspace=workspace, role=GymDocument.BRIEF_UNDER_TEST, source_type=GymDocument.UPLOAD,
        title=variant["id"], extracted_text=ingested["text"], extraction_metadata=ingested["metadata"],
    )
    with ExitStack() as stack:
        if not arm["corpus"]:
            hide_federal_corpus(stack)
        else:
            reset_index()
        run = execute_run(GymRun.objects.create(workspace=workspace, brief=brief))
    reset_index()
    matches = defaultdict(list)
    for challenge in run.challenges.all():
        if found(challenge, flaw["detection_terms"]):
            matches[challenge.basis].append(
                {"finding": challenge.finding, "argument": challenge.opponent_argument[:500], "severity": challenge.severity}
            )
    return {
        "status": run.status,
        "error": run.error,
        "library": bool(matches[GymChallenge.LIBRARY]),
        "web": bool(matches[GymChallenge.WEB]),
        "matches": dict(matches),
        # Every challenge, matched or not: on a control brief the unmatched ones
        # are where a false positive would hide.
        "challenges": [
            {"basis": c.basis, "category": c.category, "severity": c.severity, "finding": c.finding,
             "argument": c.opponent_argument[:400]}
            for c in run.challenges.all()
        ],
        "applicability": [
            {k: item.get(k) for k in ("authority", "verdict", "grounded", "quote")}
            for item in (run.applicability or {}).get("assessments", [])
        ],
        "webLookup": {k: (run.web_lookup or {}).get(k) for k in ("status", "summary", "questions", "withheld")},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Use the configured model provider (costs money).")
    parser.add_argument("--web", action="store_true", help="Turn web search on for the web and all arms (AI_WEB_SEARCH_ENABLED).")
    parser.add_argument("--runs", type=int, default=1, help="Repetitions per arm; models are not deterministic.")
    parser.add_argument("--variants", default="", help="Comma-separated variant ids to run (default: all).")
    parser.add_argument("--arms", default=",".join(ARMS), help="Comma-separated subset of: " + ", ".join(ARMS))
    parser.add_argument("--output", help="Write the full report, with every matching challenge, as JSON.")
    args = parser.parse_args()

    suite = yaml.safe_load((ROOT / "content" / "argument-gym" / "seeded-flaws.yaml").read_text(encoding="utf-8"))
    arms = {name: ARMS[name] for name in args.arms.split(",") if name in ARMS}

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old_config = runner.setup_databases()
    report = []
    try:
        with override_settings(
            AI_DRAFTING_ENABLED=args.live,
            AI_WEB_SEARCH_ENABLED=args.live and args.web,
            ARGUMENT_GYM_BACKGROUND_RUNS=False,
        ):
            user = get_user_model().objects.create_user("seeded-flaw-benchmark")
            for flaw in suite["flaws"]:
                for variant in flaw["variants"]:
                    if args.variants and variant["id"] not in args.variants.split(","):
                        continue
                    for arm_name, arm in arms.items():
                        for repetition in range(args.runs):
                            result = run_variant(flaw, variant, arm_name, arm, user=user)
                            report.append({
                                "flaw": flaw["id"], "variant": variant["id"], "expectFlagged": variant["expect_flagged"],
                                "arm": arm_name, "run": repetition + 1, **result,
                            })
                            print(f"{flaw['id']}/{variant['id']:<12} {arm_name:<14} run {repetition + 1}: "
                                  f"library={'Y' if result['library'] else '-'} web={'Y' if result['web'] else '-'} "
                                  f"{result['status']}", flush=True)
    finally:
        runner.teardown_databases(old_config)

    print("\nVariant         arm             library  web    (flaw: higher is better; control: lower is better)")
    grouped = defaultdict(list)
    for row in report:
        grouped[(row["flaw"], row["variant"], row["arm"])].append(row)
    for (flaw_id, variant_id, arm_name), rows in grouped.items():
        library = sum(row["library"] for row in rows)
        web = sum(row["web"] for row in rows)
        kind = "flaw" if rows[0]["expectFlagged"] else "control"
        print(f"{variant_id:<15} {arm_name:<15} {library}/{len(rows):<6} {web}/{len(rows):<6} {kind}")
    if args.output:
        Path(args.output).write_text(json.dumps({"live": args.live, "results": report}, indent=2), encoding="utf-8")
        print(f"\nFull report: {args.output}")
    if args.live:
        from apps.ai.prompt_catalog import load_file_prompts

        models = sorted({prompt.default_model for key, prompt in load_file_prompts().items() if key.startswith("argument_gym.")})
        print(f"\nModel: the configured provider, using each gym prompt's default model ({', '.join(models)})")
    else:
        print("\nModel: off (deterministic floor)")


if __name__ == "__main__":
    main()
