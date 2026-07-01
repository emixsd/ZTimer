import unittest
from datetime import datetime, timedelta, timezone

from config import Config
from sync import MetricSyncer


class FakeZendeskClient:
    def __init__(self):
        self.tag_updates = []
        self.comments = []

    def update_ticket_tags(self, ticket_id, tags, updated_stamp=None):
        self.tag_updates.append(
            {
                "ticket_id": ticket_id,
                "tags": set(tags),
                "updated_stamp": updated_stamp,
            }
        )
        return {"tags": tags, "updated_at": "2026-06-01T12:00:01Z"}

    def add_private_comment_with_tags(
        self,
        ticket_id,
        body,
        tags,
        updated_stamp=None,
    ):
        self.comments.append(
            {
                "ticket_id": ticket_id,
                "body": body,
                "tags": set(tags),
                "updated_stamp": updated_stamp,
            }
        )
        return {"tags": tags, "updated_at": "2026-06-01T12:00:01Z"}


def pending_audits(minutes_ago, reason_tag="organização_sla60m"):
    started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    events = [
        {
            "field_name": "status",
            "previous_value": "open",
            "value": "pending",
        }
    ]
    if reason_tag:
        events.insert(0, {"field_name": "tags", "value": [reason_tag]})
    return [
        {
            "created_at": started_at.isoformat().replace("+00:00", "Z"),
            "events": events,
        }
    ]


class PendingTimerTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeZendeskClient()
        self.syncer = MetricSyncer(client=self.client)

    def test_overdue_ticket_receives_only_the_most_urgent_notice(self):
        ticket = {
            "id": 101,
            "status": "pending",
            "tags": ["important"],
            "updated_at": "2026-06-01T12:00:00Z",
        }

        result = self.syncer.process_pending_timers(ticket, pending_audits(65))

        self.assertEqual(result["notes_sent"], [60])
        self.assertEqual(len(self.client.comments), 1)
        self.assertEqual(len(self.client.tag_updates), 0)
        expected_control_tags = {
            Config.PENDING_TIMER_ARMED_TAG,
            *(alert["tag"] for alert in Config.PENDING_TIMER_ALERTS),
        }
        self.assertTrue(expected_control_tags <= self.client.comments[0]["tags"])
        self.assertIn("SLA excedido", self.client.comments[0]["body"])

    def test_leaving_pending_removes_only_armed_tag(self):
        # As tags de aviso ficam: o relógio é acumulativo, então elas são o
        # registro do que já foi notificado e evitam nota repetida na volta.
        alert_tags = [alert["tag"] for alert in Config.PENDING_TIMER_ALERTS]
        ticket = {
            "id": 202,
            "status": "open",
            "tags": ["important", Config.PENDING_TIMER_ARMED_TAG, *alert_tags],
            "updated_at": "2026-06-01T12:00:00Z",
        }

        result = self.syncer.process_pending_timers(ticket, [])

        self.assertEqual(result["status"], "disarmed")
        self.assertEqual(result["alerts_sent"], [10, 30, 55, 60])
        self.assertEqual(len(self.client.tag_updates), 1)
        self.assertEqual(
            self.client.tag_updates[0]["tags"],
            {"important", *alert_tags},
        )
        self.assertEqual(result["tags_removed"], [Config.PENDING_TIMER_ARMED_TAG])

    def test_reentry_does_not_repeat_already_notified_milestones(self):
        # Saiu de pending com 35 min somados e voltou: as notas de 10 e 30 já
        # foram; nada repete e o próximo marco é o de 55.
        ticket = {
            "id": 505,
            "status": "pending",
            "tags": [
                "organização_sla60m",
                "nota_pendente_10m_ok",
                "nota_pendente_30m_ok",
            ],
            "updated_at": "2026-06-01T12:00:00Z",
        }

        result = self.syncer.process_pending_timers(ticket, pending_audits(35))

        self.assertEqual(result["notes_sent"], [])
        self.assertEqual(len(self.client.comments), 0)
        self.assertEqual(result["alerts_sent"], [10, 30])
        self.assertEqual(result["next_alert_minutes"], 55)
        self.assertEqual(result["tags_added"], [Config.PENDING_TIMER_ARMED_TAG])

    def test_first_scan_arms_timer_without_comment_before_ten_minutes(self):
        ticket = {
            "id": 303,
            "status": "pending",
            "tags": [],
            "updated_at": "2026-06-01T12:00:00Z",
        }

        result = self.syncer.process_pending_timers(ticket, pending_audits(2))

        self.assertEqual(result["notes_sent"], [])
        self.assertEqual(result["next_alert_minutes"], 10)
        self.assertEqual(len(self.client.comments), 0)
        self.assertEqual(len(self.client.tag_updates), 1)
        self.assertIn(
            Config.PENDING_TIMER_ARMED_TAG,
            self.client.tag_updates[0]["tags"],
        )

    def test_no_notice_when_type_is_not_sla60m(self):
        # 65 min em pending, mas com um tipo que não dispara avisos.
        ticket = {
            "id": 404,
            "status": "pending",
            "tags": ["organização_outrodia/hora"],
            "updated_at": "2026-06-01T12:00:00Z",
        }

        result = self.syncer.process_pending_timers(
            ticket, pending_audits(65, reason_tag="organização_outrodia/hora")
        )

        self.assertEqual(result["notes_sent"], [])
        self.assertEqual(len(self.client.comments), 0)


if __name__ == "__main__":
    unittest.main()
