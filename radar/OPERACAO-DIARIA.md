# Operação diária do Radar Low-Ticket

## Windows e macOS

Use Python 3.12 no ambiente virtual. No Windows, a partir da raiz do projeto:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\meta-ads-radar.exe mine
```

O teto padrão é R$47, ajustável com `--low-ticket-max-brl`. Entre R$10
e R$27,90 a prioridade é máxima. Acima de R$27,90 até R$37 é alta.
Acima de R$37 até o teto é média. Abaixo de R$10 é secundária.
Acima do teto, a oferta fica no histórico e fora da shortlist.
A evidência de preço se refere ao produto de entrada. Adicionais, upsells
e downsells não são somados. Preço desconhecido permanece em investigação.
No macOS, use `python3.12 -m venv .venv`, instale com
`.venv/bin/python -m pip install .` e rode `.venv/bin/meta-ads-radar`
com os mesmos argumentos. Preserve a pasta `radar-work` do pacote ou passe
`--workspace` com o mesmo caminho em cada execução. Para reexportar um
ciclo salvo sem rede, use `mine --report-cycle ID`.

O comando mantém as 106 keywords, Brasil, máximo de 20 resultados por termo,
cinco ou mais anúncios ativos da mesma oferta e janela de 60 dias.
`--every-hours 24` executa em primeiro plano enquanto o computador estiver
ligado. Em um agendador externo, execute uma vez por dia sem essa opção.
O código de saída 2 sinaliza PARTIAL, preservando checkpoints para retomada.
O SQLite bloqueia duas execuções concorrentes no mesmo workspace.

## Qualificação e priorização

A inspeção automática consulta serialmente até 12 candidatos técnicos,
com até 24 URLs iniciais, 36 requisições HTTP incluindo redirecionamentos,
timeout de 8 segundos por consulta e pausa de hosts que respondam 401, 403
ou 429. Usa o fetcher HTTP com DNS protegido já existente. As evidências
são salvas por ciclo e reaproveitadas ao reexportar.

Para a via automática, a LP precisa identificar um curso online, e-book ou
material digital em dados estruturados, com vendedor brasileiro, oferta
unitária disponível e preço em BRL. O checkout HTTP precisa corroborar
nome, SKU e valor. Ofertas recorrentes ou ambíguas, HTML sem metadados
suficientes e divergências ficam indeterminados. O preço é o observado
no checkout, sujeito a mudança. Pareceres verificados localmente continuam
aceitos e, quando existe um teto informado, passam pelo mesmo limite.

A shortlist comercial entrega até cinco ofertas. Novidades precedem
ofertas já divulgadas. Uma repetição exige crescimento na contagem de
anúncios ou mudança de nome, preço, moeda, formato, promessa ou checkout.
Entre ofertas novas ou revisitas, a ordem das faixas é máxima, alta, média e secundária. A nota
dá 2 pontos para identidade e formato, 2 para preço no checkout, 3/2/1/0
pela faixa, 1 para 10+ anúncios, 1 para dois ou mais fingerprints de
criativo verificáveis e 1 para crescimento positivo.
A nota indica prioridade de investigação, sem inferir vendas ou lucro.
Campo sem fonte permanece ausente. O tempo exibido é o decorrido desde
o primeiro anúncio observado, sem presumir veiculação ininterrupta.

O relatório diário está em `relatorios/<ciclo>/shortlist.md`. O relatório
integral inclui todos os pareceres, dados faltantes, riscos, sinais,
links e próximos passos. Preserve também `historico-shortlist.json`
para supressão de repetição e as evidências JSON do ciclo.

## Teste de profundidade no Windows

Os mesmos 103 de 106 termos atingiram 20 nos dois ciclos FULL. Em cada
ciclo, dois termos ficaram vazios e um abaixo do limite. Atingir 20 indica
saturação do limite amostral, não esgotamento da Biblioteca.

O piloto usou `ebook`, `planilha`, `hotmart`, `kiwify` e `apostila pdf`.
Cada profundidade é prefixo da mesma consulta por termo. Somente `ebook`
foi até 100. As contagens de requisições são chamadas lógicas ao método de
busca paginada, sem contar bootstrap ou tentativas internas de transporte.

| Cenário | IDs únicos no piloto | Grupos | IDs novos frente ao FULL | Grupos novos frente ao FULL | Chamadas | Tempo somado |
|---|---:|---:|---:|---:|---:|---:|
| 20 nos cinco termos | 100 | 77 | 0 | 0 | 13 | 43,8 s |
| 50 nos cinco termos | 248 | 188 | 140 | 103 | 35 | 115,5 s |
| 100 em ebook, 50 nos demais | 296 | 232 | 184 | 144 | 47 | 151,9 s |

Nenhum cenário acrescentou candidato que cumprisse o critério técnico,
mesmo combinado com o último FULL. Portanto houve zero oportunidades
brasileiras adicionais qualificadas. O piloto não mede a qualidade comercial
dos anúncios que não passaram pelo filtro técnico. Houve zero erros, bloqueios
e sinais de rate limit. A configuração recomendada continua em 20 por
keyword para a rotina, com aprofundamentos pontuais quando houver motivo.

## Nuvem e entrega no ChatGPT

**Oracle Cloud Always Free.** Uma VM Linux elegível com disco de boot
persistente permite executar o comando por timer, manter o SQLite em
volume privado e fazer backup validado. O limite documentado é 200 GB
de volumes, incluindo discos de boot. Não há limite de duração por
invocação da aplicação em uma VM. Há escassez de capacidade e risco de
recolhimento de VMs ociosas. A variante AMD x64 evita depender de um
wheel Arm até a verificação. A rede da VM e a compatibilidade real com
a Meta precisam de um piloto autorizado. [Limites OCI](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

**GitHub Actions.** Um ciclo de cerca de 20 minutos por dia consome
aproximadamente 600–620 minutos Linux por mês, antes de setup e retries.
A referência de GitHub Free privado informa 2.000 minutos e 500 MB de
armazenamento incluído, e um job hospedado tem máximo de seis horas.
O agendamento pode atrasar ou perder execuções e exige workflow na branch
padrão. Runner efêmero precisa de estado privado durável separado.
Cache com expulsão e artefatos com retenção não substituem o banco.
A alternativa não foi implantada e não pressupõe alterar main ou enviar
o SQLite ao GitHub. [Cobrança](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
· [Limite de job](https://docs.github.com/en/actions/reference/limits)
· [Agendamento](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
· [Cache](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)

**Google Compute Engine.** O plano gratuito documenta um e2-micro em
regiões americanas elegíveis, 30 GB de disco padrão e 1 GB mensal de
egresso elegível. IPv4 público padrão custa US$ 0,005/h acima da
franquia de uma hora mensal, cerca de US$ 3,60–3,72 em 30–31 dias se
ligado continuamente. Distribuição por IPv6 exige confirmar que a Meta
e demais destinos são alcançáveis. [Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)
· [Preços de rede](https://cloud.google.com/vpc/network-pricing)

**ChatGPT.** Tarefas web podem usar ferramentas e apps autorizados,
mas não leem o SQLite local com o computador desligado. A integração
oficial mais simples a testar é uma tarefa por horário usando leitura
do resumo autorizado em repositório privado via GitHub, sem banco ou
credenciais no repositório. A integração do GitHub é dependente do
plano/superfície e documenta leitura de conteúdo do repositório. Eventos
do GitHub documentados são de pull request, sem gatilho confirmado de
término do job. O teste de aceite deve usar apenas um resumo real, contendo
ID do ciclo e hash, e confirmar a leitura com a autenticação do usuário.
O fluxo autenticado ponta a ponta não foi comprovado aqui. Um MCP remoto
autenticado e somente leitura é outra alternativa, mas depende de
hospedagem e plano/permissões compatíveis. Nenhum deploy, conector,
agendamento ou envio a terceiros foi criado.
[ChatGPT Tasks](https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt)
· [GitHub no ChatGPT](https://help.openai.com/en/articles/11145903-connecting-github-to-chatgpt)
· [MCP personalizado](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

Não há configuração ponta a ponta comprovada como gratuita e confiável
nesta validação. Para avaliar continuidade real, faça primeiro um piloto
autorizado de execução externa com o estado privado, sem proxies, com
parada em bloqueio, backup SQLite e exclusão mútua. Valide separadamente
a leitura autenticada de apenas um relatório sanitizado no ChatGPT.
