import unittest
from datetime import datetime
from types import SimpleNamespace

from edge_service_zkteco.event_parser import event_to_dict, parse_event_candidate


def _attendance(user_id="1001", timestamp=None, status=0, punch=1, uid=7):
    return SimpleNamespace(
        user_id=user_id,
        timestamp=timestamp or datetime(2026, 7, 2, 10, 0, 0),
        status=status,
        punch=punch,
        uid=uid,
    )


class ParseEventCandidateTests(unittest.TestCase):
    def test_parses_attendance_like_object(self):
        candidate = parse_event_candidate(_attendance())

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.identifier, "1001")
        self.assertEqual(candidate.happened_at, "2026-07-02T10:00:00")
        self.assertEqual(candidate.status, 0)
        self.assertEqual(candidate.punch, 1)
        self.assertEqual(candidate.event_key, "1001|2026-07-02T10:00:00|0|1|7")

    def test_parses_dict_event(self):
        candidate = parse_event_candidate(
            {
                "user_id": "2002",
                "timestamp": "2026-07-02T11:30:00",
                "status": 4,
                "punch": 0,
                "uid": 9,
            }
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.identifier, "2002")
        self.assertEqual(candidate.event_key, "2002|2026-07-02T11:30:00|4|0|9")

    def test_event_key_is_stable_for_same_event(self):
        first = parse_event_candidate(_attendance())
        second = parse_event_candidate(_attendance())

        self.assertEqual(first.event_key, second.event_key)

    def test_returns_none_without_identifier(self):
        self.assertIsNone(parse_event_candidate(_attendance(user_id="")))
        self.assertIsNone(parse_event_candidate(_attendance(user_id=None)))
        self.assertIsNone(parse_event_candidate({"timestamp": "2026-07-02T10:00:00"}))

    def test_strips_and_truncates_identifier(self):
        candidate = parse_event_candidate(_attendance(user_id="  " + "9" * 80 + "  "))

        self.assertEqual(candidate.identifier, "9" * 64)

    def test_filters_by_punch_codes(self):
        allowed = (1, 2)

        kept = parse_event_candidate(_attendance(punch=1), allowed_punch_codes=allowed)
        dropped = parse_event_candidate(_attendance(punch=5), allowed_punch_codes=allowed)
        missing = parse_event_candidate(
            {"user_id": "1001", "timestamp": "2026-07-02T10:00:00"},
            allowed_punch_codes=allowed,
        )

        self.assertIsNotNone(kept)
        self.assertIsNone(dropped)
        self.assertIsNone(missing)

    def test_filters_by_status_codes(self):
        allowed = (0,)

        kept = parse_event_candidate(_attendance(status=0), allowed_status_codes=allowed)
        dropped = parse_event_candidate(_attendance(status=3), allowed_status_codes=allowed)

        self.assertIsNotNone(kept)
        self.assertIsNone(dropped)

    def test_event_without_timestamp_still_parses(self):
        candidate = parse_event_candidate({"user_id": "1001"})

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.happened_at, "")


class EventToDictTests(unittest.TestCase):
    def test_serializes_attendance_like_object(self):
        data = event_to_dict(_attendance())

        self.assertEqual(
            data,
            {
                "uid": 7,
                "user_id": "1001",
                "timestamp": "2026-07-02T10:00:00",
                "status": 0,
                "punch": 1,
            },
        )
