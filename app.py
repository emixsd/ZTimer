"""API Flask para sincronizar métricas Zendesk, timers e exportação CSV."""
import hmac
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, select

from config import Config
from models import PendingTimeLog, RequesterResponseLog, SessionLocal, init_db
from report import (
    last_report_date,
    report_due_now,
    run_daily_report,
    smtp_configured,
)
from sync import MetricSyncer, export_response_metrics_file, response_metrics_csv_from_db

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
init_db()
if not Config.WEBHOOK_SECRET:
    logging.getLogger(__name__).warning(
        "WEBHOOK_SECRET não configurado: endpoints de sync/webhook aceitam "
        "requisições sem autenticação."
    )
_syncer = None
_timer_loop_started = False
_report_loop_started = False
_timer_loop_lock = threading.Lock()
_scan_run_lock = threading.Lock()
_scan_state_lock = threading.Lock()
_scan_state = {
    "running": False,
    "started_at": None,
    "completed_at": None,
    "processed": 0,
    "notes_sent": 0,
    "errors_count": 0,
    "last_error": None,
}


def get_syncer() -> MetricSyncer:
    global _syncer
    if _syncer is None:
        _syncer = MetricSyncer()
    return _syncer


def _zendesk_configured() -> bool:
    return bool(Config.ZENDESK_SUBDOMAIN and Config.ZENDESK_EMAIL and Config.ZENDESK_API_TOKEN)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_state_snapshot() -> dict:
    with _scan_state_lock:
        return dict(_scan_state)


def _run_pending_timer_scan(query: str = None) -> dict:
    if not _scan_run_lock.acquire(blocking=False):
        return {"status": "already_running", **_scan_state_snapshot()}

    with _scan_state_lock:
        _scan_state.update(
            running=True,
            started_at=_iso_now(),
            last_error=None,
        )
    try:
        result = get_syncer().sync_query(query or Config.PENDING_TIMER_SYNC_QUERY)
        with _scan_state_lock:
            _scan_state.update(
                running=False,
                completed_at=_iso_now(),
                processed=result.get("processed", 0),
                notes_sent=result.get("timer_notes_sent", 0),
                errors_count=result.get("errors_count", 0),
                last_error=(
                    result.get("errors", [{}])[0].get("error")
                    if result.get("errors")
                    else None
                ),
            )
        return result
    except Exception as exc:
        with _scan_state_lock:
            _scan_state.update(
                running=False,
                completed_at=_iso_now(),
                errors_count=1,
                last_error=str(exc),
            )
        raise
    finally:
        _scan_run_lock.release()


def _pending_timer_loop() -> None:
    logger = logging.getLogger(__name__)
    while True:
        try:
            result = _run_pending_timer_scan()
            logger.info(
                "Pending timer scan: processed=%s notes=%s errors=%s",
                result.get("processed"),
                result.get("timer_notes_sent"),
                result.get("errors_count"),
            )
        except Exception:
            logger.exception("Pending timer scan failed")
        time.sleep(max(Config.PENDING_TIMER_LOOP_INTERVAL_SECONDS, 60))


def start_pending_timer_loop() -> None:
    global _timer_loop_started
    if not Config.PENDING_TIMER_LOOP_ENABLED or not _zendesk_configured():
        return
    with _timer_loop_lock:
        if _timer_loop_started:
            return
        thread = threading.Thread(
            target=_pending_timer_loop,
            name="ztimer-pending-timer-loop",
            daemon=True,
        )
        thread.start()
        _timer_loop_started = True


def _daily_report_loop() -> None:
    logger = logging.getLogger(__name__)
    while True:
        try:
            if report_due_now():
                result = run_daily_report()
                logger.info("Relatório diário: %s", result)
        except Exception:
            logger.exception("Relatório diário falhou")
        time.sleep(60)


def start_daily_report_loop() -> None:
    global _report_loop_started
    if not Config.REPORT_EMAIL_ENABLED or not smtp_configured():
        if Config.REPORT_EMAIL_ENABLED:
            logging.getLogger(__name__).warning(
                "REPORT_EMAIL_ENABLED=true mas SMTP_HOST/REPORT_EMAIL_TO "
                "não configurados; relatório diário desativado."
            )
        return
    with _timer_loop_lock:
        if _report_loop_started:
            return
        thread = threading.Thread(
            target=_daily_report_loop,
            name="ztimer-daily-report-loop",
            daemon=True,
        )
        thread.start()
        _report_loop_started = True


start_pending_timer_loop()
start_daily_report_loop()


def _valid_webhook(req) -> bool:
    if not Config.WEBHOOK_SECRET:
        return True
    secret = req.headers.get("X-Webhook-Secret", "")
    if secret and hmac.compare_digest(secret, Config.WEBHOOK_SECRET):
        return True
    bearer = req.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(bearer) and hmac.compare_digest(bearer, Config.WEBHOOK_SECRET)


# Endpoints acessíveis sem o secret: páginas/ações do dashboard e leituras locais.
# Rotas novas ficam protegidas por padrão.
_OPEN_ENDPOINTS = {
    "static",
    "dashboard",
    "health",
    "export_requester_responses",
    "delete_requester_response",  # formulário do dashboard (sem header custom)
    "list_requester_responses",
    "list_metrics",
    "metrics_summary",
    "read_one",
}


@app.before_request
def _require_webhook_secret():
    if request.endpoint is None or request.endpoint in _OPEN_ENDPOINTS:
        return None
    if not _valid_webhook(request):
        return jsonify(error="unauthorized"), 401
    return None


def _payload_ticket_id(body: dict):
    for key in ("ticket_id", "id", "ticketId"):
        if body.get(key):
            return int(body[key])
    ticket = body.get("ticket")
    if isinstance(ticket, dict) and ticket.get("id"):
        return int(ticket["id"])
    return None


def _payload_event(body: dict) -> str:
    return str(body.get("event") or body.get("type") or "").lower()


def _event_is_pending_exit(event: str) -> bool:
    return any(token in event for token in ("left", "exit", "cancel", "sai", "open"))


def _result_ticket_status(result: dict):
    if result.get("status") != "processed":
        return None
    response = result.get("response") or {}
    return response.get("ticket_status")


def _sync_ticket_from_webhook(ticket_id: int, retry_exit: bool = False) -> dict:
    result = get_syncer().sync_ticket_id(ticket_id)
    if retry_exit and _result_ticket_status(result) == "pending":
        time.sleep(2)
        result = get_syncer().sync_ticket_id(ticket_id)
    return result


def _parse_date(value: str):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _live_pending_minutes(row: RequesterResponseLog):
    if row.ticket_status != "pending" or row.current_pending_at is None:
        return None
    return max(
        round(
            (datetime.now(timezone.utc) - _aware_utc(row.current_pending_at)).total_seconds()
            / 60,
            1,
        ),
        0.0,
    )


def _row_alert_elapsed(row: RequesterResponseLog) -> float:
    """Minutos acumulados na tag de alerta (sla60m), do último cálculo."""
    if not row.pending_reason_minutes:
        return 0.0
    try:
        data = json.loads(row.pending_reason_minutes)
    except (ValueError, TypeError):
        return 0.0
    return float(data.get(Config.PENDING_ALERT_REASON_TAG, 0.0) or 0.0)


def _live_alert_minutes(row: RequesterResponseLog):
    """Relógio do SLA ao vivo = tempo em sla60m (só corre quando é o tipo ativo)."""
    if row.ticket_status != "pending":
        return None
    elapsed = _row_alert_elapsed(row)
    if row.alert_clock_running and row.computed_at is not None:
        elapsed += max(
            (datetime.now(timezone.utc) - _aware_utc(row.computed_at)).total_seconds() / 60,
            0.0,
        )
    return round(elapsed, 1)


def _dashboard_row(row: RequesterResponseLog) -> dict:
    data = row.to_dict()
    elapsed = _live_alert_minutes(row)
    data["live_alert_minutes"] = elapsed
    data["live_pending_minutes"] = _live_pending_minutes(row)
    data["sla_percent"] = (
        min(round((elapsed / Config.PENDING_SLA_MINUTES) * 100), 100)
        if elapsed is not None and Config.PENDING_SLA_MINUTES > 0
        else 0
    )
    if elapsed is None:
        data["sla_state"] = "done"
        data["sla_label"] = "Fora de pending"
    elif elapsed >= Config.PENDING_SLA_MINUTES:
        data["sla_state"] = "breached"
        data["sla_label"] = "SLA excedido"
    elif elapsed >= max(Config.PENDING_SLA_MINUTES - 5, 0):
        data["sla_state"] = "at-risk"
        data["sla_label"] = "Atenção imediata"
    else:
        data["sla_state"] = "on-track"
        data["sla_label"] = "Dentro do SLA"
    return data


def _row_reference_date(row: RequesterResponseLog):
    dt = row.first_opened_at or row.current_pending_at or row.computed_at
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.date()


def _date_range_label(date_from, date_to) -> str:
    if date_from and date_to:
        return f"{date_from.isoformat()} a {date_to.isoformat()}"
    if date_from:
        return f"Desde {date_from.isoformat()}"
    if date_to:
        return f"Até {date_to.isoformat()}"
    return "Todos"


def _row_in_date_range(row: RequesterResponseLog, date_from, date_to) -> bool:
    row_date = _row_reference_date(row)
    if row_date is None:
        return False
    if date_from and row_date < date_from:
        return False
    if date_to and row_date > date_to:
        return False
    return True


def _dashboard_args_from_form(form) -> dict:
    args = {}
    for key in ("date_from", "date_to", "limit"):
        value = form.get(key)
        if value:
            args[key] = value
    return args


@app.get("/")
@app.get("/dashboard")
def dashboard():
    """Painel HTML de consulta para o time."""
    limit = _bounded_int(request.args.get("limit"), 100, 10, 500)
    legacy_date_raw = request.args.get("date", "")
    date_from_raw = request.args.get("date_from", "")
    date_to_raw = request.args.get("date_to", "")
    if legacy_date_raw and not date_from_raw and not date_to_raw:
        date_from_raw = legacy_date_raw
        date_to_raw = legacy_date_raw
    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
        date_from_raw, date_to_raw = date_to_raw, date_from_raw

    with SessionLocal() as session:
        m = RequesterResponseLog
        all_rows = session.execute(
            select(m).order_by(m.computed_at.desc())
        ).scalars().all()

    if date_from or date_to:
        filtered_rows = [
            row for row in all_rows
            if _row_in_date_range(row, date_from, date_to)
        ]
    else:
        filtered_rows = all_rows

    filtered_rows.sort(
        key=lambda row: (
            row.ticket_status == "pending",
            _live_alert_minutes(row) or -1,
            _aware_utc(row.computed_at).timestamp() if row.computed_at else 0,
        ),
        reverse=True,
    )
    rows = filtered_rows[:limit]
    total = len(filtered_rows)
    first_values = [
        row.first_response_minutes for row in filtered_rows
        if row.first_response_minutes is not None
    ]
    total_values = [
        row.total_response_minutes for row in filtered_rows
        if row.total_response_minutes is not None
    ]
    avg_first = sum(first_values) / len(first_values) if first_values else None
    avg_total = sum(total_values) / len(total_values) if total_values else None
    last_sync = max((row.computed_at for row in filtered_rows if row.computed_at), default=None)
    dashboard_rows = [_dashboard_row(row) for row in rows]
    active_elapsed = [
        value
        for value in (_live_alert_minutes(row) for row in filtered_rows)
        if value is not None
    ]
    active_count = len(active_elapsed)
    breached_count = sum(
        value >= Config.PENDING_SLA_MINUTES for value in active_elapsed
    )
    at_risk_count = sum(
        max(Config.PENDING_SLA_MINUTES - 5, 0)
        <= value
        < Config.PENDING_SLA_MINUTES
        for value in active_elapsed
    )
    completed_with_sla = [
        value for value in first_values if value is not None
    ]
    sla_compliance = (
        round(
            sum(value <= Config.PENDING_SLA_MINUTES for value in completed_with_sla)
            / len(completed_with_sla)
            * 100
        )
        if completed_with_sla
        else None
    )
    scan_state = _scan_state_snapshot()

    return render_template(
        "dashboard.html",
        rows=dashboard_rows,
        total=total or 0,
        active_count=active_count,
        at_risk_count=at_risk_count,
        breached_count=breached_count,
        sla_compliance=sla_compliance,
        sla_minutes=Config.PENDING_SLA_MINUTES,
        avg_first=round(avg_first, 1) if avg_first is not None else None,
        avg_total=round(avg_total, 1) if avg_total is not None else None,
        last_sync=last_sync.isoformat() if last_sync else None,
        export_url="/export/respostas.csv",
        target_forms=Config.TARGET_TICKET_FORM_IDS,
        country_field=Config.COUNTRY_CUSTOM_FIELD_ID,
        date_from=date_from.isoformat() if date_from else "",
        date_to=date_to.isoformat() if date_to else "",
        filter_label=_date_range_label(date_from, date_to),
        limit=limit,
        zendesk_base_url=(
            f"https://{Config.ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets"
            if Config.ZENDESK_SUBDOMAIN
            else None
        ),
        zendesk_configured=_zendesk_configured(),
        timer_loop_started=_timer_loop_started,
        timer_interval_seconds=Config.PENDING_TIMER_LOOP_INTERVAL_SECONDS,
        scan_state=scan_state,
        deleted=request.args.get("deleted"),
    )


@app.post("/requester-responses/<int:ticket_id>/delete")
def delete_requester_response(ticket_id: int):
    """Remove um ticket inválido da base local do dashboard/exportação."""
    with SessionLocal() as session:
        response_row = session.get(RequesterResponseLog, ticket_id)
        pending_row = session.get(PendingTimeLog, ticket_id)
        if response_row is not None:
            session.delete(response_row)
        if pending_row is not None:
            session.delete(pending_row)
        session.commit()
    export_response_metrics_file()
    args = _dashboard_args_from_form(request.form)
    args["deleted"] = ticket_id
    return redirect(url_for("dashboard", **args))


@app.post("/zendesk/timer")
def zendesk_timer_webhook():
    """Compatibilidade com o trigger antigo: processa o ticket e alimenta o painel."""
    body = request.get_json(silent=True) or {}
    ticket_id = _payload_ticket_id(body)
    if ticket_id is None:
        return jsonify(error="payload inválido: informe ticket_id"), 400

    event = _payload_event(body)
    result = _sync_ticket_from_webhook(
        ticket_id,
        retry_exit=_event_is_pending_exit(event),
    )
    return jsonify(status="processed", ticket_id=ticket_id, result=result)


@app.post("/zendesk/cancelar")
def zendesk_cancel_webhook():
    """Compatibilidade com o trigger antigo de desarme.

    Ao sair de pending, recalcula o ticket para fechar o intervalo de pending
    e alimentar o dashboard.
    """
    body = request.get_json(silent=True) or {}
    ticket_id = _payload_ticket_id(body)
    if ticket_id is None:
        return jsonify(error="payload inválido: informe ticket_id"), 400

    result = _sync_ticket_from_webhook(ticket_id, retry_exit=True)
    return jsonify(status="processed", ticket_id=ticket_id, result=result)


@app.get("/health")
def health():
    scan_state = _scan_state_snapshot()
    return jsonify(
        status="ok" if _zendesk_configured() else "degraded",
        zendesk_configured=_zendesk_configured(),
        measure_mode=Config.MEASURE_MODE,
        field_id=Config.ZENDESK_CUSTOM_FIELD_ID,
        field_unit=Config.FIELD_UNIT,
        target_ticket_form_ids=Config.TARGET_TICKET_FORM_IDS,
        country_custom_field_id=Config.COUNTRY_CUSTOM_FIELD_ID,
        response_pending_tags=Config.RESPONSE_PENDING_TAGS,
        pending_timer_loop_enabled=Config.PENDING_TIMER_LOOP_ENABLED,
        pending_timer_loop_started=_timer_loop_started,
        pending_timer_loop_interval_seconds=Config.PENDING_TIMER_LOOP_INTERVAL_SECONDS,
        pending_timer_sync_query=Config.PENDING_TIMER_SYNC_QUERY,
        pending_sla_minutes=Config.PENDING_SLA_MINUTES,
        timer_scan=scan_state,
        report_email_enabled=Config.REPORT_EMAIL_ENABLED,
        report_loop_started=_report_loop_started,
        report_send_hour=Config.REPORT_SEND_HOUR,
        report_timezone=Config.REPORT_TIMEZONE,
        retention_hours=Config.RETENTION_HOURS,
        last_report_date=last_report_date() or None,
    )


@app.get("/custom-statuses")
def custom_statuses():
    """Lista os status custom (útil só no modo custom_status)."""
    statuses = get_syncer().client.get_custom_statuses()
    return jsonify(statuses=[
        {"id": s["id"], "agent_label": s.get("agent_label"),
         "end_user_label": s.get("end_user_label"),
         "status_category": s.get("status_category")}
        for s in statuses
    ])


@app.post("/tickets/<int:ticket_id>/sync")
def sync_one(ticket_id: int):
    """Recalcula o tempo em Pendente de um ticket e grava no campo do ZD."""
    return jsonify(get_syncer().sync_ticket_id(ticket_id))


@app.get("/tickets/<int:ticket_id>")
def read_one(ticket_id: int):
    with SessionLocal() as session:
        row = session.get(PendingTimeLog, ticket_id)
        if row is None:
            return jsonify(error="não calculado ainda; chame POST .../sync"), 404
        return jsonify(row.to_dict())


@app.post("/sync")
def sync_batch():
    """Processa um lote.
      {"query": "type:ticket updated>2024-01-01"}  -> busca do Zendesk
      {"ticket_ids": [123, 456]}                    -> lista explícita
    Sem corpo, usa DEFAULT_SYNC_QUERY."""
    body = request.get_json(silent=True) or {}
    if body.get("ticket_ids"):
        out = [get_syncer().sync_ticket_id(int(i), export=False) for i in body["ticket_ids"]]
        export_path = get_syncer().export_response_metrics()
        return jsonify(processed=len(out), export_path=export_path, results=out)
    return jsonify(get_syncer().sync_query(body.get("query") or Config.DEFAULT_SYNC_QUERY))


@app.post("/timer/scan")
def timer_scan():
    """Força a varredura dos tickets em pending para enviar notas internas vencidas."""
    body = request.get_json(silent=True) or {}
    query = body.get("query") or Config.PENDING_TIMER_SYNC_QUERY
    return jsonify(_run_pending_timer_scan(query))


@app.post("/report/run")
def report_run():
    """Dispara manualmente o relatório por e-mail + limpeza de retenção."""
    return jsonify(run_daily_report())


@app.get("/requester-responses")
def list_requester_responses():
    """Métricas novas: primeira saída de pending e total em pending."""
    limit = _bounded_int(request.args.get("limit"), 100, 1, 1000)
    offset = _bounded_int(request.args.get("offset"), 0, 0, 1_000_000)

    stmt = (
        select(RequesterResponseLog)
        .order_by(RequesterResponseLog.computed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()
        return jsonify(count=len(rows), results=[r.to_dict() for r in rows])


@app.get("/export/respostas.csv")
def export_requester_responses():
    """CSV para Excel/Power Query."""
    csv_text = response_metrics_csv_from_db()
    filename = Config.RESPONSE_EXPORT_FILENAME
    return Response(
        "\ufeff" + csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/metrics")
def list_metrics():
    """Log local. Filtros: ?status=done|pending  ?limit=100  ?offset=0"""
    limit = _bounded_int(request.args.get("limit"), 100, 1, 1000)
    offset = _bounded_int(request.args.get("offset"), 0, 0, 1_000_000)
    status = request.args.get("status")

    stmt = select(PendingTimeLog).where(PendingTimeLog.entered_pending.is_(True))
    if status == "done":
        stmt = stmt.where(PendingTimeLog.duration_minutes.is_not(None))
    elif status == "pending":
        stmt = stmt.where(PendingTimeLog.still_pending.is_(True))
    stmt = stmt.order_by(PendingTimeLog.computed_at.desc()).limit(limit).offset(offset)

    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()
        return jsonify(count=len(rows), results=[r.to_dict() for r in rows])


@app.get("/metrics/summary")
def metrics_summary():
    """Agregados do log local (a média 'oficial' você tira no Explore)."""
    with SessionLocal() as session:
        m = PendingTimeLog
        count, avg_m, min_m, max_m = session.execute(
            select(func.count(m.ticket_id), func.avg(m.duration_minutes),
                   func.min(m.duration_minutes), func.max(m.duration_minutes))
            .where(m.duration_minutes.is_not(None))
        ).one()
        waiting = session.execute(
            select(func.count(m.ticket_id)).where(m.still_pending.is_(True))
        ).scalar_one()
        return jsonify(
            done_count=count or 0,
            still_pending_count=waiting or 0,
            avg_minutes=round(avg_m, 1) if avg_m is not None else None,
            avg_hours=round(avg_m / 60, 2) if avg_m is not None else None,
            min_minutes=min_m, max_minutes=max_m,
        )


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug)
