# Radar Low-Ticket — Fase 4A

O pacote `radar` e uma camada separada sobre o `MetaAdsCollector`. O core continua
responsavel apenas por coletar e normalizar o payload da Meta. O Radar faz
agrupamento deterministico, deduplicacao conservadora, calculo de idade e avaliacao
das evidencias.

`CANDIDATO_RADAR` significa somente que as evidencias desta coleta atendem aos
filtros oficiais do MVP. Nao significa oferta vencedora, demanda validada ou
validacao comercial.

## Uso

```bash
meta-ads-radar --registry radar/data/fernando-oliveira-pro-106.json --gate 10 --database radar.sqlite3 \
  --limit 10 --max-age-days 60 \
  --json-output radar.json --markdown-output radar.md \
  --events-output eventos.json --queue-json-output fila.json --queue-markdown-output fila.md
```

O registry canonico transcreve exatamente 106 strings, na ordem e grafia do PDF
oficial, com procedencia `Fernando Oliveira PRO` e as quatro categorias originais:
39 Formatos e materiais, 26 Nichos e temas, 21 Oferta/acesso/beneficios e
20 Plataformas/dominios.
Sem essa fonte o loader falha em vez de inventar termos. A execucao usa os gates
usados na validacao da Fase 2 (`10 -> 25 -> 50 -> 106`), em lotes configuraveis e com historico SQLite.
Na Fase 3, cada nova execucao e incremental e pode usar qualquer gate permitido sem repetir essa sequencia.

O Radar usa exclusivamente `BR` e solicita apenas anuncios ativos. A coleta pode
retornar vazia ou incompleta por mudancas nos
endpoints internos, verificacao ou rate limits da Meta; vazio nao prova ausencia
de anuncios.

## Regras conservadoras

- `page_id` sozinho nunca identifica uma oferta.
- URL canonica dentro do mesmo `page_id` e o sinal forte inicial. Parametros de
  tracking sao removidos, mas caminho e parametros comerciais sao preservados.
- Na ausencia de URL, texto criativo normalizado pode criar um grupo de confianca
  media. Sem sinal estavel, o anuncio fica separado e vai para investigacao.
- Anuncios multi-card com destinos distintos sao ambiguos: o MVP nao escolhe o
  primeiro card como se representasse sozinho a oferta e marca `INVESTIGAR`.
- IDs de anuncio iguais sao unidos entre palavras-chave; a quantidade 5+ usa IDs
  unicos, nao o total de observacoes.
- Criativos so sao contados como duplicados quando existe fingerprint de conteudo
  suficiente. Caso contrario a estimativa fica `null`.
- Data ausente produz `INCERTO` e `EVIDENCIA_INSUFICIENTE`; nenhuma data e inventada.

## Persistencia e revisao

O SQLite preserva execucoes, resultados por keyword, origens anuncio/keyword,
historico de contagem e os estados humanos `NOVO`, `REVISAR`, `APROVADO`,
`DESCARTADO` e `ACOMPANHAR`. Uma nova coleta nunca aprova automaticamente um grupo.

As tabelas aditivas `incremental_events` e `review_queue` sao criadas automaticamente
em bancos da Fase 2, sem destruir snapshots anteriores. Os eventos suportados sao
`NEW_GROUP`, `NEW_ADS`, `COUNT_INCREASED`, `ENTERED_RADAR`, `LEFT_RADAR`,
`STATUS_CHANGED` e `EVIDENCE_CHANGED`. `ENTERED_RADAR` descreve apenas a passagem
dos filtros tecnicos.

A fila ordena entrada no Radar, novo candidato, candidato com novos anuncios,
ambiguidade material e demais mudancas. Um grupo revisado e inalterado nao volta.
O acknowledgement (`--ack-group GROUP_ID`) remove o item pendente da execucao sem
alterar `review_status`. Eventos e itens usam chaves unicas por execucao/grupo para
que um rerun nao os duplique.
`--since-run-id ID` permite exportar a fila e os eventos posteriores a uma execucao
especifica; sem essa opcao, a CLI mostra somente a execucao que acabou de concluir.

O identificador do grupo deriva de `page_id` mais URL canonica ou texto normalizado.
Quando esses sinais faltam ou um multi-card tem destinos distintos, a identidade e
deliberadamente conservadora: o anuncio fica isolado e `INVESTIGAR`, evitando fundir
ofertas possivelmente diferentes. A ausencia completa de um grupo numa coleta nao
gera `LEFT_RADAR`; isso poderia ser apenas coleta parcial. A saida so e emitida quando
o mesmo grupo e observado com todos os IDs anteriores ainda presentes e muda de
`CANDIDATO_RADAR` para outro status tecnico.

## Permanecem manuais

A revisao de grupos ambiguos, a confirmacao de URLs/redirecionamentos, a avaliacao
comercial da oferta e qualquer decisao de produto, pagina ou criativo continuam
fora deste MVP.

## Ciclos recorrentes manuais

A Fase 4A separa o ciclo operacional dos `runs` tecnicos. Um ciclo guarda o plano,
hash do registry, configuracao, progresso por keyword e todos os `run_id` usados.
O resume aceita somente o mesmo registry/plano/configuracao, repete apenas falhas e
pendencias e nunca repete uma keyword concluida, inclusive quando o resultado foi
vazio.

```bash
# planejar um ciclo oficial (anote o cycle_id exibido)
meta-ads-radar --cycle-action start --registry radar/data/fernando-oliveira-pro-106.json \
  --gate 106 --database radar.sqlite3

# executar ou retomar manualmente
meta-ads-radar --cycle-action resume --cycle-id CYCLE_ID \
  --registry radar/data/fernando-oliveira-pro-106.json --gate 106 \
  --database radar.sqlite3 --json-output ciclo.json --markdown-output ciclo.md

# consultar sem scheduler, daemon ou notificacao externa
meta-ads-radar --cycle-action status --cycle-id CYCLE_ID \
  --registry radar/data/fernando-oliveira-pro-106.json --gate 106 --database radar.sqlite3
meta-ads-radar --cycle-action queue --cycle-id CYCLE_ID \
  --registry radar/data/fernando-oliveira-pro-106.json --gate 106 --database radar.sqlite3
```

`FULL_COVERAGE` exige sucesso nas 106 keywords do registry oficial; resultado vazio
conta como processado e erro nao. Gate/subset e sempre `PARTIAL_COVERAGE` em relacao
ao plano oficial. O limite (padrao `20`) limita a profundidade de cada keyword:
FULL do plano nunca significa cobertura total da Meta Ads Library.

Ausencia em ciclo parcial nao reduz a evidencia conhecida nem gera `LEFT_RADAR`.
Em ciclo completo, a primeira ausencia muda o frescor para `STALE`; somente a
segunda ausencia em ciclos FULL consecutivos muda para `POSSIBLY_GONE` e pode gerar
`LEFT_RADAR` para um grupo que era candidato. Mesmo assim, o evento e uma solicitacao
conservadora de revisao humana, nunca uma afirmacao de que a oferta morreu.

O ciclo continua manual: iniciar, executar/retomar, revisar a fila, reconhecer itens
e iniciar o proximo ciclo. Scheduler, cron, daemon, crawling avancado, IA semantica,
scoring comercial e notificacoes permanecem fora da Fase 4A.

## Fase 4B - identidade de oferta e revisao

Na V1, a chave era a pagina mais o primeiro destino, ou o texto normalizado
quando faltava destino. A raiz e links de plataformas podiam dar confianca
alta. A normalizacao descartava `src` e `ref`, inclusive quando distinguiam
ofertas. O fallback por texto podia promover copy repetida ao atingir cinco
IDs. A V1 isolava cards com destinos distintos, mas so mostrava o primeiro
destino e perdia um destino especifico quando apenas o primeiro card nao
possuia URL. Adicionar keywords a um ID repetido ja era suportado, mas
divergencias de destino entre essas observacoes eram descartadas.

A V2 usa as seguintes regras, sem consultas a landing pages:

- **ALTA / STRONG_SAME_OFFER** exige o mesmo page_id e o mesmo fingerprint
  de destino especifico, sem conflito conhecido. Variacoes de criativo podem
  integrar o mesmo grupo. Destinos especificos distintos permanecem separados,
  inclusive quando dividem anunciante, copy ou collation_id.
- **MEDIA / PROBABLE_SAME_OFFER** usa coincidencia exata de texto e fingerprint
  de criativo dentro da mesma pagina e da mesma chave de destino fraco ou
  ausente. O resultado segue para revisao humana.
- **BAIXA / AMBIGUOUS** cobre identidade isolada, cards heterogeneos, URLs
  invalidas e divergencias de destino para o mesmo ID. Um collation_id
  compartilhado entre destinos especificos distintos reduz a confianca dos
  respectivos grupos e nao serve para fundi-los.
- O fingerprint ignora HTTP/HTTPS e o prefixo www, normaliza o host e remove
  um slash final simples. Preserva caso do path, portas nao padrao, valores
  vazios e a ordem dos valores de parametros repetidos. Remove utm_* e
  identificadores conhecidos de clique, preservando parametros como ref, src
  e sku. Raizes, rotas genericas, plataformas e encurtadores reconhecidos nao
  sao destino especifico. A classificacao usa somente a URL observada.
- Apenas confianca alta pode passar para CANDIDATO_RADAR, com IDs distintos,
  atividade confirmada e dentro da janela do Radar. Em cobertura parcial, a
  ficha distingue IDs atuais dos IDs de um grupo V2 anteriormente observado.
  Ausencia parcial continua sem gerar LEFT_RADAR.

Os IDs V2 sao hashes de chaves versionadas. A tabela aditiva
`grouping_aliases` vincula identidades quando a pagina, o destino e todos os IDs
historicos V1 coincidem com um unico grupo V2 de alta confianca. A revisao e
a primeira observacao sao preservadas, e o historico pode ser consultado
pelo ID V2. Acknowledgements ficam no item original, com seu run e seu ID.
Sobreposicoes parciais, fusoes e splits entram para conciliacao humana.
Grupos legados sem correspondencia ficam no historico e nao geram saida
por ausencia em um ciclo V2.

O relatorio e a fila incluem as razoes de identidade, conflitos, sinais de
ruido, destinos de todos os cards e contexto de cobertura quando disponivel.
CANDIDATO_RADAR indica que o grupo passou filtros tecnicos. A verificacao
da oferta, do destino real de redirecionamentos e a decisao comercial
continuam manuais.

## Fase 4C — investigação seletiva de landing pages

A investigação é manual e opt-in. Use `--lp-enrichment` na coleta em lote ou
em `--cycle-action run/resume` para acrescentar review cards ao relatório.
O resultado da 4B permanece no histórico. Os cards da 4C apresentam identidade,
agrupamento e status propostos, sem aplicar decisões ao histórico ou à fila humana.

A seleção considera INVESTIGAR, janela, página, destino consultável, conflitos
e IDs únicos. O padrão é consultar grupos com 3 IDs ou grupos do mesmo page_id
com potencial conjunto de atingir o mínimo de 5 IDs. O potencial orienta a
seleção. Para propor uma união, cada grupo precisa de evidência STRONG com
o mesmo fingerprint na mesma página. Texto, títulos e raízes são sinais
complementares. Os critérios de atividade, idade e contagem da 4B continuam
valendo para candidaturas propostas.

O cliente consulta HTTP(S) diretamente, em série, com DNS validado e fixado
na conexão, TLS verificado, orçamento de tempo e de bytes e verificação de
cada redirect. Uma resposta 401, 403 ou 429 interrompe novas consultas ao host
durante a execução. A investigação usa o HTML recebido, sem executar scripts.
O cache SQLite guarda metadados e evidências por URL e versão. O histórico mantém evidências anteriores após a expiração. O TTL padrão é
24 horas. Erros de uma consulta permanecem UNRESOLVED.

Use uma cópia do banco para o replay. O comando lê o snapshot V2 do ciclo e
grava somente o cache e o histórico HTTP da 4C no SQLite. As consultas novas são destinadas às
LPs elegíveis. O replay também pode reutilizar o cache válido.

```bash
meta-ads-radar --cycle-action replay --cycle-id CYCLE_ID \
  --database radar-replay.sqlite3 --lp-enrichment \
  --json-output replay.json --markdown-output replay.md
```

As opções `--lp-min-ids`, `--lp-ttl-hours`, `--lp-timeout`,
`--lp-max-redirects`, `--lp-max-bytes` e `--lp-max-urls` configuram o gate,
o cache e os limites de inspeção. A cobertura FULL/PARTIAL continua descrevendo
o processamento dos termos da Meta.

## Fase 4D — recuperação e diagnóstico

Uma interrupção preserva os checkpoints já confirmados. Repita o comando com
`--cycle-action resume`, o mesmo ID, registry e parâmetros. O Radar reconhece
runs interrompidos e retoma apenas keywords pendentes ou com erro. A finalização
grava histórico, fila, frescor e conclusão do ciclo na mesma transação.
Uma execução concorrente contra o mesmo banco é recusada. O arquivo auxiliar
`*.lock.sqlite3` usa um bloqueio mantido pelo SQLite e liberado ao sair do
processo. Não apague esse arquivo durante uma execução.

O Radar pausa o ciclo diante de bloqueio ou rate limit e mantém a cobertura
parcial. Um timeout pode receber uma repetição curta, além dos mecanismos
do coletor. Respostas com sinal de coleta incompleta e checkpoints
inconsistentes não contam como buscas vazias bem-sucedidas. Ao receber um
bloqueio externo, retome somente depois de o acesso ser restabelecido.

Use `--log-json radar-operacao.jsonl` para registrar IDs, tentativas, categorias
de erro, recuperação e conclusão. O log operacional não inclui mensagens
brutas do coletor. Para recuperar os relatórios técnicos a partir de um
snapshot finalizado, sem rede ou nova coleta:

```bash
meta-ads-radar --cycle-action report --cycle-id CYCLE_ID \
  --database radar.sqlite3 --json-output radar.json --markdown-output radar.md
```

Os arquivos são gravados em UTF-8 e publicados por substituição atômica
individual. Se a exportação falhar depois da coleta, use `report` para repetir
a geração. Para exportar evidência de LP, use separadamente o replay opt-in.
Transfira o banco por backup SQLite verificado, com os relatórios e o SHA
Git associados. O arquivo de bloqueio local não faz parte do backup.

## Fase 5 — mineração e monitoramento por um comando

Para a rotina sob demanda **Rodar Radar**, consulte a
[operação local com verificação comercial](../docs/radar-operacao-local.md).

Instale o projeto em um ambiente Python isolado. No Windows, com Python 3.12
disponível pelo launcher:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\meta-ads-radar.exe mine
```

Em um ambiente já ativado, basta executar `meta-ads-radar mine`. O comando usa
o registro oficial embarcado de 106 keywords, Brasil, anúncios ativos, até 20
resultados por keyword e janela de 60 dias. O SQLite e o log ficam em
`radar-work/`. Use `--workspace CAMINHO` para escolher outra pasta. Preserve
a mesma pasta entre execuções para manter o histórico incremental.

Se houver interrupção, execute o mesmo comando. O Radar retoma o ciclo
automatizado pendente e pula checkpoints confirmados. Caso a coleta tenha
finalizado antes de uma falha na exportação, ele recupera o relatório antes
de abrir outro ciclo. O código de saída 2 identifica cobertura parcial. O
enriquecimento de LP segue opcional por `--lp-enrichment`.

Para monitoramento recorrente em primeiro plano, execute
`meta-ads-radar mine --every-hours 24`. O intervalo é contado após cada
execução. A janela precisa permanecer aberta. Ctrl+C preserva os checkpoints.
Novos ciclos comparam o histórico do mesmo banco, mantendo fila,
acknowledgements e decisões humanas.

Cada ciclo publica `relatorios/<ciclo>/shortlist.md`, `shortlist-tecnica.md`,
`relatorio.md` e `relatorio.json`. As shortlists têm até cinco itens por
padrão, ajustáveis entre 1 e 10 por `--shortlist`. A lista técnica mostra
candidatos de confiança alta com evidência do ciclo atual. A ordem usa
contagem de IDs observados, data de início mais recente e novos IDs frente
ao histórico. A classificação
não infere preço, vendas ou demanda. Cobertura parcial aparece como
provisória, e o Radar preserva o tratamento conservador de ausências.
O JSON inclui todas as keywords, grupos, eventos e a fila pendente.

Para recuperar uma exportação local sem repetir consultas:

```text
meta-ads-radar mine --workspace CAMINHO --report-cycle ID_DO_CICLO
```

Para transferir após a validação do Windows, feche a mineração e use um
backup SQLite verificado, o código ou wheel identificado pelo SHA e o
registro oficial. No macOS, crie um ambiente com `python3 -m venv .venv`,
instale o projeto e restaure `radar.sqlite3` na pasta escolhida. Confirme
primeiro com `mine --report-cycle`. A transferência inclui apenas
configurações operacionais públicas. Mantenha credenciais e configurações
de autenticação em cada computador.

## Retificação de identidade e qualificação comercial

O fingerprint de criativo ignora apenas uma lista explícita de parâmetros
voláteis nas URLs de mídia de `.fbcdn.net`. Host, caminho e os demais
parâmetros continuam fazendo parte da evidência. O histórico FULL oferece
aliases adicionais somente quando IDs, página, destinos e identidade
recalculada têm correspondência inequívoca. O histórico de eventos já
gravados continua consultável. `CREATIVE_REOBSERVED` registra os mesmos IDs,
página e destino quando faltam provas para conciliar a identidade da oferta.
A passagem do tempo, por si só, não gera alteração de evidência. A mudança
do critério de idade continua registrada.

A mineração exporta a shortlist comercial em `shortlist.md`, mantém os
candidatos técnicos em `shortlist-tecnica.md` e inclui todos os pareceres
comerciais no relatório. A shortlist comercial exige infoproduto low-ticket
brasileiro com preço em reais e checkout verificado. O arquivo local
`radar-work/qualificacao-comercial.json` aceita uma lista de pareceres
verificados com `cycle_id`, `group_id`, `page_id`, `lp`, `category`,
`verified_at` e `sources`. O parecer de um ciclo não vale automaticamente
para o próximo. Sem evidência vinculada, a oferta fica indeterminada.

As categorias são `INFOPRODUTO_LOW_TICKET_BR`, `SOFTWARE_SAAS`,
`PRODUTO_FISICO`, `SERVICO`, `REFERENCIA_INTERNACIONAL` e `INDETERMINADO`.
Pareceres brasileiros qualificados também incluem `market: BR`, `currency: BRL`,
`price`, `price_verified: true`, `name`, `niche`, `format`, `promise`
e `checkout`, com a página de checkout na lista de fontes. O parecer
comercial não altera o agrupamento, as regras de candidatura ou a revisão
humana no SQLite. Use `mine --report-cycle ID` para aplicar o parecer à
exportação de um ciclo salvo sem fazer nova coleta.
