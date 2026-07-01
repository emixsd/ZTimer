"""Log de auditoria local (SQLAlchemy 2.x).

Uma linha por ticket, registrando o que foi calculado e se o valor foi
escrito de volta no campo do Zendesk. O dashboard "oficial" é o Explore
(lendo o campo do ticket); esta tabela serve para auditoria/idempotência.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from config import Config


class Base(DeclarativeBase):
    pass


class PendingTimeLog(Base):
    __tablename__ = "pending_time_log"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entered_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    entered_pending_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exited_pending_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_to_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    duration_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    still_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    elapsed_minutes_so_far: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    written_to_zendesk: Mapped[bool] = mapped_column(Boolean, default=False)
    ticket_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "entered_pending": self.entered_pending,
            "entered_pending_at": self.entered_pending_at.isoformat() if self.entered_pending_at else None,
            "exited_pending_at": self.exited_pending_at.isoformat() if self.exited_pending_at else None,
            "exit_to_status": self.exit_to_status,
            "duration_minutes": self.duration_minutes,
            "duration_hours": round(self.duration_minutes / 60, 2) if self.duration_minutes is not None else None,
            "still_pending": self.still_pending,
            "elapsed_minutes_so_far": self.elapsed_minutes_so_far,
            "written_to_zendesk": self.written_to_zendesk,
            "ticket_status": self.ticket_status,
            "subject": self.subject,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


class RequesterResponseLog(Base):
    __tablename__ = "requester_response_log"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    requester_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    requester_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_response_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_response_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    response_count: Mapped[int] = mapped_column(BigInteger, default=0)
    first_pending_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Nomes legados: hoje guardam a primeira/ultima saida de pending.
    first_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_pending_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_pending_elapsed_minutes: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    # JSON {tag_de_tipo: minutos} — tempo em pending por tipo de pendência.
    pending_reason_minutes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A tag de alerta (sla60m) é o tipo ativo agora? (relógio do aviso correndo)
    alert_clock_running: Mapped[bool] = mapped_column(Boolean, default=False)

    ticket_form_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ticket_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    timer_alerts_sent: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    timer_next_alert_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timer_last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> Dict[str, Any]:
        reason_minutes: Dict[str, Any] = {}
        if self.pending_reason_minutes:
            try:
                reason_minutes = json.loads(self.pending_reason_minutes)
            except (ValueError, TypeError):
                reason_minutes = {}
        reason_list = [
            {
                "tag": tag,
                "label": Config.PENDING_REASON_LABELS.get(tag, tag),
                "minutes": round(float(reason_minutes.get(tag, 0.0) or 0.0), 1),
            }
            for tag in Config.PENDING_REASON_TAGS
        ]
        alert_elapsed = float(
            reason_minutes.get(Config.PENDING_ALERT_REASON_TAG, 0.0) or 0.0
        )
        total_geral = round(
            (self.total_response_minutes or 0.0)
            + (self.current_pending_elapsed_minutes or 0.0),
            2,
        )
        return {
            "ticket_id": self.ticket_id,
            "requester_id": self.requester_id,
            "requester_email": self.requester_email,
            "country": self.country,
            "first_response_minutes": self.first_response_minutes,
            "first_response_hours": round(self.first_response_minutes / 60, 2)
            if self.first_response_minutes is not None
            else None,
            "total_response_minutes": self.total_response_minutes,
            "total_response_hours": round(self.total_response_minutes / 60, 2)
            if self.total_response_minutes is not None
            else None,
            "response_count": self.response_count,
            "first_pending_at": self.first_pending_at.isoformat() if self.first_pending_at else None,
            "first_exited_at": self.first_opened_at.isoformat() if self.first_opened_at else None,
            "last_exited_at": self.last_response_at.isoformat() if self.last_response_at else None,
            "first_opened_at": self.first_opened_at.isoformat() if self.first_opened_at else None,
            "last_response_at": self.last_response_at.isoformat() if self.last_response_at else None,
            "current_pending_at": self.current_pending_at.isoformat()
            if self.current_pending_at
            else None,
            "current_pending_elapsed_minutes": self.current_pending_elapsed_minutes,
            "ticket_form_id": self.ticket_form_id,
            "ticket_status": self.ticket_status,
            "subject": self.subject,
            "timer_alerts_sent": [
                int(value)
                for value in (self.timer_alerts_sent or "").split(",")
                if value
            ],
            "timer_next_alert_minutes": self.timer_next_alert_minutes,
            "timer_last_checked_at": self.timer_last_checked_at.isoformat()
            if self.timer_last_checked_at
            else None,
            "pending_reason_minutes": reason_list,
            "alert_elapsed_minutes": round(alert_elapsed, 2),
            "alert_clock_running": bool(self.alert_clock_running),
            "total_geral_minutes": total_geral,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }

    def to_export_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "email_solicitante": self.requester_email,
            "pais": self.country,
            "primeira_resposta_minutos": self.first_response_minutes,
            "tempo_total_resposta_minutos": self.total_response_minutes,
            "primeira_saida_pending_minutos": self.first_response_minutes,
            "tempo_total_pending_minutos": self.total_response_minutes,
        }


engine = create_engine(Config.DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    if "requester_response_log" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("requester_response_log")}
    needed = {
        "first_pending_at": "DateTime",
        "first_opened_at": "DateTime",
        "last_response_at": "DateTime",
        "current_pending_at": "DateTime",
        "current_pending_elapsed_minutes": "Float",
        "timer_alerts_sent": "String",
        "timer_next_alert_minutes": "Integer",
        "timer_last_checked_at": "DateTime",
        "pending_reason_minutes": "Text",
        "alert_clock_running": "Boolean",
    }
    missing = {name: kind for name, kind in needed.items() if name not in existing}
    if not missing:
        return

    type_map = {
        "sqlite": {
            "DateTime": "DATETIME",
            "Float": "FLOAT",
            "String": "VARCHAR(100)",
            "Integer": "INTEGER",
            "Text": "TEXT",
            "Boolean": "BOOLEAN",
        },
        "postgresql": {
            "DateTime": "TIMESTAMP WITH TIME ZONE",
            "Float": "DOUBLE PRECISION",
            "String": "VARCHAR(100)",
            "Integer": "INTEGER",
            "Text": "TEXT",
            "Boolean": "BOOLEAN",
        },
    }
    dialect_types = type_map.get(engine.dialect.name, type_map["sqlite"])
    with engine.begin() as conn:
        for name, kind in missing.items():
            conn.execute(
                text(
                    f"ALTER TABLE requester_response_log "
                    f"ADD COLUMN {name} {dialect_types[kind]}"
                )
            )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
