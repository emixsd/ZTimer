"""Cálculo do "cronômetro" de tempo em status Pendente.

Regra (definida com o time):
  - Inicia quando o ticket entra em Pendente pela 1ª vez (entered_at).
  - Para quando o ticket sai de Pendente (exited_at).
  - O valor é a duração corrida (24h/dia) entre os dois, em minutos.
  - Só o 1º intervalo conta; idas e voltas posteriores são ignoradas.

Fonte de verdade: a Ticket Audits API. Mudança de status aparece como
evento Change com field_name == "status" (valores base: new/open/pending/
hold/solved/closed). Opcionalmente dá pra medir por status custom usando
field_name == "custom_status_id".
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _parse_ts(value: str) -> datetime:
    """Converte timestamp ISO do Zendesk (…Z) em datetime aware (UTC)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sorted_audits(audits: List[dict]) -> List[dict]:
    return sorted(audits, key=lambda audit: _parse_ts(audit["created_at"]))


@dataclass
class PendingIntervalResult:
    ticket_id: int
    entered_pending: bool                  # chegou a entrar em Pendente?
    entered_pending_at: Optional[datetime]
    exited_pending_at: Optional[datetime]
    exit_to_status: Optional[str]          # para qual status saiu
    duration_minutes: Optional[float]      # MÉTRICA: minutos no 1º intervalo
    still_pending: bool                    # entrou mas ainda não saiu
    elapsed_minutes_so_far: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("entered_pending_at", "exited_pending_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class PendingStatusInterval:
    entered_pending_at: datetime
    exited_pending_at: datetime
    exit_to_status: Optional[str]
    duration_minutes: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entered_pending_at": self.entered_pending_at.isoformat(),
            "exited_pending_at": self.exited_pending_at.isoformat(),
            "exit_to_status": self.exit_to_status,
            "duration_minutes": self.duration_minutes,
        }


@dataclass
class RequesterResponseResult:
    ticket_id: int
    first_response_minutes: Optional[float]
    total_response_minutes: Optional[float]
    response_count: int
    intervals: List[PendingStatusInterval]
    first_pending_at: Optional[datetime] = None
    first_exited_at: Optional[datetime] = None
    last_exited_at: Optional[datetime] = None
    current_pending_at: Optional[datetime] = None
    current_pending_elapsed_minutes: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "first_response_minutes": self.first_response_minutes,
            "total_response_minutes": self.total_response_minutes,
            "response_count": self.response_count,
            "first_pending_at": self.first_pending_at.isoformat() if self.first_pending_at else None,
            "first_exited_at": self.first_exited_at.isoformat() if self.first_exited_at else None,
            "last_exited_at": self.last_exited_at.isoformat() if self.last_exited_at else None,
            "current_pending_at": self.current_pending_at.isoformat()
            if self.current_pending_at
            else None,
            "current_pending_elapsed_minutes": self.current_pending_elapsed_minutes,
            "intervals": [interval.to_dict() for interval in self.intervals],
        }


def _event_values(event: dict) -> set[str]:
    values = set()
    for key in ("value", "previous_value"):
        raw = event.get(key)
        if raw is None:
            continue
        if isinstance(raw, (list, tuple, set)):
            values.update(str(v) for v in raw)
        else:
            values.update(str(raw).replace(",", " ").split())
    return values


def compute_first_pending_interval(
    audits: List[dict],
    ticket_id: int,
    pending_value: str = "pending",
    field_name: str = "status",
    now: Optional[datetime] = None,
) -> PendingIntervalResult:
    """
    audits        : audits em ordem cronológica crescente.
    pending_value : valor que representa "Pendente" ('pending' para status base,
                    ou o id do status custom quando field_name='custom_status_id').
    field_name    : 'status' (padrão) ou 'custom_status_id'.
    """
    now = now or datetime.now(timezone.utc)
    pending_value = str(pending_value)

    entered_at: Optional[datetime] = None
    exited_at: Optional[datetime] = None
    exit_to: Optional[str] = None

    for audit in _sorted_audits(audits):
        audit_time = _parse_ts(audit["created_at"])
        for ev in audit.get("events", []):
            if ev.get("field_name") != field_name:
                continue
            value = str(ev.get("value")) if ev.get("value") is not None else None
            prev = str(ev.get("previous_value")) if ev.get("previous_value") is not None else None

            # Início do cronômetro: 1ª entrada em Pendente.
            if entered_at is None and value == pending_value:
                entered_at = audit_time
            # Parada do cronômetro: saiu de Pendente.
            elif entered_at is not None and exited_at is None and prev == pending_value and value != pending_value:
                exited_at = audit_time
                exit_to = value
        if entered_at is not None and exited_at is not None:
            break

    if entered_at is None:
        return PendingIntervalResult(
            ticket_id=ticket_id, entered_pending=False, entered_pending_at=None,
            exited_pending_at=None, exit_to_status=None, duration_minutes=None,
            still_pending=False, elapsed_minutes_so_far=None,
        )

    if exited_at is not None:
        minutes = round((exited_at - entered_at).total_seconds() / 60, 2)
        return PendingIntervalResult(
            ticket_id=ticket_id, entered_pending=True, entered_pending_at=entered_at,
            exited_pending_at=exited_at, exit_to_status=exit_to,
            duration_minutes=max(minutes, 0.0), still_pending=False,
            elapsed_minutes_so_far=None,
        )

    # Entrou mas ainda está em Pendente: cronômetro rodando.
    return PendingIntervalResult(
        ticket_id=ticket_id, entered_pending=True, entered_pending_at=entered_at,
        exited_pending_at=None, exit_to_status=None, duration_minutes=None,
        still_pending=True,
        elapsed_minutes_so_far=round((now - entered_at).total_seconds() / 60, 2),
    )


def compute_pending_response_times(
    audits: List[dict],
    ticket_id: int,
    pending_tags: Optional[List[str]] = None,
    now: Optional[datetime] = None,
) -> RequesterResponseResult:
    """Calcula tempo do solicitante: cada intervalo em pending ate a saida.

    - primeira resposta: primeiro intervalo pending -> qualquer status nao-pending
    - total: soma de todos os intervalos pending -> qualquer status nao-pending
    - se pending_tags for informado, so conta pendentes em que uma dessas tags
      esta ativa no audit trail
    """
    now = now or datetime.now(timezone.utc)
    wanted_tags = set(pending_tags or [])
    tag_active = not wanted_tags
    current_status: Optional[str] = None
    pending_started_at: Optional[datetime] = None
    intervals: List[PendingStatusInterval] = []

    for audit in _sorted_audits(audits):
        audit_time = _parse_ts(audit["created_at"])
        events = audit.get("events", [])
        next_tag_active = tag_active
        next_status = current_status

        if wanted_tags:
            for ev in events:
                if ev.get("field_name") != "tags":
                    continue
                values = _event_values(ev)
                if values & wanted_tags:
                    current = ev.get("value")
                    if isinstance(current, (list, tuple, set)):
                        next_tag_active = bool(set(str(v) for v in current) & wanted_tags)
                    else:
                        next_tag_active = bool(
                            set(str(current or "").replace(",", " ").split()) & wanted_tags
                        )

        for ev in events:
            if ev.get("field_name") != "status":
                continue
            value = str(ev.get("value")) if ev.get("value") is not None else None
            if value is not None:
                next_status = value

        was_counting = current_status == "pending" and tag_active
        should_count = next_status == "pending" and next_tag_active

        if not was_counting and should_count:
            pending_started_at = audit_time
        elif was_counting and not should_count and pending_started_at is not None:
            minutes = round((audit_time - pending_started_at).total_seconds() / 60, 2)
            intervals.append(
                PendingStatusInterval(
                    entered_pending_at=pending_started_at,
                    exited_pending_at=audit_time,
                    exit_to_status=next_status,
                    duration_minutes=max(minutes, 0.0),
                )
            )
            pending_started_at = None

        current_status = next_status
        tag_active = next_tag_active

    active_elapsed = None
    if pending_started_at is not None:
        active_elapsed = max(
            round((now - pending_started_at).total_seconds() / 60, 2),
            0.0,
        )

    if not intervals and pending_started_at is None:
        return RequesterResponseResult(
            ticket_id=ticket_id,
            first_response_minutes=None,
            total_response_minutes=None,
            response_count=0,
            intervals=[],
        )

    total = (
        round(sum(interval.duration_minutes for interval in intervals), 2)
        if intervals
        else None
    )
    return RequesterResponseResult(
        ticket_id=ticket_id,
        first_response_minutes=intervals[0].duration_minutes if intervals else None,
        total_response_minutes=total,
        response_count=len(intervals),
        intervals=intervals,
        first_pending_at=(
            intervals[0].entered_pending_at if intervals else pending_started_at
        ),
        first_exited_at=intervals[0].exited_pending_at if intervals else None,
        last_exited_at=intervals[-1].exited_pending_at if intervals else None,
        current_pending_at=pending_started_at,
        current_pending_elapsed_minutes=active_elapsed,
    )


compute_pending_to_open_response_times = compute_pending_response_times


def _tag_set(raw: Any) -> set:
    """Normaliza o valor de um evento de tags em um conjunto de strings."""
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(v) for v in raw}
    return set(str(raw).replace(",", " ").split())


def compute_pending_reason_breakdown(
    audits: List[dict],
    reason_tags: List[str],
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Soma o tempo em pending atribuido a cada tag de tipo de pendencia.

    Cada trecho em pending e creditado a tag de tipo que estava ativa naquele
    momento (lista suspensa = uma tag por vez). Trocar de tipo no meio do
    pending divide o tempo entre os baldes; trechos da mesma tag se somam,
    mesmo separados. Tempo em pending sem nenhuma dessas tags nao e creditado.
    """
    now = now or datetime.now(timezone.utc)
    reason_tags = list(reason_tags)
    totals: Dict[str, float] = {tag: 0.0 for tag in reason_tags}
    reason_lookup = set(reason_tags)

    current_status: Optional[str] = None
    current_reason: Optional[str] = None
    segment_start: Optional[datetime] = None

    def credit(end: datetime) -> None:
        if (
            segment_start is not None
            and current_status == "pending"
            and current_reason in totals
        ):
            minutes = (end - segment_start).total_seconds() / 60
            if minutes > 0:
                totals[current_reason] += minutes

    for audit in _sorted_audits(audits):
        audit_time = _parse_ts(audit["created_at"])
        next_status = current_status
        next_reason = current_reason

        for ev in audit.get("events", []):
            field_name = ev.get("field_name")
            if field_name == "status":
                value = ev.get("value")
                if value is not None:
                    next_status = str(value)
            elif field_name == "tags":
                # value traz o conjunto completo de tags apos a mudanca.
                present = _tag_set(ev.get("value")) & reason_lookup
                next_reason = next(iter(present)) if present else None

        if next_status != current_status or next_reason != current_reason:
            credit(audit_time)
            current_status = next_status
            current_reason = next_reason
            segment_start = audit_time

    credit(now)
    return {tag: round(minutes, 2) for tag, minutes in totals.items()}


def current_pending_started_at(
    audits: List[dict],
    current_status: Optional[str],
    ticket_created_at: Optional[str] = None,
) -> Optional[datetime]:
    """Retorna quando o intervalo pending atual comecou, se ainda estiver pending."""
    if current_status != "pending":
        return None

    pending_started_at: Optional[datetime] = None
    for audit in _sorted_audits(audits):
        audit_time = _parse_ts(audit["created_at"])
        for ev in audit.get("events", []):
            if ev.get("field_name") != "status":
                continue

            value = str(ev.get("value")) if ev.get("value") is not None else None
            prev = str(ev.get("previous_value")) if ev.get("previous_value") is not None else None

            if value == "pending" and prev != "pending":
                pending_started_at = audit_time
            elif prev == "pending" and value != "pending":
                pending_started_at = None

    if pending_started_at is None and ticket_created_at:
        return _parse_ts(ticket_created_at)
    return pending_started_at


def resolve_custom_status_ids(custom_statuses: List[dict], target_labels: List[str]) -> Dict[int, str]:
    """Mapeia rótulos -> {custom_status_id: rótulo} (modo custom_status, opcional).
    Compara agent_label e end_user_label (case-insensitive)."""
    wanted = {lbl.strip().lower(): lbl for lbl in target_labels}
    result: Dict[int, str] = {}
    for cs in custom_statuses:
        for lab in [(cs.get("agent_label") or "").strip().lower(),
                    (cs.get("end_user_label") or "").strip().lower()]:
            if lab in wanted:
                result[int(cs["id"])] = cs.get("agent_label") or wanted[lab]
                break
    return result
