import logging
import queue
import tempfile
import unittest
from pathlib import Path

from edge_service_zkteco.file_event_source import FileEventSource


def _logger():
    logger = logging.getLogger("test-file-event-source")
    logger.addHandler(logging.NullHandler())
    return logger


class FileEventSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.input_file = Path(self._tmpdir.name) / "input.txt"
        self.events = queue.Queue()
        self.source = FileEventSource(
            input_file=self.input_file,
            logger=_logger(),
            on_event=self.events.put,
            poll_interval_seconds=0.01,
        )
        self.source.start()
        self.addCleanup(self.source.stop)

    def _append(self, line):
        with self.input_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def test_plain_line_becomes_user_id_event(self):
        self._append("1001")

        event = self.events.get(timeout=3.0)
        self.assertEqual(event["user_id"], "1001")
        self.assertTrue(event["timestamp"])

    def test_json_line_is_parsed_as_event(self):
        self._append('{"user_id": "2002", "timestamp": "2026-07-02T10:00:00", "punch": 1}')

        event = self.events.get(timeout=3.0)
        self.assertEqual(event["user_id"], "2002")
        self.assertEqual(event["timestamp"], "2026-07-02T10:00:00")
        self.assertEqual(event["punch"], 1)

    def test_only_new_lines_after_start_are_emitted(self):
        self._append("first")
        self.events.get(timeout=3.0)
        self._append("second")

        event = self.events.get(timeout=3.0)
        self.assertEqual(event["user_id"], "second")
        self.assertTrue(self.events.empty())

    def test_invalid_json_line_is_ignored(self):
        self._append("{broken json")
        self._append("3003")

        event = self.events.get(timeout=3.0)
        self.assertEqual(event["user_id"], "3003")
