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
- Na Fase 5, o `FakeClassifier` possuía uma identidade versionada persistida em
  `classifier_versions`. O mesmo mecanismo e a mesma constraint de identidade são reutilizados
  pelo classificador real para proteger workers concorrentes.
- Resultado da classificação e conclusão do job pertencem à mesma transação, evitando resultado
  sem conclusão ou conclusão sem histórico.

## Fronteira com o provedor de IA

- A integração com `llm-sim` permanece separada em cliente, prompt, parser e schemas. O
  `LLMClassifier` compõe esses módulos e é a implementação usada pelo worker de produção.
- `classifier-v1` envia somente assunto e mensagem no campo `input`. Instruções ficam em
  `system`, que declara explicitamente que o ticket é dado não confiável.
- O parser só extrai um único objeto JSON completo. Texto externo é aceito apenas quando curto e
  sem estruturas JSON concorrentes; depois disso, Pydantic valida campos obrigatórios e enums sem
  normalização semântica.
- Erros de autenticação, requisição inválida, rate limit, falha transitória, timeout, transporte e
  resposta inválida possuem exceções de domínio distintas. A chave de API não integra mensagens
  de erro nem logs.

## Limitação global de chamadas ao LLM

- O limite temporal usa janela deslizante no Redis com script Lua atômico e chave compartilhada. O
  relógio vem de `Redis TIME`, evitando decisões divergentes entre processos.
- A janela admite no máximo `LLM_RATE_LIMIT_RPS` chamadas e inclui uma margem conservadora de 50 ms
  para absorver a diferença entre o instante da reserva e a chegada da requisição ao provedor.
- Quando a janela está cheia, o cliente aguarda o tempo calculado no Redis e tenta novamente. Falha
  de Redis interrompe a tentativa; não existe fallback sem rate limiting.
- `LLM_MAX_CONCURRENCY` limita chamadas simultâneas por instância do cliente e permanece separado
  de `LLM_RATE_LIMIT_RPS`. O semáforo não é usado como substituto do limite distribuído.
- A janela expira após ociosidade e contém somente estado operacional; PostgreSQL permanece a
  fonte de verdade para dados duráveis.

## Retry da classificação real

- Retries não mantêm o worker bloqueado: o job volta para `retry_scheduled` com novo
  `available_at`. `attempt_count` é incrementado na reserva e `max_attempts` limita toda execução.
- 429 respeita `Retry-After`. Erros 500/503, timeout, rede, rate limiter indisponível e saída
  inválida são transitórios. O atraso combina backoff exponencial, jitter de até 25% e teto
  configurado por `CLASSIFICATION_WORKER_MAX_RETRY_DELAY_SECONDS`.
- 401/403, requisições inválidas e outros erros HTTP permanentes falham imediatamente. Em
  particular, `context_length_exceeded` não repete o mesmo payload.
- Mensagens do provedor não são gravadas em `processing_jobs`; apenas códigos internos e mensagens
  operacionais genéricas são persistidos. Isso evita copiar conteúdo não confiável ou dados
  sensíveis para logs e campos de erro.
- A saída bruta é preservada somente quando foi extraída e validada com sucesso. Tentativas com
  saída inválida não criam linha parcial em `classifications`.
