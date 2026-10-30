"""Finding selection must not manufacture issues or hide the argument."""
import json
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.argument_gym.pipeline import Stage, _unit_payload, judge_stage, select_challenges


class ActionabilityTests(SimpleTestCase):
    def test_no_minimum_and_rejected_attacks_stay_rejected(self):
        attacks = [{"id": "a1"}, {"id": "a2"}]
        assessments = [{"attackId": "a1", "keep": False, "verdict": "serious", "importance": 99},
                       {"attackId": "a2", "keep": True, "verdict": "weak", "importance": 20}]
        self.assertEqual(select_challenges(attacks, assessments), [])
        self.assertEqual(select_challenges(attacks, []), [])

    def test_rank_before_cap_and_only_remove_represented_duplicates(self):
        attacks = [{"id": f"a{i}"} for i in range(10)]
        assessments = [{"attackId": a["id"], "keep": True, "verdict": "serious", "importance": i}
                       for i, a in enumerate(attacks)]
        assessments[8]["duplicateOf"] = "a9"
        kept = select_challenges(attacks, assessments)
        self.assertEqual(kept[0]["id"], "a9")
        self.assertNotIn({"id": "a8"}, kept)
        self.assertEqual(len(kept), 7)
        assessments[9]["keep"] = False
        self.assertEqual(select_challenges(attacks, assessments)[0]["id"], "a8")

    def test_plain_heading_and_strict_boolean_survive_parser(self):
        class Client:
            def complete(self, **kwargs):
                return json.dumps({"assessments": [
                    {"attackId": "a1", "keep": "false", "verdict": "serious"},
                    {"attackId": "a2", "keep": True, "verdict": "answerable",
                     "finding": "The notice element has weak support"}]})
        with patch('apps.argument_gym.pipeline.ai_enabled', return_value=True), \
             patch('apps.argument_gym.pipeline.render_prompt'):
            assessments, trace = judge_stage([], [], [{"id": "a1"}, {"id": "a2"}], [],
                                             jurisdiction="Synthetic", llm_client=Client())
        self.assertFalse(assessments[0]['keep'])
        self.assertEqual(assessments[1]['finding'], 'The notice element has weak support')
        self.assertEqual(trace['method'], 'llm')

    def test_valid_empty_opponent_result_does_not_trigger_fabricated_fallback(self):
        class Client:
            def complete(self, **kwargs): return '{"attacks": []}'
        def forbidden(): raise AssertionError('Valid empty result must not fall back')
        with patch('apps.argument_gym.pipeline.ai_enabled', return_value=True), \
             patch('apps.argument_gym.pipeline.render_prompt'):
            items, trace = Stage('opponent', llm_client=Client()).run(
                prompt_key='argument_gym.opponent', context={}, parse=lambda p: p['attacks'],
                fallback=forbidden, empty_key='attacks')
        self.assertEqual(items, [])
        self.assertEqual(trace['method'], 'llm')

    def test_argument_beyond_eighty_units_and_long_paragraphs_are_read(self):
        units = [{"id": f"u{i}", "type": "argument", "text": "x" * 1200 + "Decisive answer.",
                  "locator": {"section": "III", "paragraph": i, "page": 1}} for i in range(110)]
        payload = _unit_payload(units)
        self.assertEqual(len(payload), 110)
        self.assertTrue(payload[-1]['text'].endswith('Decisive answer.'))

    def test_malformed_empty_response_still_records_fallback(self):
        class Client:
            def complete(self, **kwargs): return '{}'
        with patch('apps.argument_gym.pipeline.ai_enabled', return_value=True), \
             patch('apps.argument_gym.pipeline.render_prompt'):
            items, trace = Stage('opponent', llm_client=Client()).run(
                prompt_key='argument_gym.opponent', context={}, parse=lambda p: p.get('attacks', []),
                fallback=lambda: [{'fallback': True}], empty_key='attacks')
        self.assertEqual(items, [{'fallback': True}])
        self.assertEqual(trace['method'], 'deterministic')
        self.assertFalse(trace['trace'][0]['success'])

    def test_coach_cannot_offer_pasteable_concessions_for_verification_or_strategy(self):
        from apps.argument_gym.pipeline import coach_stage
        class Client:
            def complete(self, **kwargs):
                return json.dumps({'responses': [dict(attackId=key, recommendation='Check u91.',
                    suggestedResponse='We abandon this claim.') for key in ('a1', 'a2')]})
        units = [{'id': 'u91', 'type': 'argument', 'text': 'Argument',
                  'locator': {'section': 'III', 'paragraph': 12, 'page': 2}}]
        with patch('apps.argument_gym.pipeline.ai_enabled', return_value=True), \
             patch('apps.argument_gym.pipeline.render_prompt'):
            responses, _ = coach_stage(units, [{'attackId': 'a1', 'reviewAction': 'verify'},
                {'attackId': 'a2', 'reviewAction': 'strategy'}], [], [], jurisdiction='Synthetic', llm_client=Client())
        self.assertTrue(all(r['suggestedResponse'] == '' for r in responses))
        self.assertTrue(all(r['recommendation'] == 'Check section III, paragraph 12.' for r in responses))
