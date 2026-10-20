# Fase 16 — performance e tuning

Data da execução: 2026-10-20 (America/Fortaleza/UTC).

## Método

Os testes usaram os simuladores e o dataset oficiais em
`kit-desafio-triagem/carga/tickets.jsonl`. O gerador oficial não registra duração por
request; por isso, `scripts/measure_ingestion.py` reproduziu sua cadência de 50 tickets/s,
timeout de 2 s e até três tentativas, acrescentando apenas cronômetros por tentativa HTTP.
Não houve alteração no dataset, no limite do LLM nem nas garantias transacionais.

Foram usados bancos PostgreSQL vazios e separados. Os percentis de fila e processamento
foram calculados no banco a partir de 116 jobs concluídos em cada configuração. Latência de
LLM usa `classifications.provider_latency_ms`; latência de alerta usa
`alert_deliveries.delivered_at - attempted_at`; tempo total usa
`processing_jobs.completed_at - tickets.received_at`.

Os simuladores possuem latência e falhas aleatórias. Portanto, diferenças pequenas nas
latências externas não são atribuídas ao código. Nenhum benchmark sintético foi extrapolado.

## Baseline

Configuração: uma API, um classification worker, um alert worker, pool SQLAlchemy default
de 5 conexões + 10 de overflow, polling de 1 s e lease de 30 s.

### Ingestão — 500 entradas

| Métrica | Resultado |
|---|---:|
| Aceitas (202) | 495 |
| Rejeitadas (422) | 5 |
| Falhas finais | 0 |
| Retries do cliente | 0 |
| Duração do lote | 10,681 s |
| p50 | 614,548 ms |
| p95 | 1.397,570 ms |
| p99 | 2.169,596 ms |
| Máximo | 3.691,823 ms |

Uma repetição idempotente do lote registrou pico de 16 sessões PostgreSQL, excluindo a
conexão do amostrador. Isso corresponde ao pool máximo de 15 da API mais atividade de outro
processo. O primeiro amostrador falhou e não foi usado.

### Pipeline — 116 classificações concluídas

| Métrica | p50 | p95 | p99 |
|---|---:|---:|---:|
| Espera inicial na fila | 119,702 s | 225,111 s | 230,204 s |
| LLM | 1.090,0 ms | 4.503,0 ms | 6.117,8 ms |
| Tentativa de classificação | 1,098 s | 14,319 s | 42,641 s |
| Alerta (10 entregas) | 0,723 s | 1,085 s | 1,107 s |
| Processamento total | 120,459 s | 225,108 s | 230,202 s |

O simulador reportou zero respostas 429. Esperas locais no rate limiter ainda não eram
instrumentadas. O plano de polling fazia `Seq Scan` em 484 jobs, seguido de sort; a
recuperação de lease também fazia `Seq Scan`.

## Experimentos e decisões

- Aumentar o pool da API para 20 + 5 overflow foi rejeitado. O pico chegou a 26 sessões e
  a ingestão piorou para p50 702 ms, p95 1.840 ms e p99 2.435 ms. O limite entregue permanece
  em 5 + 10, agora explícito e observável.
- Cinco classification workers reduziram fortemente a fila em uma amostra intermediária,
  mas elevaram a cauda da ingestão no host compartilhado. Três workers foram escolhidos como
  padrão; cinco continuam possíveis por configuração.
- Batch de reserva permanece 1. Reservar vários jobs antes do rate limiter aumentaria o tempo
  sob lease sem elevar o teto global de 5 req/s.
- O `AsyncClient` já era compartilhado durante toda a vida de cada worker e, portanto, já
  reutilizava conexões HTTPX. Nenhuma troca de cliente por request foi introduzida.
- O limite permaneceu em 5 novas chamadas/s. A amostra final teve 16 esperas locais no Redis
  entre três workers e zero 429 do provedor.

## Configuração entregue

- três réplicas de classification worker por padrão;
- polling dos workers reduzido de 1 s para 0,5 s;
- lease de classificação aumentado de 30 s para 60 s, acima do p99 observado de 42,641 s;
- pool da API explicitado em 5 + 10, com timeout de aquisição de 2 s; workers usam 2 + 0;
- cache process-local da identidade imutável de `classifier_versions` após a primeira resolução;
- índice parcial de polling alinhado a `job_type` e à ordenação da fila;
- índice parcial de recuperação por `job_type/locked_at`;
- índice para a última classificação por `ticket_id/created_at/id`;
- histogramas de ingestão, fila, LLM, classificação, alerta e tempo total, além de gauges do
  pool e contador de esperas locais do rate limiter.

O plano final usou `ix_processing_jobs_polling` e
`ix_processing_jobs_lease_recovery`. Com apenas 120 classifications, o planner preferiu
corretamente `Seq Scan` para a consulta da classificação atual, embora o índice composto já
esteja disponível para crescimento da tabela.

## Resultado final

### Ingestão comparável — 500 entradas, banco vazio, três workers

| Métrica | Baseline | Final |
|---|---:|---:|
| Aceitas (202) | 495 | 495 |
| Rejeitadas (422) | 5 | 5 |
| Falhas finais | 0 | 0 |
| Retries do cliente | 0 | 0 |
| Duração do lote | 10,681 s | 10,623 s |
| p50 | 614,548 ms | 691,899 ms |
| p95 | 1.397,570 ms | 1.808,851 ms |
| p99 | 2.169,596 ms | 2.942,838 ms |

A vazão total foi preservada, mas a cauda da ingestão piorou com processamento concorrente.
Não foi reduzido `synchronous_commit`, porque isso sacrificaria durabilidade. O pico final foi
19 sessões, incluindo três workers e o alert worker; a API expôs capacidade 15, 5 conexões
abertas em repouso e 0 checked out após a carga.

### Pipeline final — primeiros 116 jobs concluídos, três workers

| Métrica | Baseline p50/p95/p99 | Final p50/p95/p99 |
|---|---:|---:|
| Espera inicial na fila | 119,702 / 225,111 / 230,204 s | 96,812 / 136,936 / 159,268 s |
| LLM | 1.090,0 / 4.503,0 / 6.117,8 ms | 1.277,0 / 3.924,0 / 6.544,0 ms |
| Tentativa de classificação | 1,098 / 14,319 / 42,641 s | 1,294 / 15,410 / 39,450 s |
| Processamento total | 120,459 / 225,108 / 230,202 s | 117,652 / 154,257 / 157,074 s |

Na amostra final completa de 120 tickets, todos os 120 jobs concluíram e 11 alertas foram
entregues. O processamento total p95 caiu 31,5% e o p99 caiu 31,8%; p50 ficou praticamente
estável. A latência de alerta na amostra final de 11 entregas foi 0,212 / 1,106 / 1,158 s.

## Limitações

- O ambiente Docker Desktop apresentou pausas de dezenas de segundos. Uma delas expirou um
  lease de 60 s, mas o job foi recuperado e concluído; não houve perda nem duplicação de
  classification.
- Percentis do simulador variam entre execuções por desenho. Os resultados não constituem
  benchmark de capacidade de produção.
- A cauda de ingestão continua sensível à competição local com workers. Separar recursos da API
  e workers deve ser avaliado no ambiente de implantação antes de elevar réplicas.

## Validações executadas

- `docker compose config --quiet`: passou;
- `docker compose build`: passou;
- migration `20261020_0006` em banco vazio e no banco principal: passou;
- downgrade/upgrade de migrations pela suíte: passou;
- testes relacionados: 49 passaram;
- primeira suíte completa: 111 passaram e 1 falhou intermitentemente no cenário
  `500 → 500 → 200`, já registrado na Fase 15;
- repetição isolada desse cenário: 10 de 10 passaram;
- suíte completa final: 112 passaram, com um warning de depreciação Starlette/httpx;
- Ruff: passou;
- MyPy: passou em 50 arquivos;
- `/health/live` e `/health/ready`: HTTP 200 após restauração do banco principal.
