"""
Zendesk Timer Service — Observações Internas Automáticas
========================================================
Recebe webhook do Zendesk quando ticket entra em "Em Organização",
agenda 3 observações internas em 10, 30 e 55 minutos.

Deploy: Render.com (free tier) + UptimeRobot
"""

import os
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests as http_requests

# ============================================================
# CONFIGURAÇÃO (variáveis de ambiente no Render)
# ============================================================
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_EMAIL = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "minha-chave-secreta")

# ID do custom status "Em Organização"
CUSTOM_STATUS_ID = os.environ.get("CUSTOM_STATUS_ID", "50108859882131")

# ============================================================
# 3 MENSAGENS AUTOMÁTICAS
# ============================================================
TIMERS_CONFIG = [
    {
        "nome": "10min_prestador",
        "segundos": 600,        # 10 minutos
        "tag": "nota_em_org_10m_ok",
        "mensagem": (
            "⏱️ **10 MINUTOS em Organização**\n\n"
            "Verifique se o prestador confirmou o recebimento. "
            "Caso contrário, ligue para ele e confirme."
        ),
    },
    {
        "nome": "30min_sla",
        "segundos": 1800,       # 30 minutos
        "tag": "nota_em_org_30m_ok",
        "mensagem": (
            "⚠️ **30 MINUTOS em Organização**\n\n"
            "Atenção! Estamos próximos de ultrapassar o SLA! "
            "Cobre o prestador ou acione o próximo da IT!"
        ),
    },
    {
        "nome": "55min_critico",
        "segundos": 3300,       # 55 minutos
        "tag": "nota_em_org_55m_ok",
        "mensagem": (
            "🚨 **55 MINUTOS em Organização — ALERTA CRÍTICO**\n\n"
            "ATENÇÃO! 60 MINUTOS PRÓXIMOS!\n"
            "Envie os dados de atendimento para o cliente, se já houver. "
            "Se não tivermos dados, adéque expectativa!"
        ),
    },
]

TAG_ARMADO = "tmr_em_org_armado"

# ============================================================
# APP
# ============================================================
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("zendesk-timer")

# { "ticket_id": [timer1, timer2, timer3] }
timers_ativos = {}

ZENDESK_BASE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZENDESK_AUTH = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)


# ============================================================
# FUNÇÕES ZENDESK
# ============================================================

def buscar_ticket(ticket_id):
    """Busca dados atuais do ticket."""
    url = f"{ZENDESK_BASE_URL}/tickets/{ticket_id}.json"
    try:
        resp = http_requests.get(url, auth=ZENDESK_AUTH, timeout=15)
        resp.raise_for_status()
        return resp.json().get("ticket")
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Erro ao buscar ticket #{ticket_id}: {e}")
        return None


def adicionar_observacao(ticket_id, mensagem, tag):
    """Adiciona observação interna (nota privada) ao ticket."""
    # Primeiro busca as tags atuais do ticket
    ticket = buscar_ticket(ticket_id)
    tags_atuais = ticket.get("tags", []) if ticket else []

    # Adiciona a nova tag se ainda não existe
    if tag not in tags_atuais:
        tags_atuais.append(tag)

    url = f"{ZENDESK_BASE_URL}/tickets/{ticket_id}.json"
    payload = {
        "ticket": {
            "comment": {
                "body": mensagem,
                "public": False,
            },
            "tags": tags_atuais,
        }
    }
    try:
        resp = http_requests.put(url, auth=ZENDESK_AUTH, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info(f"✅ Observação + tag '{tag}' adicionadas ao ticket #{ticket_id}")
        return True
    except http_requests.exceptions.RequestException as e:
        logger.error(f"❌ Erro ao atualizar ticket #{ticket_id}: {e}")
        return False


# ============================================================
# LÓGICA DOS TIMERS
# ============================================================

def executar_timer(ticket_id, timer_config):
    """Executado quando um timer dispara. Revalida antes de agir."""
    nome = timer_config["nome"]
    tag = timer_config["tag"]
    mensagem = timer_config["mensagem"]
    minutos = timer_config["segundos"] // 60

    logger.info(f"⏰ Timer '{nome}' ({minutos}min) disparou para ticket #{ticket_id}")

    # Revalidar: ticket ainda em "Em Organização"?
    ticket = buscar_ticket(ticket_id)
    if not ticket:
        logger.info(f"⏭️ Ticket #{ticket_id} não encontrado. Ignorando.")
        return

    custom_status_atual = str(ticket.get("custom_status_id", ""))
    status_category = ticket.get("status", "")
    tags = ticket.get("tags", [])

    if custom_status_atual != str(CUSTOM_STATUS_ID):
        logger.info(f"⏭️ Ticket #{ticket_id} não está mais em 'Em Organização'. Ignorando.")
        return

    if status_category != "open":
        logger.info(f"⏭️ Ticket #{ticket_id} não está mais 'Aberto'. Ignorando.")
        return

    if tag in tags:
        logger.info(f"⏭️ Ticket #{ticket_id} já tem tag '{tag}'. Ignorando.")
        return

    # Tudo certo — adicionar observação
    adicionar_observacao(ticket_id, mensagem, tag)


def cancelar_timers_ticket(ticket_id):
    """Cancela todos os timers de um ticket."""
    ticket_id_str = str(ticket_id)
    if ticket_id_str in timers_ativos:
        for timer in timers_ativos[ticket_id_str]:
            timer.cancel()
        del timers_ativos[ticket_id_str]
        logger.info(f"🛑 Todos os timers cancelados para ticket #{ticket_id}")
        return True
    return False


def armar_timers_ticket(ticket_id):
    """Arma os 3 timers para um ticket."""
    ticket_id_str = str(ticket_id)

    # Cancelar anteriores se houver
    cancelar_timers_ticket(ticket_id)

    timers_lista = []

    for config in TIMERS_CONFIG:
        timer = threading.Timer(
            config["segundos"],
            executar_timer,
            args=[ticket_id, config],
        )
        timer.daemon = True
        timer.start()
        timers_lista.append(timer)

        minutos = config["segundos"] // 60
        logger.info(f"   ⏳ Timer '{config['nome']}' ({minutos}min) armado — ticket #{ticket_id}")

    timers_ativos[ticket_id_str] = timers_lista


# ============================================================
# VALIDAÇÃO
# ============================================================

def validar_webhook(req):
    """Valida o secret do webhook."""
    if not WEBHOOK_SECRET:
        return True
    secret = req.headers.get("X-Webhook-Secret", "")
    if secret == WEBHOOK_SECRET:
        return True
    auth = req.headers.get("Authorization", "").replace("Bearer ", "")
    if auth == WEBHOOK_SECRET:
        return True
    return False


# ============================================================
# ENDPOINTS
# ============================================================

@app.route("/zendesk/timer", methods=["POST"])
def receber_webhook():
    """Webhook: ticket entrou em 'Em Organização'."""
    if not validar_webhook(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or not data.get("ticket_id"):
        return jsonify({"error": "payload inválido"}), 400

    ticket_id = data["ticket_id"]
    logger.info(f"📩 Webhook: ticket #{ticket_id} → 'Em Organização'")

    armar_timers_ticket(ticket_id)

    return jsonify({
        "status": "timers_armados",
        "ticket_id": ticket_id,
        "timers": [
            {"nome": t["nome"], "minutos": t["segundos"] // 60}
            for t in TIMERS_CONFIG
        ],
    }), 200


@app.route("/zendesk/cancelar", methods=["POST"])
def cancelar_timer():
    """Webhook: ticket saiu de 'Em Organização'."""
    if not validar_webhook(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or not data.get("ticket_id"):
        return jsonify({"error": "payload inválido"}), 400

    ticket_id = data["ticket_id"]
    cancelado = cancelar_timers_ticket(ticket_id)

    return jsonify({
        "status": "timers_cancelados" if cancelado else "nenhum_timer_ativo",
        "ticket_id": ticket_id,
    }), 200


@app.route("/health", methods=["GET"])
def health_check():
    """UptimeRobot pinga aqui a cada 5 min pra manter o serviço acordado."""
    total_tickets = len(timers_ativos)
    total_timers = sum(len(t) for t in timers_ativos.values())
    return jsonify({
        "status": "ok",
        "tickets_monitorados": total_tickets,
        "timers_ativos": total_timers,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "zendesk-timer-em-organizacao",
        "status": "running",
        "timers": [
            {"nome": t["nome"], "minutos": t["segundos"] // 60}
            for t in TIMERS_CONFIG
        ],
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Serviço iniciando na porta {port}")
    for t in TIMERS_CONFIG:
        logger.info(f"   📋 {t['nome']}: {t['segundos'] // 60} min")
    app.run(host="0.0.0.0", port=port)
