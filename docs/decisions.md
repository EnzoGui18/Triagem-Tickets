# Decisões

## Persistência e coordenação

- PostgreSQL permanece a fonte de verdade.
- Redis é uma dependência operacional, sem dados de domínio nesta fase.
- A fundação usa APIs assíncronas para PostgreSQL e Redis.

## Inicialização e saúde

- Migrações são executadas por um serviço de execução única antes da API no Compose.
- Liveness significa apenas que o processo da API está vivo.
- Readiness significa que a API pode ingerir e confirmar tickets com durabilidade. PostgreSQL
  indisponível retorna HTTP 503 e `status=error`.
- Redis indisponível permanece explícito em `checks.redis=error`, mas com PostgreSQL saudável o
  readiness retorna HTTP 200 e `status=degraded`; a API não é retirada da ingestão por uma
  dependência usada somente no processamento assíncrono.
- Classification workers continuam dependendo do Redis e falham fechado para não violar rate
  limit nem operar o circuit breaker sem coordenação distribuída.
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
- Uma reclassificação cria uma nova linha e um novo vínculo com `classifier_versions`; a linha
  anterior nunca é atualizada ou reutilizada como resultado do novo job.
- A API de reclassificação serializa solicitações concorrentes com lock na linha do ticket e retorna
  HTTP 409 quando já existe job de classificação ativo. O request apenas persiste o novo job; a
  identidade da versão é decidida pelo worker no processamento.
- O histórico de classificações é ordenado por `created_at` e `id`. Usage pode ser nulo para dados
  legados, e custos são serializados como strings com oito casas para não perder precisão.
- A combinação de provedor, modelo e versão identifica uma definição imutável. Reutilizar essa
  identidade com prompt ou schema diferente falha antes da chamada ao LLM, em vez de associar a
  nova classificação a metadados incorretos.

## Priorização da fila de classificação

- Uma função pura normaliza caixa e acentos de assunto e mensagem e retorna somente `0` ou `100`
  para `processing_jobs.queue_priority`.
- Sinais elevados cobrem fraude, invasão ou comprometimento de conta, cartão clonado, cobranças ou
  compras não reconhecidas/duplicadas e urgência explícita. Não existem IDs ou resultados do
  dataset na regra.
- O scheduler não produz campos de classificação nem alertas. Categoria, prioridade final,
  sentimento e efeitos urgentes continuam exclusivos do resultado validado do LLM.
- O worker preserva `queue_priority DESC` e usa disponibilidade, criação e UUID como desempates.
  Isso melhora o tempo até o LLM para possíveis urgências, mas aceita falsos positivos e possível
  aumento da espera dos jobs normais sob fluxo elevado sustentado.
- O log de ingestão registra apenas o valor controlado de `queue_priority`, nunca o conteúdo do
  ticket. A estrutura e os índices existentes já suportam os valores `0..100`; não há migration.

## Fronteira com o provedor de IA

- A integração com `llm-sim` permanece separada em cliente, prompt, parser e schemas. O
  `LLMClassifier` compõe esses módulos e é a implementação usada pelo worker de produção.
- `classifier-v1` envia somente assunto e mensagem no campo `input`. Instruções ficam em
  `system`, que declara explicitamente que o ticket é dado não confiável.
- O ticket é aceito e persistido integralmente independentemente do contexto do provedor. O JSON
  enviado ao classificador possui orçamento de 8.000 caracteres; entradas maiores preservam o
  começo e o final da mensagem, e `classifications.input_truncated` registra a redução.
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
- Novos jobs de classificação e alerta materializam, respectivamente,
  `CLASSIFICATION_MAX_ATTEMPTS` e `ALERT_MAX_ATTEMPTS`, ambos com padrão oito. Jobs existentes não
  são atualizados quando a configuração muda.
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

## Recuperação operacional de classificações falhas

- O comando `python -m triagem.tools.requeue_failed` aceita exatamente um `--external-id` ou
  `--all`; nenhuma API administrativa pública foi adicionada.
- O requeue cria um novo job em vez de zerar `attempt_count` no job falho. Isso preserva erros,
  leases finais e `classification_attempts` anteriores e evita reutilizar números protegidos por
  `(processing_job_id, attempt_number)`.
- O novo job começa em `pending`, sem lease ou erro, mantém a prioridade de fila anterior e recebe
  o budget atual de classificação. O ticket volta a `received` até a próxima reserva.
- Ticket e job mais recente são bloqueados e revalidados na transação. Se houver job posterior
  concluído ou processável, nenhum novo job é criado, tornando repetições do comando seguras.

## Circuit breaker do provedor de IA

- O estado do circuito é global entre workers e fica no Redis sob uma chave com TTL. Ele é apenas
  coordenação operacional; tickets e jobs continuam duráveis exclusivamente no PostgreSQL.
- Timeout, falha de rede e HTTP 5xx contam como indisponibilidade. 429, erros permanentes e saída
  inválida indicam que o provedor respondeu e, portanto, não abrem o circuito.
- As transições `closed` → `open` → `half_open` são atômicas. Um número de geração impede que uma
  resposta atrasada de uma chamada anterior sobrescreva um estado mais novo, e somente um worker
  recebe a permissão de probe em `half_open`.
- Job recusado em `open` não realizou chamada ao provedor e não consome `attempt_count`. Uma falha
  do probe coordenado também não esgota o job usado para testar a recuperação; ele permanece
  reagendado no PostgreSQL até a próxima janela. Chamadas normais em `closed` continuam limitadas
  por `max_attempts`.
- O mesmo tratamento sem consumo de tentativa é aplicado quando Redis impede rate limiting ou
  circuit breaking seguro, pois nenhuma chamada ao LLM foi iniciada.

## Custos de classificação

- Os preços são constantes explícitas em `Decimal`: US$ 0,40 por milhão de tokens de entrada e
  US$ 1,60 por milhão de tokens de saída. Nenhum cálculo financeiro utiliza `float`.
- `input_cost`, `output_cost` e `total_cost` usam `NUMERIC(20, 8)` no PostgreSQL. Oito casas
  decimais preservam exatamente o resultado das fórmulas atuais, cujos preços têm duas casas e
  cujo divisor é um milhão.
- Novas classificações sempre recebem os três custos. A migration recalcula registros históricos
  que já possuem ambos os contadores de tokens; registros legados sem usage permanecem com o trio
  nulo para não representar custo desconhecido como zero.
- Constraints exigem que os três custos estejam todos presentes ou todos ausentes, sejam não
  negativos e que `total_cost` seja exatamente a soma dos componentes.

## Leitura e filtragem de tickets

- A classificação atual é a última por `created_at`, com `id` como desempate determinístico. O
  histórico permanece preservado e nenhuma linha é marcada ou sobrescrita para representar esse
  conceito derivado.
- Filtros de categoria e prioridade se aplicam apenas à classificação atual. O período se aplica a
  `tickets.received_at`, usando limites inclusivos e timestamps com fuso horário.
- Filtragem, contagem e paginação permanecem no PostgreSQL. Os índices existentes de recebimento,
  categoria e prioridade atendem esta fase, portanto não foi criada uma migration adicional.

## Entrega de alertas urgentes

- Classificação urgente, tentativa inicial `pending` e job de alerta pertencem à mesma transação.
  Nenhum HTTP para o plantão ocorre antes desse commit.
- O resumo é uma normalização determinística de assunto e mensagem, truncada em 280 caracteres.
  Não há nova chamada ao LLM para gerar texto de alerta.
- O desenho de uma linha por tentativa em `alert_deliveries` foi preservado. `attempt_number` e
  `processing_jobs.attempt_count` registram a contagem; `attempted_at` em cada linha permite obter
  `first_attempt_at` e `last_attempt_at` por `MIN/MAX`; `provider_request_id` armazena o
  `provider_alert_id` quando o plantão o fornece.
- 202 conclui a entrega. 503, timeout, rede e demais 5xx são transitórios e usam reagendamento no
  PostgreSQL. 422 e demais 4xx são falhas permanentes. O worker não dorme durante o backoff.
- A entrega é **at-least-once**. Uma falha entre o aceite remoto e o commit local pode causar nova
  entrega após expiração do lease. Como o plantão não oferece chave de idempotência, exactly-once
  não é prometido.
- Falhas e retries de alerta operam apenas no job `alert_delivery`; nunca criam nova classificação
  nem retornam o job de classificação para processamento.
- `alert_request_duration_seconds` mede exclusivamente a latência HTTP do plantão.
  `urgent_alert_end_to_end_seconds` mede o requisito de negócio completo, de
  `tickets.received_at` a `alert_deliveries.delivered_at`, somente após a confirmação local da
  entrega. Durações estritamente maiores que a constante de 60 segundos incrementam
  `urgent_alert_sla_violations_total`.

## Auditoria operacional e timeline

- A timeline não usa uma tabela genérica `audit_events`: recebimento, fila, classificações e
  alertas já possuem fontes de verdade próprias e são projetados na leitura.
- `processing_jobs` sobrescreve o último erro quando uma classificação finalmente conclui. Como
  isso impedia reconstruir retries anteriores, foi criada a tabela específica
  `classification_attempts`, com uma linha numerada para cada reserva do job.
- A reserva do job e a criação da tentativa pertencem à mesma transação. Conclusão, falha,
  expiração de lease e próximo retry atualizam a tentativa sob o mesmo fencing usado pelo worker.
- Erros persistidos e expostos são códigos e mensagens internas sanitizadas; respostas brutas do
  provedor e PII do ticket não fazem parte da timeline.

## Observabilidade e segurança

- Logs usam JSON de uma linha com nome de evento e IDs de correlação. O middleware aceita somente
  `X-Request-ID` de até 128 caracteres no conjunto seguro; qualquer outro valor vira UUID.
- Campos sensíveis são excluídos por construção e por filtro defensivo no formatter. Não são
  registrados Authorization, API keys, e-mail, mensagem completa, saída bruta ou system prompt.
- Métricas usam Prometheus sem labels de alta cardinalidade. A API e cada worker possuem registry
  por processo e devem ser coletados separadamente; métricas não são mecanismo de durabilidade.
- A soma `llm_cost_usd_total` é somente telemetria em ponto flutuante exigida pelo formato
  Prometheus. Cálculo, persistência e relatórios financeiros usam exclusivamente `Decimal` e
  `NUMERIC(20, 8)` no PostgreSQL.
- A validação HTTP não reutiliza o limite de contexto do provedor: a mensagem não vazia é
  persistida integralmente. O sistema não tenta sanitizar ou interpretar HTML, JSON ou instruções
  no texto; ao montar o campo `input`, aplica orçamento conservador de 8.000 caracteres e preserva
  o começo e o final do conteúdo como dado não confiável.

## Performance e capacidade

- O Compose inicia três classification workers por padrão. O valor é configurável e foi escolhido
  após comparar uma, três e cinco réplicas: três reduziram a espera da fila sem adotar a maior
  contenção de ingestão observada com cinco no host de teste.
- O batch de reserva continua unitário. Como cada chamada passa pelo limite global de 5 req/s,
  pré-reservar jobs aumentaria exposição a lease sem aumentar a taxa permitida.
- O pool da API mantém capacidade total 15 (5 + 10 overflow). Um experimento com 25 conexões
  piorou os percentis e foi descartado. Workers usam pools menores porque processam um job por vez.
- O lease de classificação é 60 s e o polling é 0,5 s. O aumento do lease cobre a cauda medida e
  reduz recuperação prematura; fencing por `locked_by/attempt_count` permanece inalterado.
- Índices parciais acompanham polling e recuperação de lease. A classificação atual possui índice
  composto por ticket e ordem decrescente. Métricas de latência e pool são operacionais; tabelas
  PostgreSQL continuam sendo a fonte autoritativa.
