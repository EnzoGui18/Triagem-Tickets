# Uso de inteligência artificial

## Declaração de uso

Este projeto teve auxílio substancial de inteligência artificial durante sua implementação. O
OpenAI Codex foi usado como assistente de engenharia para analisar o repositório, propor alterações,
escrever e revisar código, criar testes, executar comandos de validação, investigar falhas e preparar
documentação técnica.

Portanto, não é correto afirmar que todo o código foi escrito manualmente. A IA participou tanto da
produção de trechos de código quanto do processo de verificação. O resultado final, porém, não foi
aceito com base apenas na resposta do modelo: o comportamento foi confrontado com os requisitos,
com o código existente e com execuções reais.

## Ferramentas utilizadas

- **OpenAI Codex:** análise, geração e edição assistida de código, testes e documentação;
- **PowerShell e ferramentas de terminal:** inspeção de arquivos, execução da aplicação e coleta de
  evidências;
- **Git e `git diff`:** controle de versão e revisão das alterações;
- **ripgrep (`rg`):** navegação e busca no código;
- **Docker e Docker Compose:** construção das imagens e execução do ambiente integrado;
- **pytest e pytest-asyncio:** testes unitários, de persistência, concorrência e integração;
- **Ruff e MyPy:** lint e verificação estática de tipos;
- **Alembic:** validação da sequência de migrations, upgrade, downgrade e detecção de diferenças;
- **gerador de carga e simuladores oficiais:** validação de ingestão, integração, falhas e
  performance.

As ferramentas de terminal, teste e infraestrutura listadas acima não são ferramentas generativas;
foram usadas para revisar e validar o trabalho assistido por IA. O `llm-sim` faz parte do sistema em
execução e não foi usado como ferramenta de desenvolvimento.

## Partes que receberam auxílio

O auxílio de IA abrangeu, em graus diferentes:

- bootstrap de FastAPI, configurações, PostgreSQL, Redis, SQLAlchemy assíncrono, Alembic e Docker;
- modelos, constraints, índices e migrations;
- ingestão idempotente e testes concorrentes;
- job engine com reserva por `FOR UPDATE SKIP LOCKED`, lease recovery e fencing;
- cliente HTTPX do LLM, prompt, parser, schemas e tratamento de erros;
- rate limiter e circuit breaker distribuídos pelo Redis;
- retries, backoff, versionamento e cálculo de custos com `Decimal`;
- consultas, paginação e timeline operacional;
- geração durável e entrega at-least-once de alertas urgentes;
- logging estruturado, métricas e testes de entradas hostis;
- scripts e procedimentos de carga, E2E, chaos e tuning;
- documentação de arquitetura, decisões, resultados e execução reproduzível.

Isso inclui código de aplicação e de testes. A presença de testes também gerados com auxílio de IA
não foi tratada como prova suficiente: sempre que possível, os testes foram executados contra
PostgreSQL e Redis reais e complementados por cenários com os serviços oficiais.

## Como as sugestões foram revisadas

As sugestões não foram aplicadas de forma isolada. O processo usado em cada fase foi:

1. ler requisitos, decisões arquiteturais e implementação existente antes da alteração;
2. limitar a mudança ao escopo da fase e preservar componentes já funcionais;
3. revisar o diff e confrontar modelos SQLAlchemy, migrations, contratos HTTP e configurações;
4. executar testes focados e depois a suíte completa;
5. executar lint, type checking e validações do Compose e do Alembic;
6. exercitar o sistema integrado e investigar resultados divergentes;
7. registrar falhas, limitações e números reais sem substituir resultados por estimativas.

Falhas encontradas durante carga e chaos foram analisadas a partir de logs, estado do PostgreSQL,
estado dos jobs e resultados dos simuladores. Testes não foram removidos ou relaxados apenas para
obter uma execução verde. Limitações observadas, como expiração de lease sob pausas longas e a
capacidade máxima do provedor, permanecem documentadas.

## Validação do código assistido

O código e a documentação foram validados por diferentes níveis de evidência:

| Validação | O que verificou |
|---|---|
| Testes unitários | schemas, parser, prompt, custos, tratamento HTTP e regras isoladas |
| Testes com PostgreSQL | constraints, idempotência, concorrência, jobs, histórico e consultas |
| Testes com Redis | rate limiter e circuit breaker compartilhados entre clientes independentes |
| Testes de migrations | upgrade, downgrade e compatibilidade entre ORM e banco |
| Testes de integração | workers reais, retries, alertas, timeline e transações |
| Ruff e MyPy | qualidade estática, imports e tipos |
| Docker Compose | build, configuração, dependências e inicialização dos serviços |
| Gerador oficial | ingestão de lotes de 20 e 500 tickets |
| E2E e chaos | outage, reinício de serviços, duplicatas, conflitos e payloads inválidos |
| Revisão em ambiente limpo | instruções do README, migrations, smoke test, carga e consultas |

Na revisão final anterior a este documento, a suíte integrada em Python 3.12, PostgreSQL e Redis
reais executou **112 testes com sucesso**, com um warning de depreciação Starlette/httpx. Ruff,
MyPy, `docker compose config`, o build da imagem e `git diff --check` também passaram. A execução
local sem URLs de infraestrutura passou em 52 testes e pulou explicitamente 60 testes que exigem
PostgreSQL ou Redis.

Na revisão cruzada desta fase, a primeira execução integrada terminou com 111 testes aprovados e
uma falha de ordenação no teste de timeline. O mesmo teste passou quando executado isoladamente e a
repetição da suíte completa terminou com 112 testes aprovados. A ocorrência não foi ocultada nem
tratada como prova de correção após uma única repetição; permanece como intermitência a acompanhar,
possivelmente sensível ao relógio do ambiente Docker. Nenhuma alteração de código foi feita sem uma
causa reproduzível.

Os resultados de carga, falha e performance não foram inferidos pela IA. Eles foram coletados em
execuções reais e estão registrados em:

- [`docs/load-test-results.md`](docs/load-test-results.md);
- [`docs/e2e-chaos-results.md`](docs/e2e-chaos-results.md);
- [`docs/performance-tuning-results.md`](docs/performance-tuning-results.md).

## Decisões humanas e responsabilidade

Os requisitos de negócio, os limites de cada fase, a arquitetura obrigatória e os critérios de
aceite foram definidos pelo responsável humano e pelo enunciado do desafio. Isso inclui, entre
outros pontos:

- PostgreSQL como fonte de verdade e fila durável;
- Redis somente para coordenação operacional;
- ingestão desacoplada da classificação;
- idempotência por `external_id` protegida por constraint;
- limite global de cinco novas chamadas ao LLM por segundo;
- histórico append-only de classificações;
- alertas independentes com semântica at-least-once;
- proibição de incluir PII desnecessária no LLM e nos logs.

A IA auxiliou a transformar essas restrições em implementação e também sugeriu detalhes técnicos.
Essas sugestões foram avaliadas no contexto do sistema e mantidas somente quando compatíveis com o
contrato e com as evidências obtidas. Decisões como avançar entre fases, solicitar commits e pushes,
preservar os limites do desafio e aceitar o estado entregue permaneceram sob direção humana.

Nenhuma decisão arquitetural importante deve ser interpretada como autoridade autônoma da IA. A
responsabilidade final pela revisão, aprovação, implantação e operação do software continua sendo
humana.

## Limitações desta declaração

Testes reduzem risco, mas não demonstram ausência de defeitos. Modelos de linguagem podem sugerir
código incorreto, omitir casos de borda ou produzir explicações convincentes sem evidência. Por
isso, esta declaração não substitui code review humano, revisão de segurança ou validação no
ambiente de produção.

Os números citados correspondem às execuções documentadas e não constituem benchmark de produção.
A capacidade de classificação continua limitada a cinco novas chamadas por segundo, mesmo quando a
ingestão recebe até 50 tickets por segundo; sob carga sustentada, não existe garantia matemática de
classificar todos os tickets dentro de um minuto.
