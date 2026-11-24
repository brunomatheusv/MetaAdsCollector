# Validacao controlada da Fase 4A

Data: 24 de novembro de 2026 (`America/Sao_Paulo`).

## Contrato e testes

Os testes cobrem os cenarios A-P: 106/106, erro 105+1, vazio como sucesso,
gate parcial, interrupcao/resume, nao repeticao, bloqueio de incompatibilidade,
reconciliacao parcial positiva e conservadora, duas ausencias FULL, reaparecimento,
estado humano/acknowledgement, idempotencia, migracao e aviso de profundidade.

`FULL_COVERAGE` significa que todas as 106 keywords do plano oficial foram
processadas com sucesso. Nao significa cobertura total da Meta Ads Library porque
cada keyword continua limitada a no maximo 20 resultados.

## Migracao real em copia

Uma copia de 3,9 MB do banco usado nas Fases 2/3 foi migrada. As contagens foram
identicas antes e depois: 5 runs, 201 resultados de keyword, 3.967 origens de ads,
1.326 grupos, 2.677 snapshots, 31 eventos e 31 itens de fila. Foram adicionadas
somente `collection_cycles`, `cycle_keywords`, `cycle_runs` e `group_freshness`.

## Coleta real economica: interrupcao e resume

Foi criado um ciclo controlado com as duas primeiras keywords oficiais (`ebook` e
`apostila`), `BR`, anuncios ativos, `limit=5`, lote 1 e sem proxy pago. A primeira
invocacao processou somente `ebook` e parou deliberadamente; a segunda retomou
somente `apostila`.

- parcial: 1 keyword, 5 ads, 3,951 s;
- resume: 1 keyword restante, 5 ads, 4,201 s;
- total: 10 observacoes/IDs, 8 grupos;
- erros: 0; vazios: 0; sinais de rate limit: 0;
- cada keyword teve exatamente 1 tentativa;
- cobertura final: `PARTIAL_COVERAGE`, 2/106 (subset por definicao);
- segundo run: 5 eventos positivos e 5 itens pendentes na fila;
- nenhuma ausencia foi convertida em `LEFT_RADAR`.

O ciclo real permaneceu parcial mesmo depois de concluir as duas keywords do subset,
provando que gate/subset nao se apresenta como ciclo oficial completo.
