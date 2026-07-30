"""Unit tests for system/utils.py and system/event_types.py."""

import time
import unittest

from system.utils import relative_time
from system.event_types import (
    StreamEvent,
    StreamEventType,
    ToolCallData,
    text_delta,
    tool_call_to_dict,
    parse_tool_call_from_dict,
)


class TestSystemUtils(unittest.TestCase):
    """Test relative time formatting utility."""

    def test_relative_time_just_now(self):
        now_ms = int(time.time() * 1000)
        self.assertEqual(relative_time(now_ms - 10000), "now")

    def test_relative_time_minutes(self):
        now_ms = int(time.time() * 1000)
        self.assertEqual(relative_time(now_ms - 180000), "3m ago")

    def test_relative_time_hours(self):
        now_ms = int(time.time() * 1000)
        self.assertEqual(relative_time(now_ms - 7200000), "2h ago")

    def test_relative_time_days(self):
        now_ms = int(time.time() * 1000)
        self.assertEqual(relative_time(now_ms - 172800000), "2d ago")


class TestEventTypes(unittest.TestCase):
    """Test StreamEvent creation and tool call parsing."""

    def test_text_delta_event(self):
        event = text_delta("Hello AI")
        self.assertEqual(event.type, StreamEventType.TEXT_DELTA)
        self.assertEqual(event.text_delta, "Hello AI")

    def test_tool_call_serialization(self):
        tool = ToolCallData(
            id="call_999",
            name="read_file",
            arguments='{"AbsolutePath": "/tmp/test.txt"}',
        )
        as_dict = tool_call_to_dict(tool)
        self.assertEqual(as_dict["id"], "call_999")
        self.assertEqual(as_dict["function"]["name"], "read_file")

        parsed = parse_tool_call_from_dict(as_dict)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.id, "call_999")
        self.assertEqual(parsed.name, "read_file")


if __name__ == "__main__":
    unittest.main()
