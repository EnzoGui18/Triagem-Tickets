# Decisões

## Persistência e coordenação

- PostgreSQL permanece a fonte de verdade.
- Redis é uma dependência operacional, sem dados de domínio nesta fase.
- A fundação usa APIs assíncronas para PostgreSQL e Redis.

## Inicialização e saúde

- Migrações são executadas por um serviço de execução única antes da API no Compose.
- Falhas de PostgreSQL ou Redis não impedem o processo da API de iniciar; elas tornam `/health/ready` indisponível com HTTP 503.
- Provedores externos não fazem parte do readiness, permitindo que a ingestão futura continue independente deles.

## Escopo

A revisão inicial do Alembic é deliberadamente vazia: ela estabelece o baseline da infraestrutura sem antecipar tabelas de fases posteriores.

## Histórico e filas duráveis

- `classifications.ticket_id` não possui unicidade; toda reclassificação cria uma nova linha.
- A unicidade de `tickets.external_id` é garantida por constraint PostgreSQL.
- `processing_jobs` possui índice parcial para polling de jobs `pending` e `retry_scheduled` por disponibilidade e prioridade.
- Cada linha de `alert_deliveries` representa uma tentativa numerada. A constraint `(classification_id, attempt_number)` impede duplicação e preserva falhas anteriores para investigação.
- Categorias, prioridades, sentimentos e estados internos usam enums PostgreSQL para impedir valores fora do contrato.

## Idempotência da ingestão

- O hash é calculado a partir do modelo Pydantic validado, serializado com chaves ordenadas e timestamp normalizado para UTC.
- A API não faz `SELECT` preventivo para decidir a inserção. `INSERT ... ON CONFLICT DO NOTHING` usa a constraint `uq_tickets_external_id` como proteção contra concorrência.
- Ticket e job de classificação são confirmados na mesma transação; falha em qualquer inserção desfaz ambos.
- Um conflito idempotente retorna o mesmo ticket sem criar job adicional. Mesmo `external_id` com hash diferente retorna HTTP 409.
- A ingestão não depende de Redis e não executa chamadas externas.

## Processamento concorrente de jobs

- A reserva usa `SELECT ... FOR UPDATE SKIP LOCKED`, permitindo múltiplos workers sem que dois
  processos reservem simultaneamente a mesma linha.
- A reserva é confirmada antes da classificação. O worker não mantém uma transação aberta
  enquanto executa o classificador.
- `locked_at` determina a expiração do lease. `locked_by` e o `attempt_count` reservado formam o
  fencing token; a conclusão só é aceita se ambos ainda correspondem à reserva original.
- O estado `retry_scheduled` representa o estado RETRY do job engine e foi preservado para
  manter compatibilidade com o enum e o índice criados na Fase 2.
- Leases vencidos com tentativas restantes voltam para `retry_scheduled`; leases vencidos na
  última tentativa terminam em `failed`.
- O `FakeClassifier` possui uma identidade versionada persistida em `classifier_versions`. A
  criação dessa versão usa a constraint de identidade como proteção contra workers concorrentes.
- Resultado da classificação e conclusão do job pertencem à mesma transação, evitando resultado
  sem conclusão ou conclusão sem histórico.
