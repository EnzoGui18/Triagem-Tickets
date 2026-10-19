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
- `POST /tickets`

O modelo inicial contém tickets, versões do classificador, histórico de classificações,
jobs duráveis e tentativas de entrega de alertas. O schema é aplicado pelo serviço
`migrate` antes da API.

`POST /tickets` valida e persiste o ticket e um job durável de classificação na mesma
transação. A resposta é HTTP 202 e não aguarda classificação nem depende de Redis ou de
provedores externos.

O serviço `classification-worker` reserva jobs no PostgreSQL com `FOR UPDATE SKIP LOCKED`, chama
o `llm-sim` sob limite global do Redis, valida a saída e persiste o histórico de classificações.
Leases expirados voltam para retry, permitindo retomada após interrupção do worker.

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
