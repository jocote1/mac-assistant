"""Schedule and reminder regressions without API calls or native mutations."""
import unittest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from ai_pipeline import ActionPlan, PlannedAction, ReadCalendarSlots, ReadRemindersSlots
from test_improvements import pipeline


class ScheduleReadingTests(unittest.TestCase):
    def setUp(self):
        self.assistant = pipeline()
        self.day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self.end = self.day + timedelta(days=1)
        self.task = {"id": "task", "title": "Ask host dad if William can join",
                     "due_date": (self.day + timedelta(hours=14, minutes=30)).isoformat(),
                     "completed": False, "list": "Reminders", "has_due_time": True}

    def test_exact_followup_reads_existing_reminder_without_writes(self):
        assistant = self.assistant
        assistant.last_turn_timestamp = time.time()
        assistant.conversation_history = [
            {"role": "user", "content": "Read my promotions email"},
            {"role": "assistant", "content": "Check email for ongoing promotions and deals."},
        ]
        assistant._call_structured_llm = Mock(return_value=ReadRemindersSlots())
        with patch("ai_pipeline.macos_bridge.get_reminders", return_value=[self.task]) as read, \
             patch("ai_pipeline.macos_bridge.create_reminder") as create:
            result = assistant.execute_pipeline("Like, do I have any reminders, or...?")
        read.assert_called_once_with(include_completed=False)
        create.assert_not_called()
        self.assertEqual(result["intent"], "read_reminders")
        self.assertIn(self.task["title"], result["spoken_response"])
        self.assertIn("2:30 PM", result["spoken_response"])
        self.assertNotIn("promotions", result["spoken_response"])
        assistant._call_structured_llm.assert_not_called()

    def test_reminders_set_is_a_question_not_a_creation(self):
        self.assistant._call_structured_llm = Mock(side_effect=AssertionError("Planner must not run"))
        action = self.assistant.classify_action_plan("Do I have any reminders set for today?").actions[0]
        self.assertEqual(action.intent, "read_reminders")

    def test_explicit_mixed_read_and_write_still_uses_planner(self):
        expected = ActionPlan(actions=[PlannedAction(intent="read_reminders", request="read reminders"),
            PlannedAction(intent="create_reminder", request="remind me to buy milk")])
        self.assistant._call_structured_llm = Mock(return_value=expected)
        self.assertEqual(self.assistant.classify_action_plan(
            "Do I have reminders, and remind me to buy milk?"), expected)
        self.assistant._call_structured_llm.assert_called_once()

    def test_default_reminder_read_includes_undated_and_future(self):
        undated = {**self.task, "id": "undated", "title": "Buy milk", "due_date": None}
        future = {**self.task, "due_date": (self.end + timedelta(days=4)).isoformat()}
        self.assistant._call_structured_llm = Mock(return_value=ReadRemindersSlots())
        with patch("ai_pipeline.macos_bridge.get_reminders", return_value=[undated, future]):
            result = self.assistant._handle_read_reminders("Any reminders?", "")
        self.assertEqual(len(result["reminders"]), 2)
        self.assertIn("no due date", result["spoken_response"])

    def test_date_window_uses_local_time_and_exclusive_end(self):
        outside = {**self.task, "id": "tomorrow", "due_date": self.end.isoformat()}
        undated = {**self.task, "id": "undated", "due_date": None}
        completed = {**self.task, "id": "done", "completed": True}
        with patch("ai_pipeline.macos_bridge.get_reminders", return_value=[outside, undated, completed, self.task]):
            result = self.assistant._reminders_in_window(self.day, self.end)
        self.assertEqual(result, [self.task])

    def test_date_only_reminder_does_not_speak_midnight(self):
        result = self.assistant._describe_reminders([{**self.task, "has_due_time": False,
                                                     "due_date": self.day.isoformat()}])
        self.assertNotIn("12 AM", result)
        self.assertIn(self.task["title"], result)

    def test_calendar_includes_reminder_duration_and_location(self):
        self.assistant._call_structured_llm = Mock(return_value=ReadCalendarSlots(
            start_iso=self.day.isoformat(), end_iso=self.end.isoformat(), spoken_response=""))
        event = {"title": "Escape Room", "start_iso": (self.day + timedelta(hours=16)).isoformat(),
                 "end_iso": (self.day + timedelta(hours=20)).isoformat(), "location": "Vienna"}
        with patch("ai_pipeline.macos_bridge.get_calendar_events", return_value=[event]), \
             patch("ai_pipeline.macos_bridge.get_reminders", return_value=[self.task]):
            result = self.assistant.execute_pipeline("What is my calendar looking like today?")
        self.assertIn("Escape Room from 4 PM to 8 PM in Vienna", result["spoken_response"])
        self.assertIn(self.task["title"], result["spoken_response"])
        self.assertEqual(result["reminders"], [self.task])
        self.assistant._call_structured_llm.assert_not_called()

    def test_permission_failure_never_claims_no_reminders(self):
        self.assistant._call_structured_llm = Mock(return_value=ReadRemindersSlots())
        with patch("ai_pipeline.macos_bridge.get_reminders", side_effect=PermissionError("Reminders access denied")):
            result = self.assistant.execute_pipeline("Do I have any reminders?")
        self.assertFalse(result["executed"])
        self.assertIn("access denied", result["spoken_response"])
        self.assertNotIn("don't have", result["spoken_response"])

    def test_calendar_survives_unavailable_reminders_without_claiming_empty(self):
        self.assistant._call_structured_llm = Mock(return_value=ReadCalendarSlots(
            start_iso=self.day.isoformat(), end_iso=self.end.isoformat(), spoken_response=""))
        with patch("ai_pipeline.macos_bridge.get_calendar_events", return_value=[]), \
             patch("ai_pipeline.macos_bridge.get_reminders", side_effect=PermissionError("access denied")):
            result = self.assistant._handle_read_calendar("today", "", self.day)
        self.assertIsNone(result["reminders"])
        self.assertIn("couldn't check your reminders", result["spoken_response"])

    def test_invalid_window_does_not_read_native_store(self):
        self.assistant._call_structured_llm = Mock(return_value=ReadRemindersSlots(
            start_iso=self.end.isoformat(), end_iso=self.day.isoformat()))
        with patch("ai_pipeline.macos_bridge.get_reminders") as read:
            result = self.assistant.execute_pipeline("What reminders are due between Monday and Tuesday?")
        read.assert_not_called()
        self.assertFalse(result["executed"])

    def test_large_reminder_list_reports_omitted_count(self):
        tasks = [{**self.task, "title": f"Task {index}"} for index in range(8)]
        spoken = self.assistant._describe_reminders(tasks)
        self.assertIn("3 more open reminders", spoken)
        self.assertNotIn("Task 5", spoken)

    def test_fast_window_preserves_complex_filters_for_model(self):
        for query in ("reminders on October 27", "reminders after 3 PM", "reminders in Work",
                      "calendar next week", "reminders that day", "calendar today and tomorrow"):
            with self.subTest(query=query):
                self.assertIsNone(self.assistant._simple_read_window(query, self.day))

    def test_fast_window_resolves_tomorrow_and_unbounded_reminders(self):
        start, end = self.assistant._simple_read_window("Show my calendar tomorrow", self.day)
        self.assertEqual(start.date(), (self.day + timedelta(days=1)).date())
        self.assertEqual(end.date(), (self.day + timedelta(days=2)).date())
        self.assertEqual(self.assistant._simple_read_window("Any reminders?", self.day, reminders=True), (None, None))

    def test_schedule_as_noun_uses_fast_route(self):
        self.assistant._call_structured_llm = Mock(side_effect=AssertionError("Planner must not run"))
        action = self.assistant.classify_action_plan("What's my schedule today?").actions[0]
        self.assertEqual(action.intent, "read_calendar")

    def test_schedule_as_verb_preserves_requested_creation(self):
        plan = ActionPlan(actions=[PlannedAction(intent="create_calendar_event", request="schedule a meeting")])
        self.assistant._call_structured_llm = Mock(return_value=plan)
        self.assertEqual(self.assistant.classify_action_plan("Show my calendar and schedule a meeting"), plan)
        self.assistant._call_structured_llm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
