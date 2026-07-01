"""Configuração via variáveis de ambiente."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: str = "0"):
    value = os.getenv(name, default).strip()
    parsed = int(value) if value else 0
    return parsed or None


def _int_list_env(name: str, default: str) -> list[int]:
    values = os.getenv(name, default)
    return [int(v.strip()) for v in values.split(",") if v.strip()]


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Zendesk ---
    ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "")
    ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL", "")
    ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN", "")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

    # ID do campo de ticket (tipo Decimal) onde gravamos a duração em Pendente.
    # Crie o campo no Zendesk (Admin > Campos de ticket > Decimal) e cole o ID aqui.
    ZENDESK_CUSTOM_FIELD_ID = _int_env("ZENDESK_CUSTOM_FIELD_ID")

    # Novo fluxo Seguro Viagem/N2.
    TARGET_TICKET_FORM_IDS = _int_list_env("TARGET_TICKET_FORM_IDS", "52281638323859")
    COUNTRY_CUSTOM_FIELD_ID = _int_env("COUNTRY_CUSTOM_FIELD_ID", "44008169716755")
    # Vazio (padrão) = conta todo o tempo em pending. Preencha com uma ou mais
    # tags (separadas por vírgula) para contar só quando alguma delas estiver ativa.
    RESPONSE_PENDING_TAGS = [
        s.strip()
        for s in os.getenv("RESPONSE_PENDING_TAGS", "").split(",")
        if s.strip()
    ]

    # Modo de medição:
    #   "base_status"   -> mede o status base 'pending' (Pendente padrão). [padrão]
    #   "custom_status" -> mede status custom específicos (usa TARGET_STATUS_LABELS).
    MEASURE_MODE = os.getenv("MEASURE_MODE", "base_status")

    # Usados apenas no modo custom_status.
    TARGET_STATUS_LABELS = [
        s.strip()
        for s in os.getenv("TARGET_STATUS_LABELS", "Pendente Prestador,Em Organização").split(",")
        if s.strip()
    ]

    # Unidade gravada no campo: "minutes" (padrão) ou "hours".
    FIELD_UNIT = os.getenv("FIELD_UNIT", "minutes")

    # Banco (log de auditoria local). SQLite por padrão.
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///metrics.db")

    # Busca padrão do Zendesk usada por POST /sync sem corpo.
    DEFAULT_SYNC_QUERY = os.getenv("DEFAULT_SYNC_QUERY", "type:ticket")

    # Varredura automática para os avisos de 10/30/55/60 min.
    PENDING_TIMER_LOOP_ENABLED = _bool_env("PENDING_TIMER_LOOP_ENABLED", "true")
    PENDING_TIMER_LOOP_INTERVAL_SECONDS = int(
        os.getenv("PENDING_TIMER_LOOP_INTERVAL_SECONDS", "300")
    )
    PENDING_TIMER_SYNC_QUERY = os.getenv(
        "PENDING_TIMER_SYNC_QUERY", "type:ticket status:pending"
    )
    PENDING_SLA_MINUTES = max(int(os.getenv("PENDING_SLA_MINUTES", "60")), 1)

    # Exportação consumida pelo Excel/Power Query.
    EXPORT_DIR = os.getenv("EXPORT_DIR", "exports")
    RESPONSE_EXPORT_FILENAME = os.getenv(
        "RESPONSE_EXPORT_FILENAME", "respostas_solicitantes.csv"
    )

    # Tags de controle do timer em Pendente.
    PENDING_TIMER_ARMED_TAG = os.getenv("PENDING_TIMER_ARMED_TAG", "tmr_pendente_armado")
    PENDING_TIMER_ALERTS = [
        {
            "minutes": 10,
            "tag": "nota_pendente_10m_ok",
            "level": "info",
            "message": (
                "⏱️ ZTimer · 10 min em Pendente\n"
                "Confirme se o prestador recebeu o acionamento. Sem confirmação? "
                "Faça contato por telefone e registre a tentativa."
            ),
        },
        {
            "minutes": 30,
            "tag": "nota_pendente_30m_ok",
            "level": "warning",
            "message": (
                "⚠️ ZTimer · 30 min em Pendente\n"
                "Metade do SLA consumida. Cobre o prestador agora; sem retorno, "
                "acione o próximo contato da IT."
            ),
        },
        {
            "minutes": 55,
            "tag": "nota_pendente_55m_ok",
            "level": "critical",
            "message": (
                "🚨 ZTimer · 55 min em Pendente\n"
                "Faltam 5 min para o SLA. Envie os dados disponíveis ou alinhe "
                "imediatamente a expectativa de prazo com o solicitante."
            ),
        },
        {
            "minutes": 60,
            "tag": "nota_pendente_60m_ok",
            "level": "breached",
            "message": (
                "🔴 ZTimer · SLA excedido (60 min)\n"
                "O ticket ultrapassou o SLA. Envie os dados imediatamente ou "
                "comunique o atraso e registre o próximo passo."
            ),
        },
    ]
