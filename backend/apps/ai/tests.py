from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.ai.case_chat import case_chat_reply
from apps.ai.case_chat import normalize_ai_text as normalize_chat_text
from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.ai.models import PromptOverride
from apps.ai.prompt_catalog import PromptCatalogError, PromptRenderError, get_prompt, load_file_prompts, render_prompt
from apps.ai.services import ConstrainedDraftingService, GenerationContext
from apps.ai.tool_loop import ToolEvaluation, run_tool_with_repair
from apps.matters.models import Matter
from apps.matters.serializers import matter_to_dict
from apps.sources.document_text import extract_text
from apps.sources.models import SourceConfiguration


class FakeChatCompletions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        message = SimpleNamespace(content="Generated section")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeOpenAIClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeChatCompletions())


class OpenAICompatibleClientTests(TestCase):
    def test_missing_or_blank_credentials_fail_before_creating_sdk_client(self):
        for key in (None, "", " \t\n"):
            with self.subTest(key=key), self.settings(OPENAI_API_KEY=key), \
                    patch("apps.ai.openai_client.OpenAI") as sdk:
                with self.assertRaisesRegex(OpenAIBackendError, "configuration.*API key"):
                    OpenAICompatibleClient()
                sdk.assert_not_called()

    @override_settings(OPENAI_API_KEY="")
    def test_injected_client_needs_no_credentials(self):
        fake_client = FakeOpenAIClient()
        client = OpenAICompatibleClient(client=fake_client, model="test-model")
        self.assertEqual(client.complete(system="System", user="User"), "Generated section")

    @override_settings(OPENAI_API_KEY="")
    def test_admin_and_explicit_credentials_work_without_environment_credentials(self):
        SourceConfiguration.objects.create(
            name="AI", kind="openai", openai_api_key="test-admin-key",
        )
        with patch("apps.ai.openai_client.OpenAI") as sdk:
            OpenAICompatibleClient()
            self.assertEqual(sdk.call_args.kwargs["api_key"], "test-admin-key")
            OpenAICompatibleClient(api_key="test-explicit-key")
            self.assertEqual(sdk.call_args.kwargs["api_key"], "test-explicit-key")

    def test_complete_uses_chat_completions_endpoint(self):
        fake_client = FakeOpenAIClient()
        client = OpenAICompatibleClient(client=fake_client, model="test-model")

        result = client.complete(system="System", user="User")

        self.assertEqual(result, "Generated section")
        request = fake_client.chat.completions.request
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["messages"][1]["content"], "User")

    def test_complete_uses_prompt_model_and_reasoning_level(self):
        fake_client = FakeOpenAIClient()
        client = OpenAICompatibleClient(client=fake_client, model="fallback-model")

        client.complete(
            system="System",
            user="User",
            model="prompt-model",
            reasoning_level="medium",
        )

        request = fake_client.chat.completions.request
        self.assertEqual(request["model"], "prompt-model")
        self.assertEqual(request["reasoning_effort"], "medium")

    def test_complete_omits_temperature_for_fixed_temperature_models(self):
        fake_client = FakeOpenAIClient()
        client = OpenAICompatibleClient(client=fake_client, model="gpt-5.4-mini")

        client.complete(system="System", user="User", temperature=0.1)

        self.assertNotIn("temperature", fake_client.chat.completions.request)

    def test_complete_retries_without_temperature_when_provider_rejects_it(self):
        class TemperatureRejectingCompletions:
            def __init__(self):
                self.requests = []

            def create(self, **kwargs):
                self.requests.append(kwargs)
                if "temperature" in kwargs:
                    raise RuntimeError(
                        "Unsupported value: 'temperature' does not support 0.1 with this model. "
                        "Only the default (1) value is supported."
                    )
                message = SimpleNamespace(content="Generated section")
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        completions = TemperatureRejectingCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        client = OpenAICompatibleClient(client=fake_client, model="provider-specific-model")

        result = client.complete(system="System", user="User", temperature=0.1)

        self.assertEqual(result, "Generated section")
        self.assertIn("temperature", completions.requests[0])
        self.assertNotIn("temperature", completions.requests[1])

    @override_settings(OPENAI_MODEL="env-model", OPENAI_API_KEY="env-key", OPENAI_BASE_URL="https://env.example/v1")
    def test_admin_source_configuration_overrides_openai_env_defaults(self):
        fake_client = FakeOpenAIClient()
        SourceConfiguration.objects.create(
            name="AI",
            kind="openai",
            openai_model="admin-model",
            openai_api_key="admin-key",
            openai_base_url="https://admin.example/v1",
            openai_enabled=True,
        )

        client = OpenAICompatibleClient(client=fake_client)
        client.complete(system="System", user="User")

        self.assertEqual(fake_client.chat.completions.request["model"], "admin-model")


class PromptCatalogTests(TestCase):
    def test_default_catalog_loads_all_expected_prompt_keys(self):
        prompts = load_file_prompts()

        self.assertEqual(
            set(prompts),
            {
                "drafting.constrained_section",
                "drafting.goal_recommendations",
                "drafting.letter",
                "drafting.plan",
                "drafting.template_fields",
                "triage.apply_rubric",
                "case_chat.document_ranking",
                "case_chat.document_summary",
                "case_chat.document_set_summary",
                "case_chat.suggest_actions",
                "case_chat.reply",
                "caselaw.search_keywords",
                "caselaw.metadata_extract",
                "caselaw.metadata_verify",
                "research.answer",
                "research.rerank",
                "research.treatise_relevance",
                "argument_gym.argument_map",
                "argument_gym.assessment",
                "argument_gym.benchmark",
                "argument_gym.rule_elements",
                "argument_gym.checklist",
                "argument_gym.record_audit",
                "argument_gym.research_queries",
                "argument_gym.opponent",
                "argument_gym.judge",
                "argument_gym.coach",
                "argument_gym.prep_sheet",
            },
        )

    def test_prompt_is_rendered_from_yaml_with_named_context(self):
        prompt = render_prompt(
            "drafting.constrained_section",
            allow_database_override=False,
            label="Argument",
            section_kind="argument",
            template_title="Conditions motion",
            matter_summary="Repairs needed",
            jurisdiction="Housing Court",
            client_name="Tenant",
            client_pronouns="they/them/theirs",
            household="Child One and Child Two",
            instructions="Focus on habitability.",
            facts="- Mold in bedroom",
            sources="- Inspection report",
            template_text="Preserve the statutory standard.",
            template_helpers="comma_and_list and pronoun_subjective",
            block_instructions="- Answer each argument in the opposition.",
            responding_to="This document answers: Plaintiff's Brief in Opposition.",
        )

        self.assertIn("Draft the Argument section", prompt.user)
        self.assertIn("- Answer each argument in the opposition.", prompt.user)
        self.assertIn("This document answers: Plaintiff's Brief in Opposition.", prompt.user)
        self.assertIn("- Mold in bedroom", prompt.user)
        self.assertIn("Preserve the statutory standard.", prompt.user)
        self.assertEqual(prompt.default_model, "gpt-5.5")
        self.assertEqual(prompt.default_reasoning_level, "medium")
        self.assertEqual(prompt.source, str(Path(settings.PROMPT_CATALOG_DIR) / "drafting.constrained_section.yaml"))

    def test_enabled_database_override_replaces_file_prompt(self):
        PromptOverride.objects.create(
            key="case_chat.reply",
            system="Override system for {client}",
            user="Override user for {client}",
            default_model="override-model",
            default_reasoning_level="medium",
        )

        prompt = render_prompt("case_chat.reply", client="Sam")

        self.assertEqual(prompt.system, "Override system for Sam")
        self.assertEqual(prompt.user, "Override user for Sam")
        self.assertEqual(prompt.default_model, "override-model")
        self.assertEqual(prompt.default_reasoning_level, "medium")
        self.assertEqual(prompt.source, "database override")

    def test_missing_context_is_reported_before_an_llm_request(self):
        with self.assertRaisesRegex(PromptRenderError, "matter_summary"):
            render_prompt("drafting.constrained_section", allow_database_override=False, label="Argument")

    def test_catalog_directory_can_be_swapped_for_benchmark_variant(self):
        with TemporaryDirectory() as directory:
            Path(directory, "benchmark.sample.yaml").write_text(
                "system prompt: System {name}\nuser prompt: User {name}\nsettings:\n  default model: benchmark-model\n  default reasoning level: high\n",
                encoding="utf-8",
            )
            with self.settings(PROMPT_CATALOG_DIR=Path(directory)):
                prompt = render_prompt("benchmark.sample", allow_database_override=False, name="variant")

        self.assertEqual(prompt.system, "System variant")
        self.assertEqual(prompt.user, "User variant")
        self.assertEqual(prompt.default_model, "benchmark-model")
        self.assertEqual(prompt.default_reasoning_level, "high")

    def test_invalid_catalog_schema_fails_clearly(self):
        with TemporaryDirectory() as directory:
            Path(directory, "broken.yaml").write_text("system prompt: System\n", encoding="utf-8")
            with self.settings(PROMPT_CATALOG_DIR=Path(directory)):
                with self.assertRaisesRegex(PromptCatalogError, "user prompt"):
                    get_prompt("anything", allow_database_override=False)


class ToolLoopTests(TestCase):
    def test_failed_postcondition_can_repair_and_retry_once(self):
        def execute(plan):
            return [] if plan["strict"] else ["document-1"]

        def evaluate(_plan, result):
            return ToolEvaluation(bool(result), "found" if result else "empty")

        loop = run_tool_with_repair(
            {"strict": True},
            execute=execute,
            evaluate=evaluate,
            repair=lambda plan, _result, _evaluation: {**plan, "strict": False},
            max_attempts=2,
        )

        self.assertTrue(loop.success)
        self.assertEqual(loop.result, ["document-1"])
        self.assertEqual([attempt["code"] for attempt in loop.trace()], ["empty", "found"])


class DraftingServiceLLMTests(TestCase):
    def test_fact_slug_recommendation_does_not_confuse_legal_help_with_rental_assistance(self):
        service = ConstrainedDraftingService()

        legal_help = service.recommend_fact_slugs(
            SimpleNamespace(summary="Tenant needs assistance drafting an answer for unpaid rent.")
        )
        rental_help = service.recommend_fact_slugs(
            SimpleNamespace(summary="Tenant has a pending emergency rental assistance application.")
        )

        self.assertNotIn("rental-assistance", legal_help)
        self.assertIn("rental-assistance", rental_help)

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_constrained_generation_uses_openai_compatible_client(self):
        captured_request = {}

        def complete(**kwargs):
            captured_request.update(kwargs)
            return "LLM body"

        fake_llm = SimpleNamespace(complete=complete)
        service = ConstrainedDraftingService(llm_client=fake_llm)
        matter = SimpleNamespace(summary="Repairs needed", jurisdiction="Housing Court", client_name="Tenant")
        fact = SimpleNamespace(text="There is mold in the bedroom.", source_label="LegalServer note")
        context = GenerationContext(
            matter=matter,
            selected_facts=[fact],
            selected_curated_facts=[],
            selected_sources=[{"title": "Guide", "snippet": "Use repair evidence."}],
            template=SimpleNamespace(),
            mode="draft_from_scratch",
            instructions="Focus on habitability.",
        )

        body = service.generate_constrained_section(label="Argument", context=context, fallback="Fallback")

        self.assertEqual(body, "LLM body")
        self.assertEqual(
            captured_request["system"],
            "You draft constrained legal document sections from supplied facts, approved sources, and maintained template language.\n",
        )
        self.assertIn("Draft the Argument section", captured_request["user"])
        self.assertIn("There is mold in the bedroom.", captured_request["user"])
        self.assertIn("Fallback", captured_request["user"])
        self.assertEqual(captured_request["model"], "gpt-5.5")
        self.assertEqual(captured_request["reasoning_level"], "medium")

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_fact_block_uses_llm_to_turn_evidence_into_template_shaped_prose(self):
        captured_request = {}

        def complete(**kwargs):
            captured_request.update(kwargs)
            return "Tenant reported the leak before the eviction was filed."

        block = SimpleNamespace(
            key="facts",
            label="Statement of Facts",
            block_type="facts",
            ai_fill_mode="constrained_generation",
            body="Defendant resides at {{ fields.premises_address }}.",
            required=True,
            supporting_sources=[],
        )
        template = SimpleNamespace(
            title="Housing motion",
            jurisdiction="Housing Court",
            blocks=SimpleNamespace(all=lambda: [block]),
        )
        fact = SimpleNamespace(
            text="INTAKE NOTES: - Tenant reported a leak before filing.",
            source_label="Case note 1",
        )
        context = GenerationContext(
            matter=SimpleNamespace(
                summary="Leak reported",
                jurisdiction="Housing Court",
                client_name="Tenant",
                external_id="CASE-1",
            ),
            selected_facts=[fact],
            selected_curated_facts=[],
            selected_sources=[],
            template=template,
            mode="draft_from_template",
            template_data={},
        )

        sections = ConstrainedDraftingService(
            llm_client=SimpleNamespace(complete=complete)
        ).compose_document(context, ["facts"])

        self.assertEqual(sections[0]["body"], "Tenant reported the leak before the eviction was filed.")
        self.assertIn("section (facts)", captured_request["user"])
        self.assertIn("Defendant resides at [Premises Address].", captured_request["user"])
        self.assertIn("Template language is a form and drafting model, not evidence", captured_request["user"])
        self.assertIn("Case note 1", sections[0]["sources"])

    def test_template_signature_includes_bar_number(self):
        context = GenerationContext(
            matter=Matter(client_name="Tenant", raw_payload={}),
            selected_facts=[], selected_curated_facts=[], selected_sources=[],
            template=None, mode="draft_from_template",
            author_profile={"displayName": "Example Advocate", "barNumber": "0123456"},
        )
        service = ConstrainedDraftingService()
        self.assertEqual(
            service.render_template_body("{{ advocate_name_and_bar }}", context),
            "Example Advocate (0123456)",
        )
        context.author_profile.pop("barNumber")
        self.assertEqual(
            service.render_template_body("{{ advocate_name_and_bar }}", context),
            "Example Advocate",
        )

    def test_template_rendering_fills_named_case_fields_before_model_workflow(self):
        matter = Matter.objects.create(
            external_id="CASE-FIELDS-1",
            client_name="Tenant",
            jurisdiction="Housing Court",
            raw_payload={
                "custom_fields": {
                    "Plaintiff Name": "Example Homes LLC",
                    "Filing Date": "July 12, 2026",
                }
            },
        )
        context = GenerationContext(
            matter=matter,
            selected_facts=[],
            selected_curated_facts=[],
            selected_sources=[],
            template=SimpleNamespace(jurisdiction="Housing Court"),
            mode="draft_from_template",
            template_data={},
        )

        rendered = ConstrainedDraftingService().render_template_body(
            "The landlord is [Plaintiff Name]. Filing date: [Filing Date].",
            context,
        )

        self.assertEqual(
            rendered,
            "The landlord is Example Homes LLC. Filing date: July 12, 2026.",
        )


class CaseChatTests(TestCase):
    def test_chat_text_normalizes_html_breaks(self):
        self.assertEqual(normalize_chat_text("One<br/>Two<br>Three"), "One\nTwo\nThree")

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_document_question_uses_case_documents(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )

        with patch("apps.ai.case_chat.get_case_documents", return_value=[{"title": "Lease.pdf", "id": "doc-1"}]):
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "What documents does this case have?"}],
            )

        self.assertIn("Lease.pdf", reply["message"])
        self.assertEqual(reply["toolsUsed"], ["legalserver.documents"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_note_question_uses_case_notes_tool(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
            raw_payload={
                "notes": [
                    {
                        "subject": "Documents Received",
                        "body": "Documents received via webhook.",
                        "date_posted": "2026-03-08",
                        "created_by": {"user_name": "Docassemble API"},
                    }
                ]
            },
        )

        reply = case_chat_reply(
            matter=matter,
            messages=[{"role": "user", "content": "Does it have any case notes?"}],
        )

        self.assertIn("Documents Received", reply["message"])
        self.assertEqual(reply["toolsUsed"], ["legalserver.case_notes"])
        self.assertEqual(reply["toolResults"]["case_notes"][0]["createdBy"], "Docassemble API")

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_note_tool_results_are_answered_deterministically(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
            raw_payload={"notes": [{"subject": "Documents Received", "body": "Documents received via webhook."}]},
        )

        reply = case_chat_reply(
            matter=matter,
            messages=[{"role": "user", "content": "Does it have any case notes?"}],
            llm_client=SimpleNamespace(complete_messages=lambda **kwargs: "No notes."),
        )

        self.assertIn("Documents Received", reply["message"])
        self.assertIn("legalserver.case_notes", reply["toolsUsed"])

    def test_matter_serializer_does_not_return_raw_note_json_as_summary(self):
        matter = Matter.objects.create(
            external_id="LS-2",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
            summary="[{'body': 'raw note json'}]",
            raw_payload={"case_title": "Readable case title", "case_number": "26-0000009"},
        )

        data = matter_to_dict(matter)

        self.assertEqual(data["summary"], "Readable case title")
        self.assertIn({"label": "Case number", "value": "26-0000009"}, data["details"])

    def test_stdlib_text_extractor_handles_plain_text(self):
        result = extract_text(b"Hello from a pleading", filename="motion.txt")

        self.assertEqual(result["extractor"], "stdlib")
        self.assertEqual(result["text"], "Hello from a pleading")

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_document_text_question_extracts_relevant_document(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Lease.txt", "id": "doc-1", "url": "https://legalserver.example/doc-1"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.return_value = {
                "content": b"Tenant lease text",
                "content_type": "text/plain",
                "filename": "Lease.txt",
            }
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "What does Lease.txt say?"}],
            )

        self.assertIn("Tenant lease text", reply["message"])
        self.assertIn("document.extract_text", reply["toolsUsed"])
        self.assertEqual(reply["toolResults"]["document_text"]["extractor"], "stdlib")

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_document_summary_language_triggers_extraction(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Lease - 56_VII_Steenhuis.txt", "id": "doc-1", "url": "https://legalserver.example/doc-1"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.return_value = {
                "content": b"This lease is for unit 56. Tenant pays monthly rent. This sentence should not be needed.",
                "content_type": "text/plain",
                "filename": "Lease.txt",
            }
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Tell me what the Lease document is all about"}],
            )

        self.assertIn("Summary of Lease", reply["message"])
        self.assertIn("This lease is for unit 56", reply["message"])
        self.assertIn("document.extract_text", reply["toolsUsed"])
        self.assertIn("document.summarize", reply["toolsUsed"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_collective_document_summary_does_not_treat_importance_words_as_a_filename(self):
        matter = Matter.objects.create(
            external_id="26-0000045",
            client_name="Eleanor Vance",
            matter_type="Private Landlord/Tenant",
            jurisdiction="Housing Court",
        )
        documents = [
            {"title": "Summons_and_Complaint_Eleanor_Vance.txt", "id": "doc-1", "url": "https://legalserver.example/complaint.txt", "mimeType": "text/plain"},
            {"title": "Summons_and_Complaint_Eleanor_Vance.pdf", "id": "doc-2", "url": "https://legalserver.example/complaint.pdf", "mimeType": "application/pdf"},
            {"title": "Lease_Agreement_Eleanor_Vance.txt", "id": "doc-3", "url": "https://legalserver.example/lease.txt", "mimeType": "text/plain"},
        ]

        def download(url):
            if url.endswith("complaint.txt"):
                return {"content": b"The complaint seeks possession. The answer is due August 10.", "content_type": "text/plain", "filename": "complaint.txt"}
            return {"content": b"The lease states monthly rent is $900.", "content_type": "text/plain", "filename": "lease.txt"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=documents), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.side_effect = download
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "summarize the most important documents in this matter"}],
            )

        self.assertIn("Summary of the highest-salience case documents", reply["message"])
        self.assertIn("complaint seeks possession", reply["message"])
        self.assertIn("lease states monthly rent", reply["message"])
        self.assertNotIn("could not find a matching case document", reply["message"])
        self.assertEqual(len(reply["toolResults"]["documents"]), 3)
        self.assertEqual(len(reply["toolResults"]["document_texts"]), 2)
        self.assertEqual(
            [attempt["code"] for attempt in reply["toolResults"]["toolTrace"]["documentSelection"]],
            ["overconstrained_document_match", "documents_selected"],
        )
        self.assertEqual(
            reply["toolsUsed"],
            ["legalserver.documents", "document.rank_salience", "document.extract_text", "document.summarize"],
        )

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_most_important_uses_llm_ranking_and_extracts_only_high_salience_groups(self):
        matter = Matter.objects.create(
            external_id="LS-RANK-1",
            client_name="Eleanor Vance",
            matter_type="Private Landlord/Tenant",
            jurisdiction="Housing Court",
        )
        documents = [
            {"title": "Intake_Notes.txt", "id": "intake", "url": "https://legalserver.example/intake.txt", "mimeType": "text/plain"},
            {"title": "Summons_and_Complaint.txt", "id": "complaint", "url": "https://legalserver.example/complaint.txt", "mimeType": "text/plain"},
            {"title": "Notice_to_Quit.txt", "id": "notice", "url": "https://legalserver.example/notice.txt", "mimeType": "text/plain"},
        ]
        responses = iter(
            [
                '{"ranked_documents": [{"id": "document-group-2", "reason": "Defines the claims and response deadline."}, {"id": "document-group-3", "reason": "States the asserted termination basis."}]}',
                "The complaint and notice are the high-salience documents for the response.",
            ]
        )
        llm_client = SimpleNamespace(complete=lambda **_kwargs: next(responses))

        def download(url):
            filename = url.rsplit("/", 1)[-1]
            return {"content": f"Extracted text for {filename}.".encode(), "content_type": "text/plain", "filename": filename}

        with patch("apps.ai.case_chat.get_case_documents", return_value=documents), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.side_effect = download
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Summarize the most important documents in this matter"}],
                llm_client=llm_client,
            )

        extracted_titles = [item["document"]["title"] for item in reply["toolResults"]["document_texts"]]
        self.assertEqual(extracted_titles, ["Summons_and_Complaint.txt", "Notice_to_Quit.txt"])
        self.assertNotIn("Intake_Notes.txt", extracted_titles)
        self.assertEqual(reply["toolResults"]["document_ranking"]["method"], "llm")
        self.assertIn("complaint and notice", reply["message"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_document_extraction_retries_an_alternate_copy_after_empty_text(self):
        matter = Matter.objects.create(
            external_id="LS-RETRY-1",
            client_name="Eleanor Vance",
            matter_type="Private Landlord/Tenant",
            jurisdiction="Housing Court",
        )
        documents = [
            {"title": "Lease_Agreement_Eleanor_Vance.txt", "id": "named-copy", "url": "https://legalserver.example/named.txt", "mimeType": "text/plain"},
            {"title": "Lease_Agreement.txt", "id": "plain-copy", "url": "https://legalserver.example/plain.txt", "mimeType": "text/plain"},
        ]

        def download(url):
            content = b"" if url.endswith("named.txt") else b"The alternate copy contains the lease terms."
            return {"content": content, "content_type": "text/plain", "filename": url.rsplit("/", 1)[-1]}

        with patch("apps.ai.case_chat.get_case_documents", return_value=documents), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.side_effect = download
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Summarize the lease documents"}],
            )

        extraction_attempts = reply["toolResults"]["toolTrace"]["documentExtraction"][0]["attempts"]
        self.assertEqual(
            [attempt["code"] for attempt in extraction_attempts],
            ["document_extraction_failed", "document_text_extracted"],
        )
        self.assertIn("alternate copy contains the lease terms", reply["message"])

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_document_summary_can_use_llm_without_returning_full_text(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Lease.txt", "id": "doc-1", "url": "https://legalserver.example/doc-1"}
        long_text = b"Sentence one. Sentence two. Sentence three. Sentence four."

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.return_value = {
                "content": long_text,
                "content_type": "text/plain",
                "filename": "Lease.txt",
            }
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Summarize this document"}],
                llm_client=SimpleNamespace(complete=lambda **kwargs: "Short lease summary."),
            )

        self.assertIn("Short lease summary", reply["message"])
        self.assertNotIn("Sentence four", reply["message"])
        self.assertIn("document.summarize", reply["toolsUsed"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_raw_text_request_returns_extracted_text(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Lease.txt", "id": "doc-1", "url": "https://legalserver.example/doc-1"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.return_value = {
                "content": b"Full lease text.",
                "content_type": "text/plain",
                "filename": "Lease.txt",
            }
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Show me the full text of Lease.txt"}],
            )

        self.assertIn("Extracted text", reply["message"])
        self.assertIn("Full lease text", reply["message"])
        self.assertNotIn("document.summarize", reply["toolsUsed"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_do_it_reuses_prior_document_extraction_request(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Lease - 56_VII_Steenhuis.txt", "id": "doc-1", "url": "https://legalserver.example/doc-1"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            client_class.return_value.download_document.return_value = {
                "content": b"Lease text from the follow-up request.",
                "content_type": "text/plain",
                "filename": "Lease.txt",
            }
            reply = case_chat_reply(
                matter=matter,
                messages=[
                    {"role": "user", "content": "Tell me what the Lease document is all about"},
                    {"role": "assistant", "content": "A document content retrieval API call would be needed."},
                    {"role": "user", "content": "do it"},
                ],
            )

        self.assertIn("Lease text from the follow-up request", reply["message"])
        self.assertIn("document.extract_text", reply["toolsUsed"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_specific_document_request_does_not_extract_unrelated_document(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )
        document = {"title": "Chart.png", "id": "doc-1", "url": "https://legalserver.example/doc-1"}

        with patch("apps.ai.case_chat.get_case_documents", return_value=[document]), patch(
            "apps.ai.case_chat.LegalServerClient"
        ) as client_class:
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "Tell me what the Lease document is all about"}],
            )

        self.assertIn("matching case document", reply["message"])
        self.assertNotIn("document.extract_text", reply["toolsUsed"])
        client_class.return_value.download_document.assert_not_called()

    @override_settings(AI_DRAFTING_ENABLED=True)
    def test_document_listing_uses_deterministic_tool_answer_even_with_ai_enabled(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
        )

        with patch("apps.ai.case_chat.get_case_documents", return_value=[{"title": "Lease.pdf", "id": "doc-1"}]):
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "any documents in this case?"}],
                llm_client=SimpleNamespace(complete_messages=lambda **kwargs: "No documents."),
            )

        self.assertIn("Lease.pdf", reply["message"])
        self.assertEqual(reply["toolsUsed"], ["legalserver.documents"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_timeline_question_builds_deterministic_timeline(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
            raw_payload={
                "case_number": "26-0000009",
                "date_opened": "2026-03-08",
                "notes": [{"subject": "Documents Received", "body": "Documents received.", "date_posted": "2026-03-09"}],
            },
        )

        with patch("apps.ai.case_chat.get_case_documents", return_value=[{"title": "Lease.pdf", "date": "2026-03-10"}]):
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "What's happened in this case so far?"}],
            )

        self.assertIn("case.timeline", reply["toolsUsed"])
        self.assertIn("Documents Received", reply["message"])
        self.assertIn("Lease.pdf", reply["message"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_next_step_question_returns_action_cards(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="API TestOne",
            matter_type="Housing",
            jurisdiction="Housing Court",
            raw_payload={"notes": [{"subject": "Intake", "body": "Client needs a motion."}]},
        )

        with patch("apps.ai.case_chat.get_case_documents", return_value=[{"title": "Complaint.pdf", "id": "doc-1"}]):
            reply = case_chat_reply(
                matter=matter,
                messages=[{"role": "user", "content": "What's the next step I should take?"}],
            )

        self.assertIn("case.suggest_actions", reply["toolsUsed"])
        self.assertGreaterEqual(len(reply["actions"]), 2)
        self.assertIn("custom_motion", {action["type"] for action in reply["actions"]})
