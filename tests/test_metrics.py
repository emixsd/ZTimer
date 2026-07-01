import unittest
from datetime import datetime, timedelta, timezone

from metrics import compute_pending_reason_breakdown, compute_pending_response_times


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


class PendingReasonBreakdownTest(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.reason_tags = [
            "organização_sla60m",
            "organização_outrodia/hora",
            "documento-prestador",
        ]

    def test_splits_time_by_the_active_type_tag(self):
        # 2 min em sla60m -> troca pra outrodia por 4 min -> volta e fica 80 min.
        now = self.start + timedelta(minutes=86)
        audits = [
            audit(
                self.start,
                {"field_name": "tags", "value": ["organização_sla60m"]},
                {"field_name": "status", "previous_value": "open", "value": "pending"},
            ),
            audit(
                self.start + timedelta(minutes=2),
                {"field_name": "tags", "value": ["organização_outrodia/hora"]},
            ),
            audit(
                self.start + timedelta(minutes=6),
                {"field_name": "tags", "value": ["organização_sla60m"]},
            ),
        ]

        totals = compute_pending_reason_breakdown(audits, self.reason_tags, now=now)

        self.assertEqual(totals["organização_sla60m"], 82)
        self.assertEqual(totals["organização_outrodia/hora"], 4)
        self.assertEqual(totals["documento-prestador"], 0)

    def test_time_without_a_type_tag_is_not_credited(self):
        now = self.start + timedelta(minutes=10)
        audits = [
            audit(
                self.start,
                {"field_name": "status", "previous_value": "open", "value": "pending"},
            )
        ]

        totals = compute_pending_reason_breakdown(audits, self.reason_tags, now=now)

        self.assertEqual(sum(totals.values()), 0)


if __name__ == "__main__":
    unittest.main()
