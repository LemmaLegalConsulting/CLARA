"""Seeded-flaw briefs, checked offline against the maintained rule profiles.

The briefs live in ``content/argument-gym/seeded-flaws.yaml`` so the live
comparison script reads the same text. Offline, with no model, only what a
rule profile can see deterministically is caught; each variant states whether
that is expected (``offline_flagged``). A variant a model must catch is listed
as not flagged offline, which is the honest baseline rather than a pass.
"""

from django.test import TestCase
from django.test.utils import override_settings

import yaml

from apps.argument_gym.rule_audit import challenges_from_audit, run_rule_audit
from apps.core.content_library import content_path
from apps.rules.legal_rules import detect_invoked_rules, load_legal_rule_file
from apps.rules.models import LegalRuleProfile


def seeded_flaws():
    return yaml.safe_load(content_path("argument-gym", "seeded-flaws.yaml").read_text(encoding="utf-8"))


def profile_for(slug):
    seed = load_legal_rule_file(content_path("legal-rules", f"{slug}.yaml"))
    return LegalRuleProfile(**seed)


@override_settings(AI_DRAFTING_ENABLED=False)
class SeededFlawOfflineTests(TestCase):
    def test_the_suite_is_synthetic_and_every_flaw_has_a_control(self):
        suite = seeded_flaws()
        self.assertIs(suite["synthetic"], True)
        for flaw in suite["flaws"]:
            expectations = [variant["expect_flagged"] for variant in flaw["variants"]]
            self.assertIn(True, expectations, flaw["id"])
            self.assertIn(False, expectations, f"{flaw['id']} has no control, so a false positive would pass")

    def test_each_variant_is_flagged_offline_exactly_when_the_suite_says(self):
        for flaw in seeded_flaws()["flaws"]:
            profile = profile_for(flaw["rule_slug"])
            for variant in flaw["variants"]:
                with self.subTest(flaw=flaw["id"], variant=variant["id"]):
                    audits, _traces = run_rule_audit(
                        variant["brief"], [], jurisdiction="Ohio", profiles=[profile]
                    )
                    self.assertTrue(audits, "the rule profile did not detect that the brief invoked it")
                    attacks = [
                        attack
                        for attack in challenges_from_audit(audits)
                        if attack["elementId"] == flaw["element_id"]
                    ]
                    self.assertEqual(bool(attacks), variant["offline_flagged"], attacks)

    def test_a_federal_rule_is_invoked_by_a_brief_filed_in_a_state(self):
        profile = profile_for("cares-4024-notice")
        brief = seeded_flaws()["flaws"][0]["variants"][0]["brief"]
        invoked = detect_invoked_rules(brief, profiles=[profile], jurisdiction="Ohio")
        self.assertEqual([rule["invokedBy"] for rule in invoked], ["citation"])

    def test_a_contradiction_finding_quotes_the_brief_and_says_the_list_is_unverified(self):
        flaw = seeded_flaws()["flaws"][0]
        explicit = next(variant for variant in flaw["variants"] if variant["id"] == "explicit")
        audits, _traces = run_rule_audit(explicit["brief"], [], profiles=[profile_for(flaw["rule_slug"])])
        attack = next(item for item in challenges_from_audit(audits) if item["elementId"] == flaw["element_id"])
        self.assertIn("federal subsidy", attack["quote"])
        self.assertIn("unverified", attack["whyItMatters"])
        # Unverified lists warn; only a verified one may report an error.
        self.assertEqual(attack["severity"], "warning")
