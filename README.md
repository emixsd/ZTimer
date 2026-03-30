# Zendesk Timer — 3 Observações Internas Automáticas

Quando um ticket entra em **"Em Organização"** (Aberto), o serviço agenda **3 alertas**:

| Timer | Mensagem |
|-------|----------|
| ⏱️ 10 min | Verifique se o prestador confirmou o recebimento. Caso contrário, ligue. |
| ⚠️ 30 min | Próximos de ultrapassar o SLA! Cobre o prestador ou acione o próximo da IT! |
| 🚨 55 min | ATENÇÃO! 60 MINUTOS PRÓXIMOS! Envie dados ou adéque expectativa! |

Se o ticket sair de "Em Organização" antes do tempo, **todos os timers são cancelados**.

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
