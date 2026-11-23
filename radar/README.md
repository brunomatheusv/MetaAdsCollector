# Radar Low-Ticket — Fase 2

O pacote `radar` e uma camada separada sobre o `MetaAdsCollector`. O core continua
responsavel apenas por coletar e normalizar o payload da Meta. O Radar faz
agrupamento deterministico, deduplicacao conservadora, calculo de idade e avaliacao
das evidencias.

`CANDIDATO_RADAR` significa somente que as evidencias desta coleta atendem aos
filtros oficiais do MVP. Nao significa oferta vencedora, demanda validada ou
validacao comercial.

## Uso

```bash
meta-ads-radar --registry fernando-106.json --gate 10 --database radar.sqlite3 \
  --limit 10 --max-age-days 60 \
  --json-output radar.json --markdown-output radar.md
```

O registry deve ser um JSON com exatamente 106 strings, na ordem e grafia do
material fornecido pelo usuario, todas com procedencia `Fernando Oliveira PRO`.
Sem essa fonte o loader falha em vez de inventar termos. A execucao usa os gates
obrigatorios `10 -> 25 -> 50 -> 106`, em lotes configuraveis e com historico SQLite.

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

## Permanecem manuais

A revisao de grupos ambiguos, a confirmacao de URLs/redirecionamentos, a avaliacao
comercial da oferta e qualquer decisao de produto, pagina ou criativo continuam
fora deste MVP.
