"""API Flask para sincronizar métricas Zendesk, timers e exportação CSV."""
import logging
import os

from flask import Flask, Response, jsonify, request
from sqlalchemy import func, select

from config import Config
from models import PendingTimeLog, RequesterResponseLog, SessionLocal, init_db
from sync import MetricSyncer, response_metrics_csv_from_db

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
init_db()
_syncer = None


def get_syncer() -> MetricSyncer:
    global _syncer
    if _syncer is None:
        _syncer = MetricSyncer()
    return _syncer


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        measure_mode=Config.MEASURE_MODE,
        field_id=Config.ZENDESK_CUSTOM_FIELD_ID,
        field_unit=Config.FIELD_UNIT,
        target_ticket_form_ids=Config.TARGET_TICKET_FORM_IDS,
        country_custom_field_id=Config.COUNTRY_CUSTOM_FIELD_ID,
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


@app.get("/requester-responses")
def list_requester_responses():
    """Métricas novas: primeira resposta e total pending -> open."""
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = int(request.args.get("offset", 0))

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
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = int(request.args.get("offset", 0))
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
