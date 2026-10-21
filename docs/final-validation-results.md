# Validação final e E2E

Data da execução: 2026-10-21

Commit validado: `fea3f898e33a4c88c8fd3c9967edd8aca5a679de`

Ambiente: Docker Desktop 27.5.1, Docker Compose 2.32.4 e containers Python 3.12.15.

## Escopo e isolamento

Esta fase somente validou o sistema. Nenhuma feature, migration, regra de negócio, teste, dataset
ou simulador foi alterado. O kit oficial foi executado a partir de
`kit-desafio-triagem/kit-desafio-triagem` com suas probabilidades e limites originais. Para não
escrever em `carga/out`, o `ENVIOS_LOG` do gerador foi direcionado para um volume Docker temporário.

A configuração efetiva da aplicação manteve três classification workers,
`LLM_RATE_LIMIT_RPS=5`, `LLM_MAX_CONCURRENCY=5`, `CLASSIFICATION_MAX_ATTEMPTS=8`,
`ALERT_MAX_ATTEMPTS=8`, lease de classificação de 60 segundos e polling de 0,5 segundo. O kit
manteve os padrões documentados: limite de 5 requisições por segundo, erro do LLM de 10%, output
malformado de 5%, latência habilitada e indisponibilidade do plantão de 10%.

## Regressão antes do E2E

| Validação | Resultado medido |
|---|---|
| CI da Fase 7 | 5/5 jobs verdes: Lint, Types, Tests, Migrations e Docker build |
| `ruff check . --no-cache` | PASS |
| `mypy src` | PASS, 36 arquivos sem erro |
| `pytest` local | 65 passed, 75 skipped, 195 warnings; infraestrutura ausente foi explicitamente pulada |
| `docker compose build` | PASS |
| Migrations em PostgreSQL vazio | 7 upgrades aplicados até `20261020_0007` |
| `alembic check` | PASS, nenhuma operação nova detectada |
| Suíte completa em Docker/Python 3.12 | 140 passed, 1 warning em 14,71 s |
| `GET /health/live` | HTTP 200, `status=ok` |
| `GET /health/ready` | HTTP 200, PostgreSQL `ok`, Redis `ok` |

O warning da suíte Docker é a depreciação Starlette/httpx já existente. A execução local usou
Python 3.14.3 e também emitiu warnings de APIs `asyncio` depreciadas; a validação representativa e
o CI usaram Python 3.12.

## Teste 1 — 20 tickets oficiais

O banco, os stats do LLM e o plantão estavam vazios antes da execução.

| Métrica | Resultado |
|---|---:|
| Linhas enviadas | 20 |
| Respostas 202 | 20 |
| Rejeições 4xx | 0 |
| Falhas finais/timeout | 0 |
| Reenvios do gerador | 0 |
| External IDs únicos | 20 |
| Tickets persistidos | 20 |
| Jobs de classificação | 20 completed |
| Classificações | 20 |
| Tentativas internas de classificação | 22 |
| Respostas LLM 5xx recuperadas | 2 |
| LLM 429 | 0 |
| Classificações urgentes/alertas entregues | 2/2 |

## Teste 2 — 500 tickets oficiais

O ambiente foi novamente zerado antes deste teste. O dataset possui 500 linhas, 485 external IDs
únicos e 15 linhas duplicadas.

| Métrica | Resultado |
|---|---:|
| Linhas do dataset | 500 |
| Respostas 2xx | 496, todas HTTP 202 |
| Respostas 4xx | 4, todas HTTP 422 |
| Respostas 5xx finais | 0 |
| Timeouts finais | 0 |
| Reenvios do gerador | 0 |
| External IDs únicos no dataset | 485 |
| External IDs únicos aceitos | 482 |
| Tickets persistidos | 482 |
| Jobs de classificação | 482 completed |
| Classificações | 482 |
| Jobs de classificação failed | 0 |
| Tentativas internas de classificação | 598 |
| Tentativas internas adicionais | 116 |
| Duplicações físicas no PostgreSQL | 0 |
| Tickets aceitos perdidos | 0 |
| Duração do gerador | 10,0 s |
| Latência média de ingestão | 163,912 ms |
| Ingestion p95 | maior que 250 ms e menor ou igual a 500 ms |
| Ingestion p99 | maior que 250 ms e menor ou igual a 500 ms |

Os percentis de ingestão são apresentados como intervalo porque o histogram Prometheus agrega as
500 observações em buckets: 384 ficaram em até 250 ms e todas ficaram em até 500 ms. O gerador
oficial não registra latência individual; não foi inventado um valor pontual dentro desse bucket.

Os quatro 422 correspondem a payloads realmente inválidos: `ATD-2026339` tem mensagem vazia e
aparece duas vezes; `ATD-2026340` tem assunto vazio e mensagem somente com espaços; e
`ATD-2026341` não possui assunto. Portanto, nenhum ticket válido foi rejeitado. Das 496 respostas
202, 14 são repetições idempotentes válidas; as 482 identidades aceitas correspondem exatamente
aos 482 tickets físicos.

### Ticket grande

`ATD-2026338` recebeu HTTP 202 na primeira tentativa. O PostgreSQL mediu 53.960 caracteres em
`tickets.message`, a classificação registrou `input_truncated=true`, o job terminou `completed` e
nenhum `context_length_exceeded` ocorreu. Foi o único input truncado entre as 482 classificações.

## Teste 3 — rate limit e instabilidade do LLM

Fotografia de `GET /admin/stats` após a drenagem do benchmark de 500:

| Métrica | Resultado |
|---|---:|
| Requisições | 542 |
| HTTP 200 | 487 |
| HTTP 429 | 0 |
| HTTP 5xx | 55 |
| Outputs malformados | 23 |
| Contexto excedido | 0 |
| Não autorizadas | 0 |
| Requests inválidos | 0 |
| Tokens de entrada | 92.174 |
| Tokens de saída | 25.236 |

`malformados` é um subconjunto das respostas 200 do simulador. Outputs recuperáveis foram aceitos
pelo parser; os demais foram repetidos. As 55 respostas 5xx também foram absorvidas pelos retries.

## Teste 4 — SLA urgente end-to-end

O cálculo abaixo usa exclusivamente `alert_deliveries.delivered_at - tickets.received_at`, depois
da drenagem completa. Não usa `attempted_at`.

| Métrica | Resultado |
|---|---:|
| Classificações urgentes | 49 |
| Alert jobs | 49 completed |
| Alertas entregues | 49 |
| Tentativas de alerta com falha transitória | 5 |
| p50 | 69,587 s |
| p95 | 229,539 s |
| p99 | 324,213 s |
| Máximo | 381,593 s |
| Entregues em até 60 s | 19 |
| Entregues acima de 60 s | 30 |
| Percentual dentro do SLA | 38,78% |

O SLA de 60 segundos **não foi cumprido** neste benchmark. Todos os 49 alertas foram entregues e
aparecem no plantão, mas somente 19 ficaram dentro do limite.

## Testes operacionais pós-benchmark

Estes cenários foram executados depois da fotografia anterior e não alteram seus números.

### Outage de 60 segundos

- o outage oficial foi configurado por `POST /admin/outage?segundos=60`;
- `PHASE8-OUTAGE-001` foi aceito com HTTP 202 durante o outage;
- o job recebeu duas respostas 503 e ficou `retry_scheduled`;
- sondagens do circuito falharam sem aumentar `attempt_count`;
- depois da recuperação, o job concluiu e criou uma classificação na terceira tentativa de
  provedor.

Uma primeira chamada manual deste cenário continha JSON inválido por quoting do PowerShell e
recebeu 422 corretamente. Ela ocorreu depois da fotografia do benchmark e foi repetida com JSON
válido; não foi contabilizada como falha do sistema.

### Redis indisponível

- com Redis parado, `/health/ready` retornou HTTP 200, `status=degraded`, PostgreSQL `ok` e Redis
  `error`;
- `PHASE8-REDIS-001` foi aceito com HTTP 202;
- enquanto o Redis esteve parado, o job permaneceu `retry_scheduled`, sem classificação e com
  `attempt_count=0`, erro `rate_limiter_unavailable`;
- depois de restaurar o Redis, readiness voltou a `ok` e o job concluiu.

### Reclassificação

- `ATD-2026001` possuía uma classificação;
- `POST /tickets/ATD-2026001/reclassify` retornou HTTP 202;
- após o worker processar o novo job, o histórico passou de uma para duas classificações com IDs
  distintos; ambas usam `classifier-v1`, pois não houve deploy de uma versão nova nesta validação.

### Requeue operacional

Para produzir uma falha permanente sem editar o banco, os workers normais foram parados e um
worker temporário isolado foi executado com credencial inválida. O job de
`PHASE8-REQUEUE-001` falhou na primeira tentativa com `llm_authentication_failed`.

`python -m triagem.tools.requeue_failed --external-id PHASE8-REQUEUE-001` criou um novo job com
budget 8. O job antigo e sua tentativa 401 permaneceram `failed` e consultáveis. Depois de
restaurar os workers normais, o novo job concluiu na primeira tentativa e o ticket ficou
`classified`.

## Invariantes

Na fotografia do benchmark limpo:

- 482 external IDs únicos aceitos = 482 tickets persistidos;
- todos os 482 tickets estavam `classified`;
- todos os 482 jobs de classificação estavam `completed`;
- não havia ticket sem job, classificação ou estado explicável;
- não havia duplicação física por `external_id`;
- todas as 49 classificações urgentes possuíam alert job;
- todos os alert jobs estavam `completed`; todas as linhas de entrega estavam em `delivered` ou
  `transient_failure` histórico.

Após os cenários operacionais, havia 485 tickets, todos `classified`, 486 jobs de classificação
`completed` e um job `failed` preservado propositalmente pelo teste de requeue. Havia 486
classificações, zero tickets sem job, zero urgentes sem alert job e zero estados sem explicação.

## Resumo executivo

| Métrica | Resultado |
|---|---:|
| Dataset | 500 linhas |
| Aceitos | 496 respostas; 482 IDs únicos |
| Tickets válidos rejeitados | 0 |
| Payloads inválidos rejeitados | 4 linhas / 3 IDs |
| Perdidos | 0 |
| Duplicações físicas | 0 |
| LLM 429 | 0 |
| Classificados | 482 |
| Jobs failed | 0 |
| Urgentes | 49 |
| Alertas entregues | 49 |
| Alertas em até 60 s | 19 |
| SLA urgente | 38,78% |
| Ingestion p95 | (250, 500] ms |
| Ingestion p99 | (250, 500] ms |

## Riscos e divergências ainda existentes

- O SLA urgente medido ficou abaixo da meta; a durabilidade e a entrega final passaram, mas o
  tempo end-to-end não passou.
- O histogram atual não permite recuperar p95/p99 exatos da ingestão, somente o bucket
  `(250, 500] ms` observado para ambos.
- As dependências não possuem lockfile, portanto builds futuros podem resolver versões
  transitivas diferentes.
- As inconsistências históricas encontradas no README sobre budget de tentativas e requeue foram
  corrigidas durante o code freeze, sem alteração do comportamento da aplicação.
- Métricas continuam process-local e alertas possuem semântica at-least-once.
