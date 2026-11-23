# Radar Low-Ticket — MVP Fase 1

O pacote `radar` e uma camada separada sobre o `MetaAdsCollector`. O core continua
responsavel apenas por coletar e normalizar o payload da Meta. O Radar faz
agrupamento deterministico, deduplicacao conservadora, calculo de idade e avaliacao
das evidencias.

`CANDIDATO_RADAR` significa somente que as evidencias desta coleta atendem aos
filtros oficiais do MVP. Nao significa oferta vencedora, demanda validada ou
validacao comercial.

## Uso

```bash
meta-ads-radar "curso online" "emagrecimento" \
  --limit 10 --max-age-days 60 \
  --json-output radar.json --markdown-output radar.md
```

O MVP aceita de 1 a 5 palavras-chave, usa exclusivamente `BR` e solicita apenas
anuncios ativos. A coleta pode retornar vazia ou incompleta por mudancas nos
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

## Permanecem manuais

A revisao de grupos ambiguos, a confirmacao de URLs/redirecionamentos, a avaliacao
comercial da oferta e qualquer decisao de produto, pagina ou criativo continuam
fora deste MVP.
