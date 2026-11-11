# Babel — Pipeline de Mineração de Violações de Linters

Pipeline em 3 etapas: seleciona repositórios no GitHub → extrai histórico de
commits relevantes → roda linters nos arquivos alterados e grava as
violações num dataset (`violations.jsonl` + arquivos arquivados em `Babel/`).

## Estrutura de arquivos

```
babel-mining/
  select_repositories.py
  extract_commit_history.py
  common.py
  linter_adapters.py
  run_linters.py
  requirements.txt
```

Os 5 arquivos `.py` devem estar **na mesma pasta** — `common.py` e
`linter_adapters.py` são importados por `run_linters.py`, e
`select_repositories.py` importa `linter_adapters.py` também (pra saber
quais linters existem e checar adoção antes de clonar).

Gerados automaticamente pelo próprio pipeline (não precisa criar nada disso):

- `selected_repos.jsonl` — saída da etapa 1
- `commits.jsonl` — saída da etapa 2
- `violations.jsonl` — saída da etapa 3 (o dataset em si)
- `repos_cache/` — clones locais dos repositórios
- `Babel/` — cópia de cada arquivo que teve violação, organizada por
  `linguagem/linter/repo/branch/commit/`

## Ambiente e reprodutibilidade

Três coisas diferentes precisam de versão registrada, porque cada uma tem
sua própria forma de "travar":

| O quê | Como fica reprodutível |
|---|---|
| Dependências Python (`requests`, `pylint`) | `requirements.txt` — gere com `pip freeze` do seu venv (ver abaixo), não confie só nos mínimos que vêm no arquivo |
| Jar do checkstyle | Nome do arquivo baixado (`checkstyle-X.X.X-all.jar`) já grava a versão — baixe uma versão **específica**, não `releases/latest` (isso muda com o tempo e deixa de bater com o que gerou seus dados) |
| ESLint | O `package.json` de cada repo minerado já fixa a versão via `devDependencies` — mas o `eslint` **global** que você instala tem sua própria versão, registre qual |

Em todos os três casos, o dado gerado já se autodocumenta por violação —
todo registro em `violations.jsonl` tem um campo `linter_version` com a
versão exata que gerou aquela linha especificamente. O que falta
documentar é só o **setup do zero**, pra alguém (inclusive você, daqui a
uns meses) conseguir montar o mesmo ambiente de novo.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip freeze > requirements.txt   # sobrescreve com as versoes exatas resolvidas
```

## Pré-requisitos

- Python 3.10+, `git`
- Java **21+** — obrigatório a partir do checkstyle 13.x (versões mais
  antigas de Java dão `UnsupportedClassVersionError`). Confira com
  `java -version`.
- Node.js + npm — só se for rodar o `eslint`. Confira com `node -v` e `npm -v`.
- Token do GitHub, criado em https://github.com/settings/tokens (o escopo
  `public_repo` já basta). **Sem token, o rate limit da API cai pra 60
  requisições/hora**, inviável pra minerar vários repositórios.

**Se estiver no Windows**: não rode dentro de pasta sincronizada por
OneDrive/Google Drive/Dropbox. Clonar repositório grande cria milhares de
arquivo pequeno rápido demais pro sync acompanhar, e isso causa lentidão
e erros de "arquivo já existe"/travado no meio do clone. Use uma pasta
local comum (ex: `C:\dev\babel`). No Linux isso não é um problema.

## Etapa 1 — Selecionar repositórios

```bash
pip install -r requirements.txt
export GITHUB_TOKEN="seu_token_aqui"

python3 select_repositories.py --language Python --linter pylint --sample-size 3
python3 select_repositories.py --language Java --linter checkstyle --sample-size 20
```

- `--language`: linguagem do GitHub (`Python`, `Java`, `JavaScript`, `TypeScript`, ...)
- `--linter`: **obrigatório** — só aceita repositório que comprovadamente
  adota esse linter (checado via API do GitHub, sem clonar). Isso acopla
  seleção e linter de propósito: "usa pylint" e "usa checkstyle" são
  populações de repositório diferentes, mesmo dentro da mesma linguagem.
- `--sample-size N`: quantos repositórios **no total** essa combinação
  linguagem+linter deve ter ao final. É incremental: os já selecionados
  são preservados e só a diferença é sorteada (ex: já tem 3 de Java e
  pede 20 → adiciona 17 novos).
- Roda uma vez por combinação linguagem+linter que quiser minerar — a
  saída é cumulativa (*append*), não apaga o que já tem.
- Filtros embutidos: sem fork, licença permissiva (MIT/Apache-2.0/BSD),
  atividade recente, contribuidores mínimos, nomes de "toy repo"
  excluídos, e a checagem de adoção do linter. O log final mostra quantos
  candidatos caíram em cada filtro (`Descartados -- nome: X | licenca: Y
  | nao adota <linter>: W | build nao suportado (nao-Maven): Z |
  contribuidores: V`).
- `build nao suportado` (hoje só checkstyle): exige `pom.xml` na raiz do
  HEAD (**só Maven**; Gradle fica de fora, como no Styler). Usa a mesma
  árvore de arquivos já baixada para checar a adoção (1 chamada de API
  por candidato). Config com `${variavel}` **não** é mais critério de
  projeto: virou exclusão por commit (etapa 3).

**Saída:** `selected_repos.jsonl`

## Etapa 2 — Extrair histórico de commits

```bash
python3 extract_commit_history.py \
    --input selected_repos.jsonl \
    --repos-dir ~/dev/babel/repos_cache \
    --max-commits-per-repo 50
```

- `--repos-dir`: pasta onde os clones ficam salvos. **Anote esse caminho —
  as etapas seguintes precisam apontar pro mesmo lugar**, senão os commits
  são pulados em silêncio por não achar o clone.
- `--max-commits-per-repo`: limita quantos commits recentes (por repo)
  entram na mineração, restritos aos que tocaram arquivo da linguagem alvo.
  Evita rodar o linter em milhares de commits de um projeto grande.
- Também cumulativo (append) em `commits.jsonl`.

**Saída:** `commits.jsonl` + clones em `repos_cache/`

## Etapa 3 — Rodar os linters

Cada linter é um **subcomando** de `run_linters.py` — o nome do linter vem
logo depois do nome do script, antes das outras flags. Pra ver todas as
flags de um linter específico: `python3 run_linters.py <linter> --help`

### Python — pylint

```bash
python3 run_linters.py pylint \
    --commits commits.jsonl \
    --repos-dir ~/dev/babel/repos_cache \
    --dataset-dir ./Babel \
    --output violations.jsonl
```

### Java — checkstyle

Baixe uma versão **específica** (não `releases/latest` — isso muda com o
tempo e um "mesmo comando" passa a baixar outra versão daqui uns meses):

```bash
mkdir -p ~/dev/babel && cd ~/dev/babel
wget https://github.com/checkstyle/checkstyle/releases/download/checkstyle-13.10.0/checkstyle-13.10.0-all.jar

python3 run_linters.py checkstyle \
    --commits commits.jsonl \
    --repos-dir ~/dev/babel/repos_cache \
    --dataset-dir ./Babel \
    --checkstyle-jar ~/dev/babel/checkstyle-13.10.0-all.jar \
    --output violations.jsonl
```

### JavaScript/TypeScript — eslint

```bash
sudo npm install -g eslint

python3 run_linters.py eslint \
    --commits commits.jsonl \
    --repos-dir ~/dev/babel/repos_cache \
    --dataset-dir ./Babel \
    --output violations.jsonl
```

Antes de lintar, o adapter roda `npm install --ignore-scripts` dentro de
cada repositório clonado (com cache — só reinstala se o `package.json`
daquele commit mudou desde a última vez). Sem isso, a config própria do
projeto costuma referenciar plugin (`eslint-plugin-react`,
`@typescript-eslint/*`) que não existe em disco, e falha ao carregar —
é o tipo de erro mais comum aqui, mais frequente que os de config do
checkstyle. `--ignore-scripts` é proposital: instalar dependência de
repositório de terceiro não-curado executando script arbitrário de
`postinstall` seria um risco de segurança real.

**Saída (qualquer linter):** `violations.jsonl` + arquivos arquivados em
`Babel/<linguagem>/<linter>/<repo>/<branch>/<commit>/<caminho_original>`

## ⚠️ Cuidado ao rerodar

`selected_repos.jsonl`, `commits.jsonl` e `violations.jsonl` são gravados em
modo **append** (soma ao que já existe, não substitui). Isso é o que permite
ir acumulando linguagens e linters diferentes num mesmo dataset — mas também
significa que rodar o **mesmo** comando duas vezes duplica os dados. Se
precisar rerodar algo do zero, apague o `.jsonl` correspondente antes (ou
filtre e remova só as linhas daquele linter/linguagem, já que cada linha tem
os campos `"linter"` e `"repo_full_name"`).

## Como as etapas se conectam

```
select_repositories.py <linguagem> <linter>  →  selected_repos.jsonl
                                                        │
extract_commit_history.py               →  commits.jsonl  (+ repos_cache/)
                                                        │
run_linters.py <linter>                 →  violations.jsonl  (+ Babel/)
```

## Adicionando um linter novo

Só mexe em `linter_adapters.py`: implementa uma classe nova seguindo a
mesma interface de `PylintAdapter`/`CheckstyleAdapter`/`ESLintAdapter`
(`get_version`, `has_own_config`, `is_adopted_remote`, `run`) e registra
ela no dicionário `ADAPTERS` no final do arquivo. `select_repositories.py`
e `run_linters.py` descobrem o linter novo automaticamente — não precisa
tocar em nenhum dos dois.

### Ordem das checagens por commit

O `git checkout` num clone parcial baixa blobs da rede — é o passo caro.
Por isso ele é o **último**: cada commit passa antes pelas checagens
baratas e só quem sobrevive paga o checkout.

1. `git diff-tree`: quais arquivos da linguagem o commit adicionou/mexeu
   (fora das pastas excluídas). Nenhum → marcado como processado, sem
   checkout.
2. `precheck` do adapter (opcional; hoje só o checkstyle). Só lê objetos
   do git (`git ls-tree` + no máximo dois blobs pequenos, **cacheados pelo
   id do blob** — o mesmo `pom.xml`/`checkstyle.xml` em milhares de commits
   é lido uma vez). Veredito **por commit**, na árvore daquele commit:

   | código | quando | efeito |
   |---|---|---|
   | `nao_maven` | sem `pom.xml` na raiz do commit | commit **excluído** |
   | `pom_config_ou_suppression` | o `maven-checkstyle-plugin` declara `configLocation`, `checkstyleRules`, `propertiesLocation`, `propertyExpansion` ou `suppressionsLocation/File/FileExpression` (no plugin, em `<execution>`, `pluginManagement` ou `reporting`), ou as properties `checkstyle.config.location`, `checkstyle.properties.location`, `checkstyle.suppressions.location/file` | commit **excluído** |
   | `config_variavel` | a config própria do commit tem `${...}` **fora de comentário XML** | commit **excluído** (não resolve `${config_loc}`/`${basedir}`, não troca por sun_checks) |
   | — | outras opções do plugin (`failsOnError`, `violationSeverity`, `includes`, `excludes`, `consoleOutput`...) | **lintado**; opções gravadas em `pom_checkstyle_options` |
   | — | nenhum dos 5 caminhos de config existe no commit | **lintado com `sun_checks`** (`ruleset=default`, `has_own_config=false`) |
   | — | config própria presente e sem variável | **lintado com a config do time** (jars do mais novo ao mais antigo; se nenhum carrega, cai pro default com `own_config_load_failed=true`) |

   Um commit pode ter mais de um motivo de exclusão; todos são gravados
   (`reasons`), o primeiro é o principal. Se há mais de uma config
   candidata no commit, vale a primeira da lista (`n_configs` registra
   quantas havia). Pom pai, perfis e properties herdadas **não** são
   resolvidos (mesma abordagem leve do Styler).
3. checkout + linter.

### Rastreabilidade: `--status-log`

Toda decisão vira uma linha em `commit_status.jsonl` (`--status-log`):
`status` = `linted` | `excluded` | `skipped` | `error`, `reason`/`reasons`
(códigos acima, mais `sem_arquivo_mudado` e `erro_execucao`), e, nos
lintados, `ruleset`, `ruleset_efetivo`, `config_path`, `pom_flags`,
`n_configs`, `own_config_load_failed`, `n_files`, `n_violations`.
Reexecução reavalia os excluídos (barato, sem checkout) e acrescenta nova
linha; vale a última. Resumo: `python3 analisar_status.py --status-log ...`.

## Limitações conhecidas

- **Commit raiz nunca é lintado**: `git diff-tree` sem `--root` não lista
  arquivos do primeiro commit de um repositório, que cai em "sem arquivo
  mudado". Só afeta quem tem o histórico inteiro dentro da janela.
- **Sem baseline**: só se linta o que cada commit toca (arquivo inteiro,
  não só as linhas alteradas); não há snapshot inicial da árvore.
- **Maven simplificado**: o `pom.xml` é lido sozinho (sem pom pai, perfis
  ou properties herdadas) — uma opção herdada do pai não é vista.
- **Config própria = 1 dos 5 caminhos fixos**; configs em outros caminhos
  contam como "sem config" (→ sun_checks).
- **Detecção de adoção é por presença de arquivo**, não por conteúdo —
  um `.pylintrc` vazio conta como "config própria" igual um robusto.
  Também não captura adoção via CI/workflow, script `lint` nomeado, ou
  dependência declarada sem config nenhuma — só o arquivo de config em
  si. Provavelmente **subestima** adotantes reais (falso negativo), não
  superestima.
- **Só analisa arquivos tocados por cada commit**, não a árvore inteira —
  eficiente e evita duplicar violação entre commits vizinhos, mas
  significa que o dataset não é uma foto completa do projeto em nenhum
  momento do tempo.
- **Config do repositório pode ser incompatível com a versão do linter
  instalada** (regra nova demais, DOCTYPE, plugin faltando) — o commit é
  pulado e contado em `erros` no resumo final, não derruba a execução.
