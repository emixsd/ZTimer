import unittest
from datetime import datetime, timedelta, timezone

from metrics import compute_pending_response_times


def audit(at, *events):
    return {
        "created_at": at.isoformat().replace("+00:00", "Z"),
        "events": list(events),
    }


class RequesterResponseMetricsTest(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def test_counts_pending_interval_while_target_tag_is_active(self):
        audits = [
            audit(
                self.start,
                {"field_name": "tags", "value": ["aguard_retorno_cliente"]},
                {"field_name": "status", "previous_value": "open", "value": "pending"},
            ),
            audit(
                self.start + timedelta(minutes=18),
                {
                    "field_name": "tags",
                    "previous_value": ["aguard_retorno_cliente"],
                    "value": [],
                },
                {"field_name": "status", "previous_value": "pending", "value": "open"},
            ),
        ]

        result = compute_pending_response_times(
            audits,
            123,
            pending_tags=["aguard_retorno_cliente"],
        )

        self.assertEqual(result.first_response_minutes, 18)
        self.assertEqual(result.total_response_minutes, 18)
        self.assertEqual(result.response_count, 1)
        self.assertIsNone(result.current_pending_at)

    def test_starts_when_tag_is_added_to_ticket_already_pending(self):
        audits = [
            audit(
                self.start,
                {"field_name": "status", "previous_value": "open", "value": "pending"},
            ),
            audit(
                self.start + timedelta(minutes=5),
                {"field_name": "tags", "value": ["aguard_retorno_cliente"]},
            ),
            audit(
                self.start + timedelta(minutes=15),
                {"field_name": "status", "previous_value": "pending", "value": "open"},
            ),
        ]

        result = compute_pending_response_times(
            audits,
            456,
            pending_tags=["aguard_retorno_cliente"],
        )

        self.assertEqual(result.first_response_minutes, 10)

    def test_exposes_the_active_pending_timer(self):
        now = self.start + timedelta(minutes=12)
        audits = [
            audit(
                self.start,
                {"field_name": "tags", "value": ["aguard_retorno_cliente"]},
                {"field_name": "status", "previous_value": "open", "value": "pending"},
            )
        ]

        result = compute_pending_response_times(
            audits,
            789,
            pending_tags=["aguard_retorno_cliente"],
            now=now,
        )

        self.assertEqual(result.current_pending_at, self.start)
        self.assertEqual(result.current_pending_elapsed_minutes, 12)
        self.assertIsNone(result.first_response_minutes)
        self.assertIsNone(result.total_response_minutes)


if __name__ == "__main__":
    unittest.main()
