# Fase 4 — teste de carga da ingestão

Data da execução: 2026-10-16 (UTC).

## Escopo e ambiente

O teste foi executado contra `POST http://localhost:8000/tickets`, com a API e o PostgreSQL do `compose.yaml` do projeto em execução. O cliente foi o gerador oficial, sem alterações, executado no modo sem Docker documentado pelo próprio kit.

- gerador: `carga/enviar_tickets.py`, SHA-256 `04E5E4D85C2FCDE18EA154DEC573C0FD29F99F9FA1BAC0025B63A119F42B0103`;
- dataset: `carga/tickets.jsonl`, SHA-256 `F3C078D6B0BC140F08DA1D3614BD675DD0F8E0B859E13263B1DE913992EE44EC`;
- Python do gerador: 3.14.3;
- Python da API: 3.12.15;
- PostgreSQL: 16.15;
- migration aplicada: `20261016_0002`;
- rajada: 50 tickets por segundo;
- timeout do cliente: 2 segundos;
- máximo do cliente: 3 tentativas.

Antes da carga, o banco continha zero tickets e zero jobs de classificação. O dataset possui 500 linhas, 485 `external_id` distintos e 15 pares repetidos com payload idêntico.

Comandos equivalentes usados, a partir do diretório do projeto:

```powershell
$env:TARGET_URL = 'http://localhost:8000/tickets'
$env:ENVIOS_LOG = '<kit>/carga/out/fase4-20-20261016.jsonl'
& .\.venv\Scripts\python.exe '<kit>/carga/enviar_tickets.py' --limite 20

$env:ENVIOS_LOG = '<kit>/carga/out/fase4-500-20261016.jsonl'
& .\.venv\Scripts\python.exe '<kit>/carga/enviar_tickets.py'
```

## Resultados

| Métrica | Lote de 20 | Lote de 500 |
|---|---:|---:|
| Envios | 20 | 500 |
| Aceitos (2xx) | 20 | 496 |
| Rejeitados (4xx) | 0 | 4 |
| Falhas após as tentativas | 0 | 0 |
| Reenvios/retries | 0 | 0 |
| Duração total informada pelo gerador | 1,4 s | 10,6 s |
| p50 do endpoint | não disponível | não disponível |
| p95 do endpoint | não disponível | não disponível |
| p99 do endpoint | não disponível | não disponível |

O lote de 20 iniciou em `2026-10-16T15:49:05.602060Z`. O lote de 500 iniciou em `2026-10-16T15:49:34.224742Z`.

O gerador oficial não registra o instante de conclusão nem a duração de cada requisição. O access log atual do Uvicorn também não registra duração. Por isso, não há dados reais para calcular p50, p95 ou p99; a duração total acima não foi convertida em percentis nem usada para estimá-los.

As quatro rejeições do lote completo foram respostas 422 e correspondem a três IDs inválidos do próprio dataset:

- `ATD-2026339`: mensagem vazia; aparece duas vezes;
- `ATD-2026340`: assunto vazio e mensagem somente com espaços;
- `ATD-2026341`: assunto vazio.

Esses payloads violam a validação de entrada. Não houve timeout, erro de conexão ou resposta 5xx.

## Verificação no PostgreSQL

Após o lote de 20:

| Verificação | Resultado |
|---|---:|
| Tickets | 20 |
| Jobs de classificação | 20 |
| `external_id` duplicados | 0 |
| Tickets sem job de classificação | 0 |
| Tickets com quantidade de jobs diferente de 1 | 0 |
| Jobs órfãos | 0 |

Após o lote de 500, mantendo o lote anterior no banco:

| Verificação | Resultado |
|---|---:|
| Respostas aceitas no lote | 496 |
| IDs aceitos distintos | 482 |
| Tickets duráveis | 482 |
| Jobs de classificação | 482 |
| IDs aceitos ausentes no banco | 0 |
| IDs inesperados no banco | 0 |
| `external_id` duplicados | 0 |
| Tickets sem job de classificação | 0 |
| Tickets com quantidade de jobs diferente de 1 | 0 |
| Jobs órfãos | 0 |
| Status dos tickets | `received: 482` |
| Status dos jobs | `pending: 482` |

O lote completo reapresenta os 20 primeiros tickets do lote inicial. Entre os payloads válidos há ainda 14 repetições internas idênticas. Assim, as 496 respostas aceitas correspondem corretamente a 482 tickets distintos e somente 462 novas linhas foram criadas durante a segunda execução. Cada reenvio idempotente retornou sucesso apontando para o registro já durável e não criou outro job.

## Conclusão

O critério de durabilidade foi atendido: todos os IDs que receberam resposta de sucesso possuem ticket persistido no PostgreSQL e exatamente um job de classificação correspondente. Não foram observados timeout, perda, duplicação física ou inconsistência referencial. Nenhuma alteração na ingestão foi necessária.

Os logs brutos do gerador foram preservados no kit oficial em `carga/out/fase4-20-20261016.jsonl` e `carga/out/fase4-500-20261016.jsonl`.
