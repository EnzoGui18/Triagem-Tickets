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

O serviço `classification-worker` reserva jobs no PostgreSQL com `FOR UPDATE SKIP LOCKED`,
executa o `FakeClassifier` determinístico e persiste o histórico de classificações. Leases
expirados voltam para retry, permitindo retomada após interrupção do worker. Nesta fase não
há chamada ao provedor de IA.

Os componentes isolados da integração com `llm-sim` ficam em `triagem.llm`: cliente HTTPX,
prompt `classifier-v1`, parser de resposta e schemas Pydantic. Eles ainda não estão conectados
ao `ClassificationWorker`; o processamento em execução continua usando somente o
`FakeClassifier`.

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
