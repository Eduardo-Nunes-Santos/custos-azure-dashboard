# custos-azure-dashboard

Painel web para acompanhar os custos de uma subscription do Azure e auditar o ambiente: mostra quanto cada máquina virtual (e cada recurso ligado a ela) custa por mês, qual a previsão de gasto, como o custo se distribui por tags e quais recursos estão órfãos, ou seja, gerando custo sem estar em uso.

## Sobre o projeto

O objetivo é substituir o cruzamento manual entre planilhas de faturamento e inventário de VMs. O script `azure_dashboard.py` consulta as APIs do Azure (inventário e Cost Management), cruza os dados e gera um `dashboard.html` de arquivo único, sem dependência de internet para abrir. No container, o painel é atualizado automaticamente e servido por HTTP, pronto para ser consultado por toda a equipe.

## Funcionalidades

- **Máquinas virtuais:** status (ligada, desligada, desalocada), Resource Group, região, tamanho, SO, custo da VM, custo total e previsão do mês. Clique na linha para ver o custo de cada recurso associado (VM, discos, NICs e IPs públicos).
- **VMs por tag:** agrupa as VMs pelo valor de uma tag à sua escolha (ex.: Ambiente, Setor, Centro de Custo), com quantidade por status, custo, previsão e % do total.
- **Custo por recurso (VM):** tabela única com todos os recursos de cada VM e seus custos.
- **Recursos órfãos:** discos não anexados, NICs sem VM e IPs públicos sem uso, com o custo de cada um.
- **Outros custos (sem VM):** recursos com consumo no mês que não são VM, disco, NIC nem IP do inventário (storage, rede, bancos etc.).
- **Seletor de mês:** consulta o mês atual e os meses anteriores (padrão: 3), com comparação contra o mês anterior.
- **KPIs e gráficos:** custo do mês, previsão, média diária, variação contra o mês anterior, contagem por status e custo diário (realizado e previsto).
- Tema claro e escuro automático, busca, ordenação por coluna e filtros.

## Como funciona

```
Azure Resource Manager API ─┐
                            ├─► azure_dashboard.py ─► template.html ─► dashboard.html
Azure Cost Management API ──┘        (coleta)          (+ Chart.js)     (arquivo único)
```

1. O script autentica com um Service Principal e lista VMs, discos, NICs e IPs públicos.
2. Consulta o Cost Management (custo por recurso e custo diário) de cada mês.
3. Cruza custos e inventário, calcula previsão e órfãos.
4. Injeta os dados no `template.html` (marcador `__DATA__`) e embute o Chart.js, gerando um HTML que funciona sem internet.

No container, um loop reexecuta o script a cada `REFRESH_MINUTES` e serve o resultado por HTTP.

## Requisitos

- Python 3.10 ou superior (execução local) ou Podman/Docker (container).
- Um Service Principal com as roles **Reader** e **Cost Management Reader** na subscription.

Para criar o Service Principal:

```bash
az ad sp create-for-rbac --name azure-finops-dashboard \
    --role Reader --scopes /subscriptions/<SUBSCRIPTION_ID>

az role assignment create --assignee <CLIENT_ID> \
    --role "Cost Management Reader" --scope /subscriptions/<SUBSCRIPTION_ID>
```

## Configuração

Copie `.env.example` para `.env` e preencha:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TENANT_ID` | sim | ID do tenant do Entra ID |
| `CLIENT_ID` | sim | ID do aplicativo (Service Principal) |
| `CLIENT_SECRET` | sim | Segredo do aplicativo |
| `SUBSCRIPTION_ID` | sim | ID da subscription a analisar |
| `MESES_HISTORICO` | não | Quantos meses anteriores incluir (padrão: 3) |
| `OUTPUT_DIR` | não | Pasta de saída (padrão: a pasta do script) |
| `REFRESH_MINUTES` | não | Intervalo de atualização no container (padrão: 60) |
| `PORT` | não | Porta HTTP no container (padrão: 8080) |

> **Nunca versione o `.env`.** Ele está no `.gitignore`. Com `podman --env-file`, não use aspas nos valores.

## Execução local

```bash
pip install -r requirements.txt
python azure_dashboard.py                  # mês atual + 3 anteriores
python azure_dashboard.py --mes 2026-09    # mês principal específico
python azure_dashboard.py --historico 6    # 6 meses anteriores
```

O resultado é o `dashboard.html`, que pode ser aberto direto no navegador. O `data.json` ao lado traz os dados coletados, útil para conferência.

## Execução em container (Podman)

```bash
podman build -t azure-dashboard .
podman run -d --init --name azure-dashboard --restart=always -p 8080:8080 \
    --env-file .env -v azure-dashboard-data:/data azure-dashboard
```

Acesse `http://<servidor>:8080`. Comandos úteis:

```bash
podman logs -f azure-dashboard                                   # acompanhar a coleta
podman restart azure-dashboard                                   # atualizar agora
podman exec azure-dashboard sh -c 'rm -f /data/.cache_*' \
    && podman restart azure-dashboard                            # atualizar ignorando o cache
```

Comportamento do container:

- Na primeira execução, a página mostra "Gerando o dashboard..." e publica primeiro só o mês atual; o histórico entra em seguida.
- Se uma atualização falhar, a versão anterior continua no ar. Se ainda não existir versão, a página exibe o motivo do erro.
- Somente a pasta `/data/www` é exposta pela web. `data.json` e o cache ficam fora dela.
- O volume `/data` guarda o cache, então reinícios são rápidos.

### Iniciar no boot (Quadlet)

O arquivo `azure-dashboard.container` é um modelo para o Podman 4.4 ou superior. Copie para `~/.config/containers/systemd/`, ajuste o caminho do `.env` e, no modo rootless, execute `loginctl enable-linger <usuario>`.

### Acesso por nome e porta 80

O Podman sem root costuma falhar ao publicar a porta 80. Mantenha o container na 8080 e use um redirecionamento do firewall ou um proxy reverso (nginx, Caddy etc.) na frente. Em servidores com firewalld:

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --permanent --add-forward-port=port=80:proto=tcp:toport=8080
sudo firewall-cmd --reload
```

## Dados e limitações

- **Não é tempo real.** O HTML é uma foto do momento da coleta. O custo no Azure leva de 8 a 24 horas para refletir o consumo.
- **Previsão:** custo acumulado + média diária dos últimos 7 dias × dias restantes do mês. Por VM, é uma estimativa proporcional ao total. Meses fechados não têm previsão.
- **Meses anteriores:** só os custos mudam. Status, tags e recursos órfãos refletem o estado atual do Azure, e recursos já excluídos aparecem em "Outros custos".
- **Custo da VM** = VM + discos (SO e dados) + NICs + IPs públicos associados. Outros recursos ligados à VM (extensões, snapshots, backups) não entram.
- **Limite da API (erro 429):** o Cost Management limita as consultas e o limite é compartilhado por quem usa o mesmo Service Principal. O script espera o tempo informado pelo Azure e tenta de novo (até 8 vezes), espaça as consultas e guarda cache em disco (1 hora para o mês atual, 6 horas para meses fechados). Rode **uma única instância** por subscription.
- Uma subscription por execução.

## Segurança

O painel exibe informações de custo e **não tem autenticação**. Restrinja o acesso por firewall ou VPN, ou publique atrás de um proxy reverso com HTTPS e controle de acesso. O Service Principal tem apenas permissões de leitura.

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| "Nenhuma VM retornada" no painel | `SUBSCRIPTION_ID` errado ou falta a role Reader |
| "Nenhum custo retornado neste mês" | Falta Cost Management Reader, ou o mês ainda não tem dados |
| Erro 401 na página ou no log | `TENANT_ID`, `CLIENT_ID` ou `CLIENT_SECRET` inválidos (verifique aspas no `.env`) |
| Erro 429 / mensagens de espera | Limite da API de custos; aguarde, não rode várias instâncias |
| Gráficos ausentes | Falta `vendor/chart.umd.min.js`; sem ele o script usa o CDN, que pode estar bloqueado |
| `Connection reset` na porta 80 | Publicação de porta baixa no Podman sem root; use a 8080 com redirecionamento |

## Estrutura do repositório

```
azure_dashboard.py          coleta os dados e gera o dashboard.html
template.html               modelo do painel (mantenha o marcador __DATA__)
vendor/chart.umd.min.js     Chart.js embutido (funciona offline)
Containerfile               imagem do container
entrypoint.sh               loop de atualização + servidor HTTP
azure-dashboard.container   modelo Quadlet (systemd)
requirements.txt            dependências Python
.env.example                modelo de configuração
```
