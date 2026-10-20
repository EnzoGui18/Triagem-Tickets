# Triagem de Tickets

Fundação técnica do serviço de triagem inteligente de tickets.

## Requisitos

- Python 3.12+
- Docker e Docker Compose
- rede Docker externa `desafio-kit`

## Execução com Docker

```bash
docker network create desafio-kit
docker compose up --build
```

A API fica disponível em `http://localhost:8000`.

Endpoints disponíveis:

- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`
- `POST /tickets`
- `GET /tickets/{external_id}`
- `GET /tickets/{external_id}/timeline`
- `GET /tickets`

O modelo inicial contém tickets, versões do classificador, histórico de classificações,
jobs duráveis e tentativas de entrega de alertas. O schema é aplicado pelo serviço
`migrate` antes da API.

`POST /tickets` valida e persiste o ticket e um job durável de classificação na mesma
transação. A resposta é HTTP 202 e não aguarda classificação nem depende de Redis ou de
provedores externos.

O serviço `classification-worker` reserva jobs no PostgreSQL com `FOR UPDATE SKIP LOCKED`, chama
o `llm-sim` sob limite global do Redis, valida a saída e persiste o histórico de classificações.
Leases expirados voltam para retry, permitindo retomada após interrupção do worker.

Classificações urgentes criam, no mesmo commit, um registro inicial de entrega e um job durável.
O serviço separado `alert-worker` envia o alerta ao plantão e registra cada tentativa. Respostas
503, timeout e falhas de rede são reagendadas; 422 encerra o job como falha permanente. A entrega
possui semântica at-least-once, pois o plantão não oferece chave de idempotência para garantir
exactly-once.

Os componentes da integração com `llm-sim` ficam em `triagem.llm`: cliente HTTPX, prompt
`classifier-v1`, parser de resposta e schemas Pydantic. O worker compõe esses componentes por
meio do `LLMClassifier` e envia somente assunto e mensagem ao provedor.

Novas chamadas do cliente ao LLM passam por uma janela deslizante global e atômica no Redis,
configurada por `LLM_RATE_LIMIT_RPS`. O limite de requisições simultâneas é independente e
configurado por `LLM_MAX_CONCURRENCY`.

Falhas de disponibilidade do LLM alimentam um circuit breaker distribuído no Redis. Após o limiar
configurado, o circuito abre e os jobs permanecem reagendados no PostgreSQL sem novas chamadas ao
provedor. Depois do intervalo de recuperação, um único worker executa o probe em `half_open`; o
sucesso fecha o circuito e o processamento é retomado automaticamente.

Falhas transitórias são reagendadas no PostgreSQL com backoff exponencial e jitter. Respostas 429
respeitam `Retry-After`; falhas permanentes de autenticação e requisição inválida encerram o job
sem consumir todas as tentativas.

Cada classificação preserva a versão exata do classificador, os tokens de entrada e saída e os
custos calculados com `Decimal`. Reclassificações criam novas linhas e mantêm intacto o histórico
anterior.

`GET /tickets/{external_id}` retorna os dados do ticket, sua classificação mais recente e o job
ativo de classificação, quando ainda houver processamento pendente. `GET /tickets` aceita
`categoria`, `prioridade`, `inicio`, `fim`, `page` e `page_size`. O período filtra `received_at`,
com limites inclusivos e timestamps obrigatoriamente acompanhados de fuso horário. Filtros e
paginação são executados no PostgreSQL.

`GET /tickets/{external_id}/timeline` deriva uma linha do tempo cronológica do ticket, dos jobs,
das tentativas de classificação, das classificações e das entregas de alerta. O histórico inclui
retries e códigos de erro internos sem expor o conteúdo do ticket nem mensagens brutas dos
provedores.

Logs operacionais são emitidos em JSON e usam `X-Request-ID` como correlação quando o valor é
seguro; caso contrário, a API gera um UUID e o devolve no response header. Os logs incluem IDs
operacionais, mas não incluem Authorization, API keys, e-mail, mensagem do cliente, saída bruta ou
system prompt.

As métricas Prometheus da API estão em `/metrics`. Cada worker também expõe métricas na porta
configurada por `METRICS_PORT` (8000 por padrão) dentro da rede Docker. Como são processos
separados, um coletor deve fazer scrape de `api`, `classification-worker` e `alert-worker`.

`mensagem` aceita no máximo 12.000 caracteres. O conteúdo continua sendo tratado como dado não
confiável: HTML, JSON e instruções no texto são serializados apenas no campo `input` do LLM. O
prompt ajuda a orientar o modelo, mas não é fronteira de segurança; limites Pydantic, enums e o
parser permanecem responsáveis pela validação estrutural.

## Desenvolvimento local

```bash
python -m venv .venv
pip install -e ".[dev]"
alembic upgrade head
uvicorn triagem.main:app --reload
```

Validações:

```bash
pytest
ruff check .
mypy src tests
```

Os testes de persistência usam PostgreSQL real e podem ser executados no perfil de teste:

```bash
docker compose --profile test run --rm test
```
