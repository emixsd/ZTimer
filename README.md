# Zendesk Timer — 3 Observações Internas Automáticas

Quando um ticket entra em **"Em Organização"** (Aberto), o serviço agenda **3 alertas**:

| Timer | Mensagem |
|-------|----------|
| ⏱️ 10 min | Verifique se o prestador confirmou o recebimento. Caso contrário, ligue. |
| ⚠️ 30 min | Próximos de ultrapassar o SLA! Cobre o prestador ou acione o próximo da IT! |
| 🚨 55 min | ATENÇÃO! 60 MINUTOS PRÓXIMOS! Envie dados ou adéque expectativa! |

Se o ticket sair de "Em Organização" antes do tempo, **todos os timers são cancelados**.

---

## PASSO 1 — Gerar Token da API no Zendesk

1. Acesse **Admin Center** → Apps and integrations → Zendesk API
2. Ative **Token Access**
3. Clique **Add API token** → copie e guarde

---

## PASSO 2 — Subir no GitHub

1. Crie um repositório no GitHub (ex: `zendesk-timer`)
2. Faça upload dos 3 arquivos: `app.py`, `requirements.txt`, `render.yaml`

---

## PASSO 3 — Deploy no Render.com

1. Crie conta em [render.com](https://render.com) (grátis)
2. Clique **New** → **Web Service** → conecte o GitHub → selecione o repositório
3. Configure as **variáveis de ambiente** (Environment):

| Variável | Valor |
|----------|-------|
| `ZENDESK_SUBDOMAIN` | Seu subdomínio (ex: `minhaempresa`) |
| `ZENDESK_EMAIL` | Email admin (ex: `admin@empresa.com`) |
| `ZENDESK_API_TOKEN` | Token do Passo 1 |
| `CUSTOM_STATUS_ID` | `50108859882131` |
| `WEBHOOK_SECRET` | Invente uma senha (ex: `chave-segura-2024`) |

4. Clique **Deploy**
5. Anote a URL gerada (ex: `https://zendesk-timer-xxxx.onrender.com`)
6. Teste acessando `https://SUA-URL/health` — deve retornar `{"status": "ok"}`

---

## PASSO 4 — Configurar UptimeRobot (manter serviço acordado)

1. Crie conta em [uptimerobot.com](https://uptimerobot.com) (grátis)
2. Clique **Add New Monitor**
3. Configure:
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `Zendesk Timer`
   - URL: `https://SUA-URL-DO-RENDER/health`
   - Monitoring Interval: **5 minutes**
4. Clique **Create Monitor**

---

## PASSO 5 — Criar Webhooks no Zendesk

### Webhook 1: Armar timers
1. **Admin Center** → Apps and integrations → Webhooks → **Create webhook**
2. Name: `Timer Em Organização - Armar`
3. Request method: **POST**
4. Endpoint URL: `https://SUA-URL-DO-RENDER/zendesk/timer`
5. Request format: **JSON**
6. Authentication: **API key**
   - Header name: `X-Webhook-Secret`
   - Value: a mesma senha do `WEBHOOK_SECRET`

### Webhook 2: Cancelar timers
Repita com:
- Name: `Timer Em Organização - Cancelar`
- Endpoint URL: `https://SUA-URL-DO-RENDER/zendesk/cancelar`
- Mesma autenticação

---

## PASSO 6 — Criar Triggers no Zendesk

### Trigger 1: Armar ao entrar em "Em Organização"

**Admin Center** → Objects and rules → Triggers → **Add trigger**

**Nome:** `Timer — Armar ao entrar Em Organização`

**Conditions (Meet ALL):**
- Status category → Is → Open
- Custom ticket status → Changed to → Em Organização
- Tags → Contains none of → `tmr_em_org_armado`

**Actions:**
- Add tags → `tmr_em_org_armado`
- Notify active webhook → `Timer Em Organização - Armar`
- JSON body:
```json
{
  "ticket_id": "{{ticket.id}}",
  "event": "entered_em_organizacao"
}
```

### Trigger 2: Desarmar ao sair de "Em Organização"

**Nome:** `Timer — Desarmar ao sair de Em Organização`

**Conditions (Meet ALL):**
- Custom ticket status → Changed from → Em Organização
- Tags → Contains at least one of → `tmr_em_org_armado`

**Actions:**
- Remove tags → `tmr_em_org_armado` `nota_em_org_10m_ok` `nota_em_org_30m_ok` `nota_em_org_55m_ok`
- Notify active webhook → `Timer Em Organização - Cancelar`
- JSON body:
```json
{
  "ticket_id": "{{ticket.id}}",
  "event": "left_em_organizacao"
}
```

---

## PASSO 7 — Testar!

1. Pegue um ticket de teste
2. Mude o status para **"Em Organização"**
3. Espere 10 minutos → verifique se a primeira observação apareceu
4. Se quiser testar mais rápido: no Render, mude as variáveis de ambiente temporariamente alterando os segundos no `app.py` (ex: 60, 120, 180 segundos)

---

## Tags de controle (referência)

| Tag | Função |
|-----|--------|
| `tmr_em_org_armado` | Timers foram armados para este ticket |
| `nota_em_org_10m_ok` | Observação de 10 min já foi adicionada |
| `nota_em_org_30m_ok` | Observação de 30 min já foi adicionada |
| `nota_em_org_55m_ok` | Observação de 55 min já foi adicionada |

---

## Resumo da arquitetura

```
Ticket → "Em Organização"
    ↓
Trigger Zendesk (arma)
    ↓
Webhook POST → /zendesk/timer
    ↓
Serviço (Render) arma 3 timers: 10min, 30min, 55min
    ↓
Timer dispara → Revalida no Zendesk → Adiciona observação interna


Ticket sai de "Em Organização"
    ↓
Trigger Zendesk (desarma)
    ↓
Webhook POST → /zendesk/cancelar
    ↓
Serviço cancela todos os timers daquele ticket
```
