# Arquitetura

Nesta fase, a aplicação é uma API FastAPI assíncrona com PostgreSQL como banco de dados e Redis para coordenação operacional futura. SQLAlchemy usa o driver `asyncpg`, e o esquema do banco é versionado pelo Alembic.

O processo da API não exige disponibilidade de serviços externos para iniciar. O liveness verifica apenas o processo; o readiness consulta PostgreSQL e Redis. `llm-sim` e `plantao` não participam do readiness.

Os containers compartilham a rede Docker externa `desafio-kit`. Uma execução isolada de migração termina antes do início da API.

Chamadas ao LLM, entrega de alertas e rate limiting ainda não fazem parte da aplicação.

## Job engine de classificação

O `classification-worker` usa exclusivamente o PostgreSQL como fila durável. Em uma transação
curta, seleciona um job disponível com `FOR UPDATE SKIP LOCKED`, altera seu estado para
`processing`, incrementa a tentativa e grava `locked_at` e `locked_by`. A classificação ocorre
depois do commit da reserva, sem manter lock de linha durante o processamento.

Nesta fase, o worker usa um `FakeClassifier` determinístico e não realiza chamadas HTTP. A
classificação, o vínculo `processing_jobs.classification_id`, a conclusão do job e a atualização
do ticket são confirmados na mesma transação.

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
