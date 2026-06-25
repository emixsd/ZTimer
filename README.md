# Ztimer 2.0

Serviço Flask para tickets Zendesk do formulário **Seguro Viagem/N2**.

Novo fluxo principal:

```text
ticket_form_id == 52281638323859
status monitorado == pending padrão do Zendesk
```

## O que ele faz

1. Calcula quanto tempo o solicitante demorou para responder:
   - primeira resposta: tempo em `pending` até a primeira saída de `pending`
   - total: soma de todos os períodos em `pending`

2. Exporta para CSV, pronto para Excel/Power Query:

```text
ticket_id
email_solicitante
pais
primeira_resposta_minutos
tempo_total_resposta_minutos
primeira_saida_pending_minutos
tempo_total_pending_minutos
```

3. Mostra um painel HTML para o time consultar:

```text
/
/dashboard
```

4. Enquanto o ticket do formulário alvo estiver em `pending`, envia observações
   internas para chamar atenção do agente:

| Timer | Tag | Mensagem |
|---|---|---|
| 10 min | `nota_pendente_10m_ok` | Verifique se o prestador confirmou o recebimento. Caso contrário, ligue. |
| 30 min | `nota_pendente_30m_ok` | Próximos de ultrapassar o SLA! Cobre o prestador ou acione o próximo da IT! |
| 55 min | `nota_pendente_55m_ok` | ATENÇÃO! 60 MINUTOS PRÓXIMOS! Envie dados ou adéque expectativa! |
| 60 min | `nota_pendente_60m_ok` | ATENÇÃO! SLA EXCEDIDO! Envie dados imediatamente ou comunique o atraso. |

Tag de controle de armado:

```text
tmr_pendente_armado
```

Se o ticket sair de `pending`, os timers restantes deixam de ser enviados.

## Configuração

```bash
cp env.example .env
pip install -r requirements.txt
python app.py
```

Principais variáveis:

```env
ZENDESK_SUBDOMAIN=suaempresa
ZENDESK_EMAIL=voce@suaempresa.com
ZENDESK_API_TOKEN=seu_token_de_api
WEBHOOK_SECRET=

TARGET_TICKET_FORM_IDS=52281638323859
COUNTRY_CUSTOM_FIELD_ID=44008169716755
RESPONSE_PENDING_TAGS=aguard_retorno_cliente

DATABASE_URL=sqlite:///metrics.db
DEFAULT_SYNC_QUERY=type:ticket

EXPORT_DIR=exports
RESPONSE_EXPORT_FILENAME=respostas_solicitantes.csv
```

O campo `COUNTRY_CUSTOM_FIELD_ID` é uma lista suspensa do Zendesk; o CSV exporta
o nome da opção quando a API retorna a lista de opções do campo. As métricas
consideram apenas períodos em `pending` em que a tag `aguard_retorno_cliente`
estava ativa.

## Uso

Processar um ticket:

```bash
curl -X POST localhost:5000/tickets/12345/sync
```

Webhook do Zendesk ao entrar em Pendente:

```text
POST /zendesk/timer
{"ticket_id": 12345}
```

Webhook ao sair de Pendente:

```text
POST /zendesk/cancelar
{"ticket_id": 12345}
```

O `/zendesk/timer` processa a entrada em `pending`. O `/zendesk/cancelar`
processa a saída de `pending`, fecha o intervalo em `pending` e alimenta o
dashboard. Quando o ticket não está mais em `pending`, os próximos avisos deixam
de ser enviados automaticamente.

Processar uma lista:

```bash
curl -X POST localhost:5000/sync -H "Content-Type: application/json" \
  -d "{\"ticket_ids\":[123,456]}"
```

Processar por busca Zendesk:

```bash
curl -X POST localhost:5000/sync -H "Content-Type: application/json" \
  -d "{\"query\":\"type:ticket updated>2026-06-01\"}"
```

Ver métricas novas:

```bash
curl localhost:5000/requester-responses
```

Painel HTML:

```text
http://localhost:5000/dashboard
```

Filtro por dia:

```text
http://localhost:5000/dashboard?date=2026-06-22
```

Filtro por período:

```text
http://localhost:5000/dashboard?date_from=2026-06-01&date_to=2026-06-25
```

No dashboard, o botão `Excluir` remove o ticket apenas da base local do ZTimer e
do CSV exportado; ele não altera o ticket no Zendesk.

Exportar para Excel/Power Query:

```text
http://localhost:5000/export/respostas.csv
```

O serviço também grava uma cópia local em:

```text
exports/respostas_solicitantes.csv
```

## Arquivos

- `app.py` - rotas Flask
- `sync.py` - orquestra Zendesk, cálculo, timers e exportação
- `metrics.py` - cálculo dos períodos em `pending`
- `zendesk_client.py` - cliente da API Zendesk
- `models.py` - logs locais em SQLAlchemy
- `config.py` - configuração por env
