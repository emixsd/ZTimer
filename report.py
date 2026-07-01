"""Relatório diário por e-mail (CSV anexo) + limpeza de retenção.

Fluxo: uma vez por dia, no horário configurado, envia o CSV das métricas
para os e-mails configurados e depois apaga os registros já resolvidos com
mais de RETENTION_HOURS. Tickets ainda em pending nunca são apagados (o
dashboard precisa deles). A limpeza só roda se o envio tiver sucesso, para
não perder dados que ainda não chegaram a ninguém.
"""
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import delete, func, or_, select

from config import Config
from models import PendingTimeLog, RequesterResponseLog, SessionLocal
from sync import response_metrics_csv_from_db

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(Config.SMTP_HOST and Config.REPORT_EMAIL_TO)


def report_timezone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(Config.REPORT_TIMEZONE)
    except Exception:  # noqa: BLE001 — sem tzdata, assume UTC-3 (Brasília)
        return timezone(timedelta(hours=-3))


def _marker_path() -> Path:
    return Path(Config.EXPORT_DIR) / ".last_report_sent"


def last_report_date() -> str:
    try:
        return _marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _mark_report_sent(date_str: str) -> None:
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(date_str, encoding="utf-8")


def report_due_now() -> bool:
    now_local = datetime.now(report_timezone())
    if now_local.hour != Config.REPORT_SEND_HOUR:
        return False
    return last_report_date() != now_local.date().isoformat()


def _row_counts() -> dict:
    with SessionLocal() as session:
        total = session.execute(
            select(func.count(RequesterResponseLog.ticket_id))
        ).scalar_one()
        pending = session.execute(
            select(func.count(RequesterResponseLog.ticket_id)).where(
                RequesterResponseLog.ticket_status == "pending"
            )
        ).scalar_one()
    return {"total": total or 0, "pending": pending or 0}


def send_report_email(csv_text: str, date_label: str, counts: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"ZTimer · Relatório diário {date_label}"
    msg["From"] = Config.SMTP_FROM or Config.SMTP_USER
    msg["To"] = ", ".join(Config.REPORT_EMAIL_TO)
    msg.set_content(
        f"Relatório diário do ZTimer ({date_label}).\n\n"
        f"Tickets no período: {counts['total']}\n"
        f"Ainda em pending: {counts['pending']}\n\n"
        f"O CSV completo segue em anexo. Registros resolvidos com mais de "
        f"{Config.RETENTION_HOURS}h são removidos da base após este envio."
    )
    msg.add_attachment(
        csv_text.encode("utf-8-sig"),
        maintype="text",
        subtype="csv",
        filename=f"ztimer_{date_label}_{Config.RESPONSE_EXPORT_FILENAME}",
    )

    if Config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, timeout=30) as smtp:
            if Config.SMTP_USER:
                smtp.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            if Config.SMTP_USER:
                smtp.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            smtp.send_message(msg)


def purge_old_rows(retention_hours: int) -> dict:
    """Apaga registros resolvidos mais antigos que a retenção.

    Nunca apaga linhas com ticket ainda em pending, para o dashboard e a
    varredura de timers não perderem o que está ativo.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    with SessionLocal() as session:
        responses_deleted = session.execute(
            delete(RequesterResponseLog).where(
                or_(
                    RequesterResponseLog.ticket_status.is_(None),
                    RequesterResponseLog.ticket_status != "pending",
                ),
                RequesterResponseLog.computed_at < cutoff,
            )
        ).rowcount
        pending_deleted = session.execute(
            delete(PendingTimeLog).where(
                or_(
                    PendingTimeLog.still_pending.is_(None),
                    PendingTimeLog.still_pending.is_not(True),
                ),
                PendingTimeLog.computed_at < cutoff,
            )
        ).rowcount
        session.commit()
    return {
        "responses_deleted": responses_deleted,
        "pending_logs_deleted": pending_deleted,
        "cutoff": cutoff.isoformat(),
    }


def run_daily_report() -> dict:
    """Envia o CSV por e-mail e, se o envio der certo, limpa os antigos."""
    if not smtp_configured():
        return {
            "status": "skipped",
            "reason": "SMTP_HOST/REPORT_EMAIL_TO não configurados",
        }

    date_label = datetime.now(report_timezone()).date().isoformat()
    counts = _row_counts()
    csv_text = response_metrics_csv_from_db()
    send_report_email(csv_text, date_label, counts)
    _mark_report_sent(date_label)
    purged = purge_old_rows(Config.RETENTION_HOURS)
    logger.info(
        "Relatório diário enviado (%s linhas) e retenção aplicada: %s",
        counts["total"],
        purged,
    )
    return {
        "status": "sent",
        "date": date_label,
        "recipients": Config.REPORT_EMAIL_TO,
        "rows": counts["total"],
        "still_pending": counts["pending"],
        "purged": purged,
    }
