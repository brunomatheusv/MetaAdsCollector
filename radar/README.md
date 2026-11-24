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
