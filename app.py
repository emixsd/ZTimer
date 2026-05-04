"""
Zendesk Timer Service v2 — Polling (Sem Timers em Memória)
==========================================================
Arquitetura robusta que sobrevive a restarts e deploys.

Como funciona:
1. Webhook do Zendesk avisa quando ticket entra em "Em Organização"
   → Serviço grava o horário de entrada em um dicionário
2. Background thread roda a cada 2 min verificando os tickets rastreados
   → Se passou 10/30/55 min, adiciona a observação interna
3. Se o serviço reiniciar (deploy), ao subir ele recupera os tickets
   perdidos consultando o Zendesk (tickets com tag tmr_em_org_armado)

Tags no ticket = "banco de dados". Nada se perde.
"""

import os
import threading
import time
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import requests as http_requests

# ============================================================
# CONFIGURAÇÃO
# ============================================================
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_EMAIL = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "minha-chave-secreta")

CUSTOM_STATUS_ID = os.environ.get("CUSTOM_STATUS_ID", "50108859882131")

# Intervalo do polling em segundos (2 minutos)
POLLING_INTERVALO = int(os.environ.get("POLLING_INTERVALO", "120"))

# ============================================================
# 4 ALERTAS AUTOMÁTICOS
# ============================================================
ALERTAS = [
    {
        "nome": "10min_prestador",
        "minutos": 10,
        "tag": "nota_em_org_10m_ok",
        "mensagem": (
            "⏱️ **10 MINUTOS em Organização**\n\n"
            "Verifique se o prestador confirmou o recebimento. "
            "Caso contrário, ligue para ele e confirme."
        ),
    },
    {
        "nome": "30min_sla",
        "minutos": 30,
        "tag": "nota_em_org_30m_ok",
        "mensagem": (
            "⚠️ **30 MINUTOS em Organização**\n\n"
            "Atenção! Estamos próximos de ultrapassar o SLA! "
            "Cobre o prestador ou acione o próximo da IT!"
        ),
    },
    {
        "nome": "55min_critico",
        "minutos": 55,
        "tag": "nota_em_org_55m_ok",
        "mensagem": (
            "🚨 **55 MINUTOS em Organização — ALERTA CRÍTICO**\n\n"
            "ATENÇÃO! 60 MINUTOS PRÓXIMOS!\n"
            "Envie os dados de atendimento para o cliente, se já houver. "
            "Se não tivermos dados, adéque expectativa!"
        ),
    },
    {
        "nome": "60min_sla_ultrapassado",
        "minutos": 60,
        "tag": "nota_em_org_60m_ok",
        "mensagem": (
            "🚨 **60 MINUTOS em Organização — SLA ULTRAPASSADO**\n\n"
            "ATENÇÃO! SLA EXCEDIDO!\n"
            "Envie os dados de atendimento para o cliente imediatamente. "
            "Se não houver dados, comunique o atraso e ajuste expectativas."
        ),
    },
]

TAG_ARMADO = "tmr_em_org_armado"
TODAS_TAGS = [TAG_ARMADO] + [a["tag"] for a in ALERTAS]

# ============================================================
# APP
# ============================================================
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("zendesk-timer")

ZENDESK_BASE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZENDESK_AUTH = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)

# ============================================================
# "BANCO DE DADOS" EM MEMÓRIA (com recuperação automática)
# { "ticket_id": datetime_entrada }
# ============================================================
tickets_rastreados = {}
lock = threading.Lock()


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
    """Adiciona observação interna + tag ao ticket."""
    # Busca tags atuais pra não sobrescrever
    ticket = buscar_ticket(ticket_id)
    if not ticket:
        return False

    tags_atuais = list(ticket.get("tags", []))
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
        logger.info(f"✅ [{tag}] Observação adicionada ao ticket #{ticket_id}")
        return True
    except http_requests.exceptions.RequestException as e:
        logger.error(f"❌ Erro ao atualizar ticket #{ticket_id}: {e}")
        return False


def buscar_tickets_armados():
    """Busca tickets com tag tmr_em_org_armado via Search API."""
    url = f"{ZENDESK_BASE_URL}/search.json"
    query = f"type:ticket tags:{TAG_ARMADO}"
    try:
        resp = http_requests.get(
            url, auth=ZENDESK_AUTH,
            params={"query": query},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Erro na busca de tickets armados: {e}")
        return []


def obter_hora_entrada_via_audit(ticket_id):
    """
    Consulta o audit trail do ticket pra descobrir quando a tag
    tmr_em_org_armado foi adicionada (= quando entrou em Em Organização).
    """
    url = f"{ZENDESK_BASE_URL}/tickets/{ticket_id}/audits.json"
    try:
        resp = http_requests.get(
            url, auth=ZENDESK_AUTH,
            params={"sort_order": "desc"},
            timeout=15,
        )
        resp.raise_for_status()
        audits = resp.json().get("audits", [])

        for audit in audits:
            for event in audit.get("events", []):
                # Procura o evento onde a tag tmr_em_org_armado foi adicionada
                if (event.get("type") == "Change"
                        and event.get("field_name") == "tags"
                        and TAG_ARMADO in str(event.get("value", ""))):
                    created = audit.get("created_at", "")
                    if created:
                        return datetime.fromisoformat(
                            created.replace("Z", "+00:00")
                        )
        # Fallback: usa updated_at do ticket
        ticket = buscar_ticket(ticket_id)
        if ticket:
            updated = ticket.get("updated_at", "")
            if updated:
                return datetime.fromisoformat(
                    updated.replace("Z", "+00:00")
                )
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Erro ao buscar audits do ticket #{ticket_id}: {e}")

    # Último fallback: agora (pior caso, timer começa do zero)
    return datetime.now(timezone.utc)


# ============================================================
# RECUPERAÇÃO PÓS-RESTART
# ============================================================

def recuperar_tickets():
    """
    Chamado na inicialização. Busca tickets que já tinham timer
    armado mas foram perdidos da memória por causa de um restart.
    """
    logger.info("🔄 Recuperando tickets após restart...")
    tickets = buscar_tickets_armados()

    recuperados = 0
    for ticket in tickets:
        ticket_id = str(ticket["id"])
        tags = ticket.get("tags", [])

        # Se já tem todas as 3 notas, não precisa rastrear
        todas_notas = all(a["tag"] in tags for a in ALERTAS)
        if todas_notas:
            continue

        with lock:
            if ticket_id not in tickets_rastreados:
                # Descobre quando entrou em "Em Organização"
                hora_entrada = obter_hora_entrada_via_audit(ticket["id"])
                tickets_rastreados[ticket_id] = hora_entrada
                recuperados += 1

                elapsed = (datetime.now(timezone.utc) - hora_entrada).total_seconds() / 60
                logger.info(
                    f"   🔁 Ticket #{ticket_id} recuperado — "
                    f"entrou há {elapsed:.0f} min"
                )

    logger.info(f"🔄 Recuperação concluída: {recuperados} ticket(s) recuperado(s)")


# ============================================================
# POLLING (roda a cada 2 min em background)
# ============================================================

def verificar_tickets():
    """Verifica todos os tickets rastreados e adiciona observações."""
    with lock:
        ticket_ids = list(tickets_rastreados.items())

    if not ticket_ids:
        return

    logger.info(f"🔍 Verificando {len(ticket_ids)} ticket(s) rastreado(s)...")
    agora = datetime.now(timezone.utc)

    for ticket_id_str, hora_entrada in ticket_ids:
        ticket_id = int(ticket_id_str)

        # Buscar ticket atual
        ticket = buscar_ticket(ticket_id)
        if not ticket:
            continue

        # Verificar se ainda está em "Em Organização" + Aberto
        custom_status = str(ticket.get("custom_status_id", ""))
        status = ticket.get("status", "")
        tags = ticket.get("tags", [])

        if custom_status != str(CUSTOM_STATUS_ID) or status != "open":
            # Saiu de "Em Organização" — parar de rastrear
            with lock:
                tickets_rastreados.pop(ticket_id_str, None)
            logger.info(f"⏭️ Ticket #{ticket_id} saiu de 'Em Organização'. Removido.")
            continue

        # Calcular tempo decorrido
        elapsed_min = (agora - hora_entrada).total_seconds() / 60

        # Verificar cada alerta
        for alerta in ALERTAS:
            if elapsed_min >= alerta["minutos"] and alerta["tag"] not in tags:
                logger.info(
                    f"⏰ Ticket #{ticket_id} — {elapsed_min:.0f} min — "
                    f"disparando '{alerta['nome']}'"
                )
                sucesso = adicionar_observacao(
                    ticket_id, alerta["mensagem"], alerta["tag"]
                )
                if sucesso:
                    # Atualiza tags locais pra não duplicar no mesmo ciclo
                    tags.append(alerta["tag"])

        # Se já tem todas as notas, parar de rastrear
        todas_notas = all(a["tag"] in tags for a in ALERTAS)
        if todas_notas:
            with lock:
                tickets_rastreados.pop(ticket_id_str, None)
            logger.info(f"✔️ Ticket #{ticket_id} — todas as observações enviadas. Removido.")


def polling_loop():
    """Loop de polling que roda em background a cada 2 minutos."""
    logger.info(f"🔁 Polling iniciado (intervalo: {POLLING_INTERVALO}s)")
    while True:
        try:
            verificar_tickets()
        except Exception as e:
            logger.error(f"❌ Erro no polling: {e}", exc_info=True)
        time.sleep(POLLING_INTERVALO)


# ============================================================
# ENDPOINTS
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


@app.route("/zendesk/timer", methods=["POST"])
def receber_webhook():
    """
    Webhook: ticket entrou em 'Em Organização'.
    Grava o horário de entrada na memória pra o polling verificar.
    """
    if not validar_webhook(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or not data.get("ticket_id"):
        return jsonify({"error": "payload inválido"}), 400

    ticket_id = str(data["ticket_id"])
    agora = datetime.now(timezone.utc)

    with lock:
        tickets_rastreados[ticket_id] = agora

    logger.info(f"📩 Ticket #{ticket_id} registrado — polling vai verificar")

    return jsonify({
        "status": "registrado",
        "ticket_id": ticket_id,
        "alertas": [
            {"nome": a["nome"], "minutos": a["minutos"]}
            for a in ALERTAS
        ],
    }), 200


@app.route("/zendesk/cancelar", methods=["POST"])
def cancelar():
    """Webhook: ticket saiu de 'Em Organização'."""
    if not validar_webhook(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or not data.get("ticket_id"):
        return jsonify({"error": "payload inválido"}), 400

    ticket_id = str(data["ticket_id"])

    with lock:
        removido = tickets_rastreados.pop(ticket_id, None)

    if removido:
        logger.info(f"🛑 Ticket #{ticket_id} removido do rastreamento")

    return jsonify({
        "status": "removido" if removido else "não_encontrado",
        "ticket_id": ticket_id,
    }), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    with lock:
        total = len(tickets_rastreados)
        detalhes = {}
        agora = datetime.now(timezone.utc)
        for tid, entrada in tickets_rastreados.items():
            elapsed = (agora - entrada).total_seconds() / 60
            detalhes[tid] = f"{elapsed:.0f} min"

    return jsonify({
        "status": "ok",
        "tickets_rastreados": total,
        "detalhes": detalhes,
        "polling_intervalo_seg": POLLING_INTERVALO,
        "timestamp": agora.isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "zendesk-timer-v2-polling",
        "status": "running",
        "alertas": [
            {"nome": a["nome"], "minutos": a["minutos"]}
            for a in ALERTAS
        ],
    }), 200


# ============================================================
# INICIALIZAÇÃO
# ============================================================

def inicializar():
    """Roda na primeira requisição: recupera tickets e inicia polling."""
    # Recuperar tickets perdidos
    if ZENDESK_SUBDOMAIN:
        recuperar_tickets()

    # Iniciar polling em background
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()


# Flag pra inicializar só uma vez
_inicializado = False
_init_lock = threading.Lock()


@app.before_request
def antes_de_cada_request():
    global _inicializado
    if not _inicializado:
        with _init_lock:
            if not _inicializado:
                inicializar()
                _inicializado = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Serviço v2 (polling) iniciando na porta {port}")
    for a in ALERTAS:
        logger.info(f"   📋 {a['nome']}: {a['minutos']} min")

    # Inicializar antes de rodar
    if ZENDESK_SUBDOMAIN:
        inicializar()
        _inicializado = True

    app.run(host="0.0.0.0", port=port)
