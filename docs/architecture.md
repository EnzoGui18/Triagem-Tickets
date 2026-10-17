# Arquitetura

Nesta fase, a aplicação é uma API FastAPI assíncrona com PostgreSQL como banco de dados e Redis para coordenação operacional. SQLAlchemy usa o driver `asyncpg`, e o esquema do banco é versionado pelo Alembic.

O processo da API não exige disponibilidade de serviços externos para iniciar. O liveness verifica apenas o processo; o readiness consulta PostgreSQL e Redis. `llm-sim` e `plantao` não participam do readiness.

Os containers compartilham a rede Docker externa `desafio-kit`. Uma execução isolada de migração termina antes do início da API.

O cliente do LLM, o prompt, o parser e o rate limiter são compostos pelo worker de classificação.
Eles não são invocados pela API. Entrega de alertas ainda não faz parte da aplicação.

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

## Job engine de classificação

O `classification-worker` usa exclusivamente o PostgreSQL como fila durável. Em uma transação
curta, seleciona um job disponível com `FOR UPDATE SKIP LOCKED`, altera seu estado para
`processing`, incrementa a tentativa e grava `locked_at` e `locked_by`. A classificação ocorre
depois do commit da reserva, sem manter lock de linha durante o processamento.

Depois da reserva, o worker chama o `llm-sim` por meio do rate limiter global, extrai e valida a
resposta e persiste a classificação. A classificação, o vínculo
`processing_jobs.classification_id`, a conclusão do job e a atualização do ticket são confirmados
na mesma transação. A linha registra versão do classificador, id e modelo da geração, saída bruta,
tokens, latência e resultado validado.

Falhas 429, 500, 503, timeout, rede, indisponibilidade do rate limiter e saída inválida são
reagendadas por `available_at`, limitadas por `max_attempts`. O atraso usa backoff exponencial com
jitter e teto configurável; `Retry-After` prevalece quando maior. Autenticação inválida, erros HTTP
permanentes e requisições inválidas — incluindo excesso de contexto — encerram o job. O worker não
dorme durante o backoff.

Antes de reservar, o worker recupera jobs `processing` cujo lease expirou. Se ainda houver
tentativas, eles retornam a `retry_scheduled`; se o limite tiver sido atingido, passam para
`failed`. `locked_at` determina a expiração; a finalização exige correspondência de `locked_by`
e `attempt_count`, usado como fencing token monotônico. Assim, um worker antigo não conclui um
job cujo lease já foi assumido por outro processo.

## Modelo de dados inicial

O PostgreSQL contém cinco entidades principais:

- `tickets`: registro durável e idempotente do ticket, protegido por `UNIQUE (external_id)`;
- `classifier_versions`: versão imutável do modelo, prompt de sistema e schema de saída;
- `classifications`: histórico de classificações, com relação muitos-para-um com o ticket;
- `processing_jobs`: fila durável para classificação e entrega de alertas;
- `alert_deliveries`: uma linha por tentativa de entrega, preservando payload, resultado e erro.

Jobs de alerta podem referenciar a classificação que os originou. Isso separa retry de entrega de uma nova execução da IA. Foreign keys usam `RESTRICT` para impedir a remoção acidental de histórico.

## Ingestão

`POST /tickets` valida o contrato com Pydantic e canonicaliza o payload para calcular um SHA-256.
Uma única transação PostgreSQL insere o ticket e seu job de classificação. O endpoint retorna
somente após o commit e não chama Redis, LLM ou plantão.

A inserção usa `ON CONFLICT` sobre `uq_tickets_external_id`. Apenas a transação vencedora cria
o job; retries leem o registro já confirmado e comparam o hash. Hash diferente resulta em HTTP
409.
