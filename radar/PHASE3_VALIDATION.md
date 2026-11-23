# Validacao controlada da Fase 3

Data: 23 de novembro de 2026. Execucao local entre 11:50:56 e 11:52:46
(`America/Sao_Paulo`), com duracao medida de 109,646 segundos.

Foi feita exatamente uma nova coleta real depois dos testes. Ela usou uma copia do
banco com as quatro execucoes da Fase 2, as primeiras 10 keywords do registry
Fernando oficial, limite de 20 resultados por keyword, lotes de 5, intervalo de
1 segundo e backoff de 5 segundos. A sequencia de gates da Fase 2 nao foi refeita.

Resultados:

- 10 requests, todas `OK`; 0 erros, 0 vazios e 0 possiveis rate limits.
- 200 observacoes brutas, 177 IDs unicos e 146 grupos.
- 0 `CANDIDATO_RADAR`, 13 `INVESTIGAR`, 133 `REJEITADO` e 0 evidencia insuficiente.
- 31 eventos `EVIDENCE_CHANGED`. Nesta amostra curta nao houve entrada ou saida do
  Radar, novos IDs ou aumento de contagem em relacao ao snapshot historico.
- 31 itens de fila: 7 `REVISAR_AMBIGUIDADE` e 24 `ACOMPANHAR`.
- O banco original da Fase 2 nao foi alterado; a migracao e a coleta ocorreram numa
  copia local ignorada pelo Git.

As mudancas observadas foram explicadas por keywords, IDs/datas/idades e URLs. Como
a execucao usou somente 10 keywords contra o gate historico completo, reducoes de
evidencia podem refletir cobertura parcial. Por isso, a implementacao nunca emite
`LEFT_RADAR` quando IDs historicos deixam de aparecer; a saida exige que todos os
IDs anteriores continuem presentes e que o status tecnico piore.
