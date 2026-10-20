# Arquitetura

Nesta fase, a aplicação é uma API FastAPI assíncrona com PostgreSQL como banco de dados e Redis para coordenação operacional. SQLAlchemy usa o driver `asyncpg`, e o esquema do banco é versionado pelo Alembic.

O processo da API não exige disponibilidade de serviços externos para iniciar. O liveness verifica apenas o processo; o readiness consulta PostgreSQL e Redis. `llm-sim` e `plantao` não participam do readiness.

Os containers compartilham a rede Docker externa `desafio-kit`. Uma execução isolada de migração termina antes do início da API.

O cliente do LLM, o prompt, o parser e o rate limiter são compostos pelo worker de classificação.
Eles não são invocados pela API. A entrega de alertas urgentes é executada por um worker separado
e também não participa do processo da API nem do readiness.

## Integração isolada com o LLM

O pacote `triagem.llm` separa quatro responsabilidades: contrato HTTP, prompt, extração do JSON
e validação semântica. O cliente usa o contrato oficial `model/system/input`, envia a chave
somente no header `Authorization` e preserva status HTTP, id da geração, modelo e usage na
resposta de domínio.

O prompt `classifier-v1` mantém instruções exclusivamente em `system`. O campo `input` contém
um JSON apenas com assunto e mensagem; identificadores e dados pessoais do cliente não são
enviados. O parser aceita JSON puro, uma cerca markdown ou um único objeto com pequeno texto ao
redor. Em todos os casos, o objeto passa pelo mesmo schema Pydantic e valores fora dos enums não
são convertidos.

Nenhum desses componentes participa do readiness. A indisponibilidade do `llm-sim` continua sem
impedir a API de receber tickets.

## Rate limiter global do LLM

O rate limiter usa uma janela deslizante armazenada no Redis. Uma operação Lua única consulta o
relógio do Redis, remove entradas expiradas, registra uma permissão ou devolve o tempo de espera.
A atomicidade do script garante que processos e workers com conexões independentes compartilhem o
mesmo limite de cinco novas chamadas em qualquer janela temporal.

O `LLMClient` adquire a permissão imediatamente antes do POST. Se o Redis estiver indisponível, a
chamada falha fechada em vez de acessar o provedor sem limitação. A concorrência é controlada
separadamente por um semáforo local configurado por `LLM_MAX_CONCURRENCY`; ele não participa do
cálculo da taxa temporal.

O estado da janela possui TTL e é apenas coordenação operacional. Tickets, jobs e classificações
continuam armazenados exclusivamente no PostgreSQL.

## Circuit breaker do LLM

O cliente compartilha no Redis um circuit breaker com estados `closed`, `open` e `half_open`.
Falhas de transporte, timeout e HTTP 5xx contam para a abertura. Respostas que comprovam que o
provedor está acessível — inclusive 4xx e respostas 2xx inválidas — não caracterizam outage. O
estado e suas transições usam scripts Lua atômicos e o relógio do Redis, de modo que todos os
workers observam o mesmo circuito e somente um deles executa o probe em `half_open`.

Enquanto o circuito está aberto, o worker mantém o job no PostgreSQL e apenas atualiza
`available_at`; nenhuma chamada ao provedor é feita. A recusa em `open` e uma falha do probe em
`half_open` não consomem o limite de tentativas do job. Isso permite atravessar uma indisponibilidade
prolongada sem transformar o circuit breaker em fila ou mecanismo de durabilidade. Um probe bem
sucedido fecha o circuito e a classificação correspondente é persistida normalmente.

## Job engine de classificação

O `classification-worker` usa exclusivamente o PostgreSQL como fila durável. Em uma transação
curta, seleciona um job disponível com `FOR UPDATE SKIP LOCKED`, altera seu estado para
`processing`, incrementa a tentativa e grava `locked_at` e `locked_by`. A classificação ocorre
depois do commit da reserva, sem manter lock de linha durante o processamento.

Depois da reserva, o worker chama o `llm-sim` por meio do rate limiter global, extrai e valida a
resposta e persiste a classificação. A classificação, o vínculo
`processing_jobs.classification_id`, a conclusão do job e a atualização do ticket são confirmados
na mesma transação. A linha registra versão do classificador, id e modelo da geração, saída bruta,
tokens, custos, latência e resultado validado. Os custos de entrada e saída são calculados com
`Decimal` a partir do usage validado e o custo total é a soma exata dos dois componentes.

Falhas 429, 500, 503, timeout, rede e saída inválida são reagendadas por `available_at`, limitadas
por `max_attempts`. Indisponibilidade do rate limiter ou do circuit breaker também reagenda o
job, mas não consome tentativa porque nenhuma chamada ao provedor ocorreu. O atraso usa backoff
exponencial com jitter e teto configurável; `Retry-After` prevalece quando maior. Autenticação
inválida, erros HTTP permanentes e requisições inválidas — incluindo excesso de contexto — encerram
o job. O worker não dorme durante o backoff.

Antes de reservar, o worker recupera jobs `processing` cujo lease expirou. Se ainda houver
tentativas, eles retornam a `retry_scheduled`; se o limite tiver sido atingido, passam para
`failed`. `locked_at` determina a expiração; a finalização exige correspondência de `locked_by`
e `attempt_count`, usado como fencing token monotônico. Assim, um worker antigo não conclui um
job cujo lease já foi assumido por outro processo.

## Modelo de dados inicial

O PostgreSQL contém seis entidades principais:

- `tickets`: registro durável e idempotente do ticket, protegido por `UNIQUE (external_id)`;
- `classifier_versions`: versão imutável do modelo, prompt de sistema e schema de saída;
- `classifications`: histórico de classificações, com relação muitos-para-um com o ticket;
- `processing_jobs`: fila durável para classificação e entrega de alertas;
- `classification_attempts`: uma linha por reserva do job de classificação, com início, resultado,
  erro interno e eventual reagendamento;
- `alert_deliveries`: uma linha por tentativa de entrega, preservando payload, resultado e erro.

Jobs de alerta podem referenciar a classificação que os originou. Isso separa retry de entrega de uma nova execução da IA. Foreign keys usam `RESTRICT` para impedir a remoção acidental de histórico.

Cada classificação referencia obrigatoriamente a linha de `classifier_versions` correspondente ao
provedor, modelo e versão que a produziu. Uma reclassificação cria outro job e outra linha em
`classifications`; o resultado anterior e sua versão permanecem imutáveis.

## Ingestão

`POST /tickets` valida o contrato com Pydantic e canonicaliza o payload para calcular um SHA-256.
Uma única transação PostgreSQL insere o ticket e seu job de classificação. O endpoint retorna
somente após o commit e não chama Redis, LLM ou plantão.

A inserção usa `ON CONFLICT` sobre `uq_tickets_external_id`. Apenas a transação vencedora cria
o job; retries leem o registro já confirmado e comparam o hash. Hash diferente resulta em HTTP
409.

## Consultas de tickets

`GET /tickets/{external_id}` consulta o ticket pela identidade externa e retorna a classificação
mais recente, ordenada deterministicamente por `created_at` e `id`. Quando existe um job de
classificação em `pending`, `processing` ou `retry_scheduled`, seu estado também acompanha a
resposta. Jobs concluídos ou falhos não são apresentados como processamento pendente.

`GET /tickets` usa uma subconsulta com função de janela para associar cada ticket somente à sua
classificação atual. Categoria e prioridade filtram essa classificação; o período filtra
`tickets.received_at`. Predicados, contagem, ordenação, `OFFSET` e `LIMIT` são executados pelo
PostgreSQL. Tickets sem classificação aparecem na listagem sem esses filtros e são naturalmente
excluídos quando categoria ou prioridade é informada.

`GET /tickets/{external_id}/timeline` consulta apenas registros vinculados ao ticket e ordena os
eventos por timestamp e precedência determinística. Recebimento e entrada na fila vêm de `tickets`
e `processing_jobs`; execuções e retries do LLM vêm de `classification_attempts`; prioridade e
conclusão vêm de `classifications`; criação, tentativas, falhas e entrega do alerta vêm de
`alert_deliveries`. Jobs anteriores à migration continuam legíveis a partir dos campos agregados
existentes, embora somente tentativas posteriores possuam granularidade completa.

## Alertas urgentes

Quando o resultado validado possui prioridade `urgente`, o `classification-worker` persiste a
classificação, a primeira tentativa `pending` e um job `alert_delivery` na mesma transação. O
payload usa somente `external_id`, categoria, prioridade fixa `urgente`, um resumo determinístico
de assunto e mensagem limitado a 280 caracteres e o instante de persistência da classificação.
Não existe uma segunda chamada ao LLM.

O `alert-worker` reserva somente jobs de alerta com `FOR UPDATE SKIP LOCKED`, confirma a reserva
antes do HTTP e usa lease e fencing token da mesma forma que o worker de classificação. HTTP 202
conclui o job; 503, timeout, falha de rede e outros 5xx geram uma nova tentativa agendada; 422 e
outros 4xx são permanentes. Falhas do plantão nunca alteram nem recriam a classificação.

Cada tentativa ocupa uma linha numerada em `alert_deliveries`. O contador global e o último erro
ficam também no `processing_job`; status HTTP, erro, id retornado pelo plantão, início da tentativa,
próximo retry e entrega ficam na linha da tentativa. O primeiro e o último horário são obtidos de
`MIN/MAX(attempted_at)`, preservando todo o histórico em vez de sobrescrevê-lo.

A integração oferece semântica **at-least-once**. Se o plantão aceitar o alerta e o worker falhar
antes do commit local, a recuperação do lease repetirá a entrega. O contrato do plantão não fornece
chave de idempotência, portanto a aplicação não promete exactly-once.

## Observabilidade e segurança de entrada

A API instala um middleware de correlação. Um `X-Request-ID` com formato seguro é preservado; valor
ausente ou hostil é substituído por UUID. O mesmo ID entra nos eventos JSON emitidos durante a
requisição. Workers correlacionam eventos por `ticket_id`, `job_id`, `classification_id`, `alert_id`
e `provider_request_id`, conforme cada etapa dispõe desses valores.

Os eventos estruturados são formados por campos controlados. O formatter também descarta campos
cujo nome indique Authorization, API key, e-mail, mensagem, saída bruta ou system prompt. A API
expõe métricas Prometheus em `/metrics`; cada worker inicia um exporter próprio na porta 8000 da
rede interna. Contadores de custo são telemetria aproximada do protocolo Prometheus; os valores
financeiros autoritativos continuam como `NUMERIC` calculado com `Decimal` no PostgreSQL.

O contrato limita `mensagem` a 12.000 caracteres antes da persistência e da fila. Assunto e mensagem
são serializados como JSON no campo `input`; nunca são concatenados ao `system`. Conteúdo Unicode,
HTML, JSON ou instruções adversariais permanece dado, mas o prompt não é tratado como barreira de
segurança. Enums Pydantic, parser estrito, limites de tamanho, retries limitados e isolamento dos
efeitos urgentes continuam sendo as fronteiras aplicadas pelo sistema.
