# Rodar Radar

No Codex, abra a pasta `MetaAdsCollector` e envie **Rodar Radar**. O agente executa o Radar uma vez, acompanha a coleta, verifica comercialmente os candidatos e entrega até cinco ofertas qualificadas com links e evidências. Uma nova solicitação inicia o próximo ciclo depois que o anterior estiver fechado.

## Instalação

Use Python 3.12. Ao transferir o pacote, extraia em uma pasta privada e abra o terminal na pasta `MetaAdsCollector`. A partir do código incluído, instale o projeto em um ambiente virtual.

**Windows · PowerShell**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\meta-ads-radar.exe mine --help
```

**macOS · Terminal**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/meta-ads-radar mine --help
```

Os exemplos abaixo usam `meta-ads-radar` como abreviação para o executável da plataforma. O coletor usa a conexão local. Mantenha credenciais e configurações pessoais no próprio computador.

## Coleta e recuperação

1. Use sempre a pasta existente `radar-work` do projeto. Se o banco estiver em outra pasta, passe seu caminho absoluto em ambos os comandos. Se o histórico esperado não estiver presente, localize ou restaure a cópia correta antes de iniciar.
2. Execute `meta-ads-radar mine --workspace radar-work --assisted-review`. Acompanhe pelo terminal e por `radar-work/radar-operacao.jsonl`.
3. Confirme o ID, a cobertura e as contagens em `radar-work/relatorios/<ciclo>/relatorio.json`. Ao interromper, execute novamente o mesmo comando. O Radar reutiliza as keywords concluídas e também recupera um ciclo completo cuja revisão comercial ficou pendente.
4. Para fechar a shortlist, espere `FULL_COVERAGE` com 106/106. Em caso de bloqueio ou rate limit persistente, preserve o checkpoint e reporte a cobertura parcial, as falhas e as pendências. A ausência de resultados em uma execução parcial não estabelece ausência de ofertas.

O plano usa as 106 keywords oficiais de Fernando Oliveira PRO, Brasil, anúncios ativos e até 20 resultados por keyword. O candidato exige pelo menos cinco IDs únicos da mesma oferta e idade máxima de 60 dias, com os demais filtros de identidade e revisão do Radar. O enriquecimento de LP para agrupamento continua opcional.

## Verificação comercial pelo agente

Comece por `commercial_assessments` no JSON do ciclo. Essa lista contém todos os candidatos técnicos elegíveis, inclusive os que não aparecem na shortlist técnica resumida. Aproveite a evidência automática confirmada. Nos casos ainda indeterminados, examine pontualmente a página pública de venda e o checkout vinculado. Sem comprar, preencher formulários, autenticar, contornar bloqueios ou insistir num host que negou acesso.

Verifique se a oferta é um infoproduto digital brasileiro. Use o texto visível da página, a descrição do formato, informações do vendedor e o resumo do pedido para corroborar o mesmo produto e o preço de entrada em BRL. Registre a identidade do produto com evidência consistente entre a oferta e o checkout, inclusive quando não houver dados estruturados. Um valor promocional publicitário isolado, a mera moeda BRL ou uma URL de checkout genérica deixam a verificação em aberto. Preserve preço desconhecido, indisponibilidade, conflitos e dúvidas como investigação. Software, produto físico, serviço e referência internacional ficam fora da shortlist brasileira.

Use somente o preço real do produto de entrada. Ignore adicionais opcionais, upsells e downsells. Registre separadamente uma eventual taxa de serviço. Mantenha o teto padrão de R$47 e as faixas oficiais de preço. Ofertas acima do teto ficam no histórico.

| Preço de entrada verificado | Prioridade |
| --- | --- |
| Abaixo de R$10 | Secundária |
| De R$10 a R$27,90 | Máxima |
| Acima de R$27,90 até R$37 | Alta |
| Acima de R$37 até R$47 | Média |

Grave evidências verificadas em `radar-work/qualificacao-comercial.json` no [formato de parecer do Radar](../radar/README.md#retificação-de-identidade-e-qualificação-comercial). Acrescente apenas registros inéditos com vínculo ao `cycle_id`, `group_id` e `page_id` atuais e uma LP canônica compatível com o destino observado. Preserve os pareceres existentes e as decisões humanas no SQLite. Para qualificação positiva, inclua nome, nicho, formato, promessa declarada, mercado, preço, checkout, horário da verificação, fontes públicas e motivo verificável. O checkout deve estar entre as fontes. Deixe campos opcionais sem evidência em branco e descreva quaisquer pendências no relatório. A análise comercial não altera os grupos nem aprova testes de compra.

Finalize via `meta-ads-radar mine --workspace radar-work --report-cycle ID --assisted-review`. Esse comando aplica o parecer ao ciclo salvo e fecha o histórico da shortlist, sem nova consulta Meta ou às LPs. Uma repetição do fechamento usa a shortlist já registrada.

## Entrega no chat

Informe o ID do ciclo, FULL ou PARTIAL, as contagens de keywords, candidatos técnicos e oportunidades qualificadas, seguidas de até cinco itens da shortlist final. Para cada oferta, apresente nome, anunciante, nicho, formato, promessa observada, preço, faixa, LP, checkout e fontes. Inclua IDs observados, datas de início, diversidade verificável de criativos, crescimento quando houver base comparável, justificativa, riscos e próximo teste comercial. Quantidade de anúncios indica atividade observada. Vendas, receita, lucro e demanda exigem evidência independente.

Se houver menos de cinco, entregue só as verificadas. Se houver zero, apresente a shortlist vazia e explique os principais casos fora do escopo ou em investigação. Cite as limitações da análise, inclusive quando candidatos ficarem sem inspeção. Linke `shortlist.md` e `relatorio.md` da pasta do ciclo.

## Transferência entre computadores

Encerre a execução antes de transferir. Mantenha uma única cópia ativa do histórico e guarde um backup antes de substituir um banco existente. Transfira a pasta `radar-work` do pacote por um meio privado, com o código e a documentação da mesma versão. O pacote contém um snapshot SQLite verificado, os relatórios e os arquivos de continuidade disponíveis. O manifesto registra o commit e os SHA-256 do conteúdo.

Após instalar no outro computador, teste primeiro uma exportação offline com `meta-ads-radar mine --workspace radar-work --report-cycle ID`. Confira o ciclo, a cobertura e os pareceres antes de iniciar uma coleta nova. O mesmo fluxo vale no Windows e macOS. Nunca mescle duas cópias SQLite concorrentes por sincronização de arquivos.
