"""A chat thread named in a URL is that thread, or nothing.

Before, an unknown thread id answered with an empty conversation that looked
like a real one, simply opening the chat screen created a conversation, and a
message sent from a window that had fallen behind landed in whichever
conversation was current.
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.ai.models import ChatConversation
from apps.matters.models import Matter

REPLY = {"message": "Noted.", "toolsUsed": [], "actions": []}


@patch("apps.matters.views.case_chat_reply", return_value=REPLY)
class CaseChatThreadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "a@example.com", "pw", is_staff=True)
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk}, external_id="MID-1", client_name="Jane", matter_type="Eviction", jurisdiction="X")
        self.url = "/api/cases/MID-1/chat/"

    def send(self, content, **extra):
        return self.client.post(self.url, data=json.dumps({"content": content, **extra}), content_type="application/json")

    def new_chat(self):
        return self.client.post(self.url, data=json.dumps({"action": "new_thread"}), content_type="application/json")

    def test_opening_the_chat_creates_nothing(self, _reply):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], [])
        self.assertIsNone(response.json()["threadId"])
        self.assertFalse(ChatConversation.objects.exists())

    def test_a_sent_message_reports_its_thread(self, _reply):
        thread_id = self.send("Hello").json()["threadId"]
        self.assertEqual(self.client.get(self.url, {"threadId": thread_id}).json()["messages"][0]["content"], "Hello")

    def test_an_unknown_thread_is_not_found_rather_than_empty(self, _reply):
        self.send("Hello")
        self.assertEqual(self.client.get(self.url, {"threadId": 999999}).status_code, 404)

    def test_another_users_thread_is_not_found(self, _reply):
        other = get_user_model().objects.create_user("other", "o@example.com", "pw", is_staff=True)
        theirs = ChatConversation.objects.create(user=other, kind=ChatConversation.CASE, scope_key=str(self.matter.id))
        self.assertEqual(self.client.get(self.url, {"threadId": theirs.id}).status_code, 404)

    def test_an_archived_thread_still_opens_by_its_id(self, _reply):
        old = self.send("First chat").json()["threadId"]
        self.new_chat()
        self.send("Second chat")
        response = self.client.get(self.url, {"threadId": old}).json()
        self.assertEqual([m["content"] for m in response["messages"] if m["role"] == "user"], ["First chat"])
        self.assertNotEqual(response["currentThreadId"], old)

    def test_a_message_for_a_thread_that_is_no_longer_current_is_refused(self, _reply):
        old = self.send("First chat").json()["threadId"]
        self.new_chat()  # another window
        response = self.send("Meant for the first chat", threadId=old)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["conflict"], "thread")
        contents = list(
            ChatConversation.objects.get(id=old).messages.values_list("content", flat=True)
        )
        self.assertNotIn("Meant for the first chat", contents)

    def test_clearing_names_the_thread_it_means(self, _reply):
        old = self.send("First chat").json()["threadId"]
        self.new_chat()
        self.send("Second chat")
        self.assertEqual(self.client.delete(f"{self.url}?threadId={old}").status_code, 409)
        self.assertTrue(ChatConversation.objects.get(id=old).messages.exists())
