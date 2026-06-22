"""Log de auditoria local (SQLAlchemy 2.x).

Uma linha por ticket, registrando o que foi calculado e se o valor foi
escrito de volta no campo do Zendesk. O dashboard "oficial" é o Explore
(lendo o campo do ticket); esta tabela serve para auditoria/idempotência.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, create_engine
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

    ticket_form_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ticket_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> Dict[str, Any]:
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
            "ticket_form_id": self.ticket_form_id,
            "ticket_status": self.ticket_status,
            "subject": self.subject,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }

    def to_export_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "email_solicitante": self.requester_email,
            "pais": self.country,
            "primeira_resposta_minutos": self.first_response_minutes,
            "tempo_total_resposta_minutos": self.total_response_minutes,
        }


engine = create_engine(Config.DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
