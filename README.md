# Sincronização Notion → GoHighLevel

Automatiza a sincronização do **stage** de cada lead do Notion (BD *Comercial*) para o pipeline correspondente no **GoHighLevel**, mantendo as Smart Lists do GHL sempre certas para enviar newsletters segmentadas — sem ninguém ter de selecionar leads à mão.

Corre todos os dias às **06:00 UTC** (≈ 07:00 Lisboa no horário de verão) através do **GitHub Actions** (grátis).

---

## Como funciona

1. Lê todas as páginas da BD `Comercial` no Notion.
2. Para cada lead, procura no GHL primeiro por **email**, depois por **telefone**.
3. Se o contacto **existe**, atualiza o stage da oportunidade no pipeline.
4. Se **não existe**, cria contacto novo + oportunidade no stage certo (com a tag `notion-sync`).
5. Leads com Status `Perdida` no Notion ficam com `opportunity.status = lost` no GHL.
6. Cada execução guarda um relatório em `reports/sync-YYYYMMDD-HHMMSS.md`.

### Mapeamento Notion → GHL

| Notion (`Status`) | GHL Stage |
|---|---|
| Novas Leads | Nova Lead |
| Contactada | Contactada |
| Reunião Análise | Reunião Análise |
| Reunião Follow Up | Reunião Follow Up |
| Q&A | Q&A |
| Nutrição | Nutrição |
| Fechado | Fechada |
| € Cash Collected € | Cash Collected €€ |
| Aberto a Upsell/Crossell | Mostrou interesse em Upsell |
| Reunião Monetização | Reunião Monetização |
| Monetização Fechada | Monetização Fechada |
| **Perdida** | *(opportunity.status = lost)* |

Se quiseres mudar/adicionar mapeamentos, edita o dicionário `STATUS_TO_STAGE` no topo do `sync.py`.

---

## Setup inicial (uma vez só)

### 1) Criar o repositório no GitHub

1. Vai a https://github.com/new
2. Cria um repo **privado** (importante — vai conter referências a tokens). Nome sugerido: `notion-ghl-sync`.
3. Não inicializes com README/license (já tens estes ficheiros).
4. No teu computador, abre uma linha de comandos dentro desta pasta e corre:
   ```bash
   git init
   git add .
   git commit -m "initial: sync notion -> ghl"
   git branch -M main
   git remote add origin https://github.com/<o-teu-utilizador>/notion-ghl-sync.git
   git push -u origin main
   ```

> Se não tens o Git instalado: instala daqui → https://git-scm.com/download/win

### 2) Adicionar os Secrets no GitHub

No teu repo: **Settings → Secrets and variables → Actions → New repository secret**. Cria estes secrets (um a um):

| Nome | Valor |
|---|---|
| `NOTION_TOKEN` | O Internal Integration Secret (começa com `ntn_…`) |
| `NOTION_DB_ID` | `53447aa7b46940108a91672874da6e8f` |
| `GHL_TOKEN` | Private Integration Token do GHL (começa com `pit-…`) |
| `GHL_LOCATION_ID` | `yzY2x446cZJhr0VCb3dr` |
| `PIPELINE_NAME` | *(opcional)* nome do pipeline; deixa vazio para usar o 1.º |

### 3) Confirmar que a integração Notion tem acesso à BD

Abre a BD **Comercial** no Notion → menu `⋯` (canto sup. direito) → **Connections** → confirma que a integração que criaste (ex.: *Sync GHL*) está adicionada. Se não estiver, o script vai falhar com erro 404 ao chamar o Notion.

### 4) Primeiro teste em DRY RUN

1. Repo no GitHub → separador **Actions** → workflow **Sync Notion -> GHL** → **Run workflow**.
2. No campo *Correr em modo DRY RUN*, mete **`true`** e clica **Run workflow**.
3. Quando acabar, abre o run e:
   - Vê o **Summary** no fim para um resumo Markdown.
   - Vê o artifact `sync-report-…` para o relatório completo.
4. Se o resumo mostra contadores razoáveis e zero `errors`, podes correr **a sério**.

### 5) Primeira execução LIVE

Mesma coisa, mas mete `false` em DRY RUN (ou simplesmente espera pelas 06:00 UTC e corre automaticamente).

---

## Trocas e ajustes

- **Mudar a hora do sync**: edita o cron em `.github/workflows/sync.yml`. Por exemplo `"0 5 * * 1-5"` = às 05:00 UTC de segunda a sexta.
- **Mudar mapping de stages**: edita o dicionário `STATUS_TO_STAGE` em `sync.py`, faz commit + push.
- **Desativar temporariamente**: Actions → workflow → **Disable workflow**.
- **Rodar tokens**: gera novos no Notion / GHL, atualiza os Secrets no GitHub.

---

## Verificações de segurança

- O token do GHL foi partilhado em chat durante o setup → **regenera-o no GHL** assim que confirmares que o sync funciona, e atualiza o secret `GHL_TOKEN` no GitHub.
- Mantém o repositório **privado**. Os secrets nunca aparecem nos logs do Actions, mas o repo privado é a primeira camada de defesa.
- O `.env` está no `.gitignore`. Nunca commits credenciais no código.

---

## Problemas comuns

| Sintoma | Causa provável | Resolução |
|---|---|---|
| `404` ao ler Notion | Integração não foi adicionada à BD via **Connections** | Adiciona como descrito no passo 3 |
| `401` no GHL | Token expirou ou foi rodado | Cria novo Private Integration token, atualiza o secret |
| Status no Notion sem mapping (warning) | Valor novo no `Status` do Notion | Adiciona-o a `STATUS_TO_STAGE` em `sync.py` |
| Lead criada duas vezes | Lead no Notion sem email *e* sem telefone | Preenche email/telefone no Notion |
