# Fase 15 — teste E2E e chaos

Data da execução: 2026-10-19/20 (America/Fortaleza/UTC).

## Ambiente e método

O teste foi executado com a aplicação completa em Docker Compose e com o kit oficial
localizado em `kit-desafio-triagem`. O gerador e o dataset oficiais não foram alterados:

- `carga/enviar_tickets.py`: SHA-256
  `04E5E4D85C2FCDE18EA154DEC573C0FD29F99F9FA1BAC0025B63A119F42B0103`;
- `carga/tickets.jsonl`: SHA-256
  `F3C078D6B0BC140F08DA1D3614BD675DD0F8E0B859E13263B1DE913992EE44EC`;
- migration aplicada: `20261019_0005`;
- banco isolado: `triagem_e2e_phase15_20261019`, criado vazio;
- serviços oficiais: `llm-sim` e `plantao`;
- classificação: cinco réplicas durante a drenagem do lote, compartilhando o rate limiter
  global no Redis;
- banco principal do projeto: preservado, sem limpeza ou alteração destrutiva.

A rede `desafio-kit` já existia sem os labels esperados pelo Compose oficial. Os containers
da aplicação foram removidos sem remover volumes, o kit criou a rede e a aplicação foi
subida novamente. Essa é uma restrição de ordem de inicialização do ambiente: o kit deve
criar a rede antes da aplicação ingressar nela.

O gerador oficial não registra duração individual por request. Portanto, p50, p95 e p99 não
estão disponíveis e não foram estimados.

## Cenários

### Cenário 1 — 20 tickets

**Expected Behavior:** os 20 envios devem ser aceitos rapidamente, persistidos e receber
exatamente um job durável cada.

**Actual Behavior:** o gerador terminou em 1,0 s: 20 aceitos, 0 rejeitados, 0 falhas e 0
reenvios. A primeira leitura do PostgreSQL encontrou 20 tickets únicos, 20 jobs, nenhum
ticket sem job e nenhum job duplicado. Nesse instante, os jobs estavam distribuídos em 5
`completed`, 13 `pending`, 1 `processing` e 1 `retry_scheduled`.

**Result:** PASS.

### Cenário 2 — 500 tickets

**Expected Behavior:** respostas válidas devem ser duráveis e idempotentes; entradas
inválidas devem retornar 422 sem criar ticket ou job.

**Actual Behavior:** o gerador terminou em 10,0 s: 495 aceitos, 5 rejeitados com 422, 0
falhas após as tentativas e 0 reenvios. As rejeições foram:

- `ATD-2026338`: mensagem com 53.960 caracteres, acima do limite de 12.000;
- `ATD-2026339`: mensagem vazia, presente duas vezes;
- `ATD-2026340`: assunto vazio e mensagem somente com espaços;
- `ATD-2026341`: assunto vazio.

O lote reapresenta os primeiros 20 tickets do cenário anterior e possui duplicatas internas.
Ao término da ingestão, o banco continha 481 tickets oficiais únicos e 481 jobs de
classificação, sem duplicação física ou ticket sem job. Após a drenagem, 478 jobs estavam
`completed` e 3 `failed`: um após três respostas transitórias do LLM
(`llm_http_500` no estado final) e dois por `lease_exhausted`.

**Result:** PASS para ingestão, idempotência e durabilidade; WARNING para processamento
devido à limitação de lease descrita em "Divergências investigadas".

### Cenário 3 — LLM offline por 60 segundos durante ingestão

**Expected Behavior:** a API deve continuar retornando 202; tickets e jobs devem permanecer
duráveis e voltar a processar automaticamente quando o provedor se recuperar.

**Actual Behavior:** o endpoint oficial `POST /admin/outage?segundos=60` foi acionado. Cinco
tickets novos foram enviados e os cinco retornaram 202. Durante a indisponibilidade havia 5
tickets, 5 jobs em `retry_scheduled` (`llm_circuit_open` ou
`llm_circuit_probe_failed`) e 0 classifications. Sem intervenção no banco, os cinco jobs
foram posteriormente concluídos e produziram cinco classifications.

**Result:** PASS.

### Cenário 4 — classification-worker desligado

**Expected Behavior:** a ingestão deve continuar e os jobs devem aguardar no PostgreSQL até
o worker voltar.

**Actual Behavior:** todas as cinco réplicas foram paradas. Três tickets retornaram 202 e
ficaram como 3 tickets, 3 jobs `pending` e 0 classifications. Após o restart, os três jobs
chegaram automaticamente a `completed`, com três classifications.

**Result:** PASS.

### Cenário 5 — alert-worker desligado

**Expected Behavior:** uma classificação urgente deve criar o alerta durável, mas nenhuma
entrega HTTP deve ser necessária antes do worker voltar.

**Actual Behavior:** com o alert worker parado, um ticket retornou 202 e foi classificado
como `pagamento/urgente`. A transação deixou um alert job `pending` e um
`alert_delivery` `pending`, tentativa 1. Após o restart, a primeira chamada ao plantão
retornou 202; o job ficou `completed` e a entrega `delivered`. O ticket manteve somente uma
classification.

**Result:** PASS.

### Cenário 6 — Redis reiniciado

**Expected Behavior:** o readiness deve indicar indisponibilidade do Redis, mas a ingestão
deve continuar porque depende somente do PostgreSQL. A classificação deve aguardar a
coordenação voltar.

**Actual Behavior:** com Redis parado, `/health/ready` retornou 503 e `POST /tickets`
retornou 202. O ticket e o job ficaram duráveis; o job foi reagendado com
`rate_limiter_unavailable`, `attempt_count=0` e nenhuma classification. Após reiniciar o
Redis, o readiness retornou 200 e o job terminou `completed`, com uma classification e
`attempt_count=1`.

**Result:** PASS.

### Cenário 7 — ticket duplicado

**Expected Behavior:** o mesmo `external_id` e o mesmo payload devem retornar o registro já
conhecido, sem novo ticket ou job.

**Actual Behavior:** os dois POSTs retornaram 202 e o mesmo UUID
`0ba2535f-d88b-4a97-b664-ca6c529c5723`. O PostgreSQL manteve 1 ticket e 1 job de
classificação.

**Result:** PASS.

### Cenário 8 — mesmo external_id com payload diferente

**Expected Behavior:** a primeira ingestão deve retornar 202 e a segunda 409, sem criar novo
ticket ou job.

**Actual Behavior:** a primeira chamada retornou 202 e a segunda retornou 409 com a mensagem
`external_id já está associado a um payload diferente`. Permaneceram 1 ticket e 1 job.

**Result:** PASS.

### Cenário 9 — payload inválido

**Expected Behavior:** o payload deve retornar 422 e não deve ser persistido.

**Actual Behavior:** um payload com e-mail, assunto, mensagem e timestamp inválidos retornou
422 com os erros estruturados de validação. Não foi criado ticket nem job para
`P15-INVALID-01`.

**Result:** PASS.

### Cenário 10 — reinício da API

**Expected Behavior:** pode haver indisponibilidade durante o restart, mas dados já aceitos
devem permanecer no PostgreSQL e a API deve voltar a aceitar tráfego.

**Actual Behavior:** um ticket retornou 202 antes do restart. Durante a reinicialização, dois
polls do health não obtiveram conexão; o terceiro retornou 200. Depois do restart,
`GET /tickets/P15-API-RESTART-01` retornou 200 e o mesmo UUID persistido. Um segundo ticket
enviado depois do restart retornou 202. Cada ticket possui exatamente um job.

**Result:** PASS.

## Invariantes finais

| Invariante | Resultado |
|---|---:|
| Respostas aceitas (2xx), incluindo duplicatas idempotentes | 530 |
| Respostas rejeitadas (4xx) | 7 |
| Reenvios do gerador oficial | 0 |
| Tickets únicos aceitos | 495 |
| Tickets persistidos | 495 |
| `external_id` distintos | 495 |
| Jobs de classificação | 495 |
| Classificações persistidas | 492 |
| Jobs `completed` | 492 |
| Jobs `pending` | 0 |
| Jobs `processing` | 0 |
| Jobs `retry_scheduled` | 0 |
| Jobs `failed` | 3 |
| Tickets sem job de classificação | 0 |
| Tickets com quantidade de jobs diferente de 1 | 0 |
| Jobs órfãos | 0 |
| Classifications órfãs | 0 |
| Jobs concluídos sem classification | 0 |
| Tickets com classifications duplicadas não solicitadas | 0 |
| Alert jobs concluídos | 49 |
| Alertas entregues | 49 |

As 495 linhas são explicadas integralmente por `492 classified + 3 failed`; nenhum ticket
ficou fora de um estado observável. Os três failures pertencem ao lote oficial. Todos os
tickets específicos dos cenários de outage, workers desligados, Redis, idempotência,
conflito e restart da API terminaram classificados.

O snapshot final de `llm-sim` desde sua inicialização mostrou 583 requests, 524 respostas
200, 59 erros 5xx, 29 outputs intencionalmente malformados e 0 respostas 429. Esse snapshot
inclui o preflight realizado antes da troca para o banco E2E e, por isso, não é usado para
derivar uma taxa exclusiva dos 495 tickets.

## Divergências investigadas

### Lease esgotado sob carga

Duas terceiras tentativas ficaram reservadas por aproximadamente 38–43 segundos e foram
recuperadas por outro worker após ultrapassarem o lease configurado de 30 segundos. Quando
os workers originais terminaram, perderam corretamente a posse e não persistiram uma
segunda classificação. Como as tentativas já tinham atingido `max_attempts=3`, os jobs
foram finalizados como `FAILED/lease_exhausted`.

O mecanismo preservou exclusão mútua, durabilidade e auditabilidade, mas o lease abrange a
espera por coordenação anterior à chamada HTTP e não possui heartbeat. Assim, 30 segundos é
uma margem insuficiente se o processo/container sofrer pausa prolongada. Não foi adicionada
uma renovação de lease nesta fase porque isso seria mudança funcional; a mitigação deve ser
tratada explicitamente em fase posterior, por aumento conservador do lease ou heartbeat.

### Teste concorrente intermitente

Na primeira suíte completa em Docker, 111 testes passaram e uma iteração do teste de workers
concorrentes falhou deixando um job em retry. Na repetição focada sem instrumentação houve
outra ocorrência em 40 casos parametrizados: o job concluiu após uma segunda tentativa, sem
classificação duplicada. Com captura temporária da exceção em memória, 60 de 60 casos
passaram e a exceção não voltou a ocorrer. A suíte completa final passou com 112 de 112
testes. A intermitência não foi escondida nem corrigida por relaxamento do teste e permanece
como risco a acompanhar.

## Validações auxiliares

- `docker compose build/up`: concluído;
- migrations em banco vazio: upgrade até `20261019_0005` concluído;
- health da API, PostgreSQL, Redis, LLM e plantão: 200 antes dos cenários;
- suíte completa final no container: 112 passed, 1 warning;
- Ruff: `All checks passed!`;
- MyPy: `Success: no issues found in 31 source files`.

