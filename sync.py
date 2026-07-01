"""Orquestra metricas do Zendesk e timers de observacao interna.

Novo fluxo principal:
  - so considera tickets do formulario configurado (Seguro Viagem/N2);
  - calcula primeira resposta e tempo total do solicitante em pending;
  - grava log local exportavel para CSV/Excel;
  - enquanto o ticket segue em pending, adiciona observacoes internas por timer.
"""
import csv
import logging
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select

from config import Config
from metrics import (
    PendingIntervalResult,
    RequesterResponseResult,
    compute_first_pending_interval,
    compute_pending_response_times,
    current_pending_started_at,
    resolve_custom_status_ids,
)
from models import PendingTimeLog, RequesterResponseLog, SessionLocal, utcnow
from zendesk_client import ZendeskClient

logger = logging.getLogger(__name__)

EXPORT_FIELDNAMES = [
    "ticket_id",
    "email_solicitante",
    "pais",
    "primeira_resposta_minutos",
    "tempo_total_resposta_minutos",
    "primeira_saida_pending_minutos",
    "tempo_total_pending_minutos",
]


class MetricSyncer:
    def __init__(self, client: Optional[ZendeskClient] = None):
        self.client = client or ZendeskClient(
            Config.ZENDESK_SUBDOMAIN, Config.ZENDESK_EMAIL, Config.ZENDESK_API_TOKEN,
        )
        self._custom_ids: Optional[Dict[int, str]] = None
        self._country_options: Optional[Dict[str, str]] = None

    # -- resolucao do que conta como "Pendente" (legado) --------------- #
    def _measure_args(self) -> dict:
        if Config.MEASURE_MODE == "custom_status":
            if self._custom_ids is None:
                self._custom_ids = resolve_custom_status_ids(
                    self.client.get_custom_statuses(), Config.TARGET_STATUS_LABELS
                )
            target_id = next(iter(self._custom_ids), None)
            return {"field_name": "custom_status_id", "pending_value": str(target_id)}
        return {"field_name": "status", "pending_value": "pending"}

    # -- filtros e campos ---------------------------------------------- #
    def _ticket_form_id(self, ticket: dict) -> Optional[int]:
        value = ticket.get("ticket_form_id")
        return int(value) if value is not None else None

    def _is_target_form(self, ticket: dict) -> bool:
        return self._ticket_form_id(ticket) in set(Config.TARGET_TICKET_FORM_IDS)

    def _custom_field_value(self, ticket: dict, field_id: Optional[int]):
        if field_id is None:
            return None
        for field in ticket.get("custom_fields") or []:
            if int(field.get("id")) == field_id:
                return field.get("value")
        return None

    def _country_label(self, ticket: dict) -> Optional[str]:
        value = self._custom_field_value(ticket, Config.COUNTRY_CUSTOM_FIELD_ID)
        if value is None:
            return None
        if self._country_options is None:
            self._country_options = {}
            try:
                field = self.client.get_ticket_field(Config.COUNTRY_CUSTOM_FIELD_ID)
                for option in field.get("custom_field_options") or []:
                    option_value = str(option.get("value") or "")
                    label = option.get("name") or option.get("raw_name") or option_value
                    if option_value:
                        self._country_options[option_value] = label
            except Exception as exc:  # noqa: BLE001
                logger.warning("Nao foi possivel carregar opcoes do campo pais: %s", exc)
        return self._country_options.get(str(value), str(value))

    def _requester_email(self, ticket: dict) -> Optional[str]:
        requester_id = ticket.get("requester_id")
        if requester_id is None:
            return None
        try:
            user = self.client.get_user(int(requester_id))
            return user.get("email")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Nao foi possivel buscar solicitante %s: %s", requester_id, exc)
            return None

    # -- calculo legado ------------------------------------------------ #
    def compute_for_ticket(self, ticket: dict) -> PendingIntervalResult:
        audits = self.client.get_ticket_audits(ticket["id"])
        return compute_first_pending_interval(audits, ticket["id"], **self._measure_args())

    def _field_value(self, result: PendingIntervalResult) -> Optional[float]:
        if result.duration_minutes is None:
            return None
        if Config.FIELD_UNIT == "hours":
            return round(result.duration_minutes / 60, 2)
        return result.duration_minutes

    def write_back(self, result: PendingIntervalResult) -> bool:
        value = self._field_value(result)
        if value is None or Config.ZENDESK_CUSTOM_FIELD_ID is None:
            return False
        self.client.update_ticket_custom_field(
            result.ticket_id, Config.ZENDESK_CUSTOM_FIELD_ID, value
        )
        return True

    def upsert(self, ticket: dict, result: PendingIntervalResult, written: bool) -> PendingTimeLog:
        with SessionLocal() as session:
            row = session.get(PendingTimeLog, result.ticket_id) or PendingTimeLog(
                ticket_id=result.ticket_id
            )
            row.entered_pending = result.entered_pending
            row.entered_pending_at = result.entered_pending_at
            row.exited_pending_at = result.exited_pending_at
            row.exit_to_status = result.exit_to_status
            row.duration_minutes = result.duration_minutes
            row.still_pending = result.still_pending
            row.elapsed_minutes_so_far = result.elapsed_minutes_so_far
            row.written_to_zendesk = written
            row.ticket_status = ticket.get("status")
            row.subject = (ticket.get("subject") or "")[:500]
            row.computed_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    # -- resposta do solicitante -------------------------------------- #
    def upsert_response(
        self,
        ticket: dict,
        result: RequesterResponseResult,
        requester_email: Optional[str],
        country: Optional[str],
    ) -> RequesterResponseLog:
        with SessionLocal() as session:
            row = session.get(RequesterResponseLog, result.ticket_id) or RequesterResponseLog(
                ticket_id=result.ticket_id
            )
            row.requester_id = ticket.get("requester_id")
            row.requester_email = requester_email
            row.country = country
            row.first_response_minutes = result.first_response_minutes
            row.total_response_minutes = result.total_response_minutes
            row.response_count = result.response_count
            row.first_pending_at = result.first_pending_at
            row.first_opened_at = result.first_exited_at
            row.last_response_at = result.last_exited_at
            row.current_pending_at = result.current_pending_at
            row.current_pending_elapsed_minutes = result.current_pending_elapsed_minutes
            row.ticket_form_id = self._ticket_form_id(ticket)
            row.ticket_status = ticket.get("status")
            row.subject = (ticket.get("subject") or "")[:500]
            row.computed_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def update_timer_state(self, ticket_id: int, timer: dict) -> RequesterResponseLog:
        with SessionLocal() as session:
            row = session.get(RequesterResponseLog, ticket_id)
            if row is None:
                raise RuntimeError(f"Ticket {ticket_id} não foi salvo antes do timer")
            row.timer_alerts_sent = ",".join(
                str(value) for value in timer.get("alerts_sent", [])
            )
            row.timer_next_alert_minutes = timer.get("next_alert_minutes")
            row.timer_last_checked_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    # -- timer em pending --------------------------------------------- #
    def process_pending_timers(self, ticket: dict, audits: list[dict]) -> dict:
        ticket_id = int(ticket["id"])
        tags = set(ticket.get("tags") or [])
        alerts = sorted(Config.PENDING_TIMER_ALERTS, key=lambda item: item["minutes"])
        control_tags = {Config.PENDING_TIMER_ARMED_TAG}
        control_tags.update(str(alert["tag"]) for alert in alerts)

        if ticket.get("status") != "pending":
            tags_removed = sorted(tags & control_tags)
            if tags_removed:
                tags.difference_update(control_tags)
                self.client.update_ticket_tags(
                    ticket_id,
                    list(tags),
                    ticket.get("updated_at"),
                )
            return {
                "status": "disarmed" if tags_removed else "not_pending",
                "notes_sent": [],
                "tags_added": [],
                "tags_removed": tags_removed,
                "alerts_sent": [],
                "next_alert_minutes": None,
            }

        pending_since = current_pending_started_at(
            audits,
            current_status=ticket.get("status"),
            ticket_created_at=ticket.get("created_at"),
        )
        if pending_since is None:
            return {
                "status": "pending_since_unknown",
                "notes_sent": [],
                "tags_added": [],
                "tags_removed": [],
                "alerts_sent": [],
                "next_alert_minutes": alerts[0]["minutes"] if alerts else None,
            }

        elapsed = round((datetime.now(timezone.utc) - pending_since).total_seconds() / 60, 2)
        updated_stamp = ticket.get("updated_at")
        tags_added: list[str] = []
        notes_sent: list[int] = []

        if Config.PENDING_TIMER_ARMED_TAG not in tags:
            tags.add(Config.PENDING_TIMER_ARMED_TAG)
            tags_added.append(Config.PENDING_TIMER_ARMED_TAG)

        due_alerts = [
            alert
            for alert in alerts
            if elapsed >= int(alert["minutes"]) and str(alert["tag"]) not in tags
        ]
        if due_alerts:
            # Se o serviço ficou fora do ar, registra os marcos antigos como
            # superados e envia somente a mensagem mais urgente, evitando spam.
            for alert in due_alerts:
                tag = str(alert["tag"])
                tags.add(tag)
                tags_added.append(tag)
            alert = due_alerts[-1]
            updated = self.client.add_private_comment_with_tags(
                ticket_id,
                str(alert["message"]),
                list(tags),
                updated_stamp,
            )
            tags = set(updated.get("tags") or tags)
            notes_sent.append(int(alert["minutes"]))
        elif tags_added:
            updated = self.client.update_ticket_tags(ticket_id, list(tags), updated_stamp)
            tags = set(updated.get("tags") or tags)

        alerts_sent = [
            int(alert["minutes"])
            for alert in alerts
            if str(alert["tag"]) in tags
        ]
        next_alert = next(
            (
                int(alert["minutes"])
                for alert in alerts
                if str(alert["tag"]) not in tags
            ),
            None,
        )
        sla_state = "breached" if elapsed >= Config.PENDING_SLA_MINUTES else "on_track"
        if sla_state == "on_track" and elapsed >= max(Config.PENDING_SLA_MINUTES - 5, 0):
            sla_state = "at_risk"

        return {
            "status": "pending",
            "pending_since": pending_since.isoformat(),
            "elapsed_minutes": elapsed,
            "notes_sent": notes_sent,
            "tags_added": tags_added,
            "tags_removed": [],
            "alerts_sent": alerts_sent,
            "next_alert_minutes": next_alert,
            "sla_state": sla_state,
        }

    # -- entradas publicas -------------------------------------------- #
    def sync_ticket_id(self, ticket_id: int, export: bool = True) -> dict:
        ticket = self.client.get_ticket(ticket_id)
        return self.sync_ticket(ticket, export=export)

    def sync_ticket(self, ticket: dict, export: bool = True) -> dict:
        ticket_id = int(ticket["id"])
        if not self._is_target_form(ticket):
            return {
                "ticket_id": ticket_id,
                "status": "skipped",
                "reason": "wrong_ticket_form",
                "ticket_form_id": self._ticket_form_id(ticket),
                "expected_ticket_form_ids": Config.TARGET_TICKET_FORM_IDS,
            }

        audits = self.client.get_ticket_audits(ticket_id)
        response_result = compute_pending_response_times(
            audits,
            ticket_id,
            pending_tags=Config.RESPONSE_PENDING_TAGS,
        )
        requester_email = self._requester_email(ticket)
        country = self._country_label(ticket)
        row = self.upsert_response(ticket, response_result, requester_email, country)
        timer = self.process_pending_timers(ticket, audits)
        row = self.update_timer_state(ticket_id, timer)

        export_path = self.export_response_metrics() if export else None
        return {
            "ticket_id": ticket_id,
            "status": "processed",
            "response": row.to_dict(),
            "timer": timer,
            "export_path": export_path,
        }

    def sync_query(self, query: str) -> dict:
        tickets = self.client.search_tickets(query)
        processed = skipped = errors_n = notes_sent_n = 0
        errors = []
        results = []

        for t in tickets:
            if t.get("result_type") and t["result_type"] != "ticket":
                continue
            try:
                out = self.sync_ticket_id(int(t["id"]), export=False)
                results.append(out)
                if out.get("status") == "processed":
                    processed += 1
                    notes_sent_n += len(out.get("timer", {}).get("notes_sent", []))
                elif out.get("status") == "skipped":
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Falha no ticket %s", t.get("id"))
                errors.append({"ticket_id": t.get("id"), "error": str(exc)})
                errors_n += 1

        export_path = self.export_response_metrics()
        return {
            "query": query,
            "tickets_found": len(tickets),
            "processed": processed,
            "skipped": skipped,
            "timer_notes_sent": notes_sent_n,
            "errors_count": errors_n,
            "errors": errors[:20],
            "export_path": export_path,
            "results": results[:20],
        }

    # -- exportacao Excel/CSV ----------------------------------------- #
    def list_response_metrics(self, limit: int = 1000, offset: int = 0) -> list[dict]:
        stmt = (
            select(RequesterResponseLog)
            .order_by(RequesterResponseLog.computed_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with SessionLocal() as session:
            rows = session.execute(stmt).scalars().all()
            return [row.to_dict() for row in rows]

    def response_metrics_csv(self) -> str:
        return response_metrics_csv_from_db()

    def export_response_metrics(self) -> str:
        return export_response_metrics_file()


def response_metrics_csv_from_db() -> str:
    stmt = select(RequesterResponseLog).order_by(RequesterResponseLog.ticket_id.asc())
    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_export_dict())
    return output.getvalue()


def export_response_metrics_file() -> str:
    export_dir = Path(Config.EXPORT_DIR)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / Config.RESPONSE_EXPORT_FILENAME
    path.write_text("\ufeff" + response_metrics_csv_from_db(), encoding="utf-8")
    return str(path)
