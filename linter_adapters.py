"""
linter_adapters.py

Cada linter suportado vira uma classe "adapter" com uma interface comum,
usada pelo run_linters.py generico. Pra adicionar um linter novo (pylint,
flake8, eslint, biome, pmd, spotbugs...):
  1. cria uma classe aqui implementando o contrato abaixo
  2. registra ela no dict ADAPTERS no final do arquivo
  3. nao precisa tocar em run_linters.py nem em common.py

Contrato que toda classe adapter precisa seguir:
  - atributos de classe: name (str), language (str, uma chave de
    common.LANGUAGE_EXTENSIONS)
  - add_cli_args(parser): classmethod, adiciona flags exclusivas desse
    linter ao subparser (ex: --checkstyle-jar)
  - __init__(self, args): recebe o namespace ja parseado do argparse
  - get_version(self) -> str
  - has_own_config(self, repo_dir: Path) -> bool
    checagem LOCAL (repo ja clonado), usada na etapa 3 por commit.
  - is_adopted_remote(cls, session, owner, repo, branch) -> bool
    classmethod, checagem REMOTA via API do GitHub (sem clonar), usada
    na etapa 1 pra pre-filtrar candidatos que nunca adotaram o linter.
    Mesma logica de deteccao de has_own_config, so que via API.
  - run(self, repo_dir: Path, files: list[str]) -> list[dict]
    cada dict ja normalizado, com estas chaves:
    file_path (RELATIVO ao repo_dir, use "/" como separador),
    line, column, rule_id, message, severity

Ganchos OPCIONAIS (o run_linters.py usa se existirem):
  - precheck(self, repo_dir, commit_hash, branch) -> Optional[dict]
    Roda DEPOIS de saber quais arquivos o commit mexeu e ANTES do
    checkout. So pode ler objetos do git (git ls-tree / cat-file / show),
    nunca a working tree -- e o ponto certo pra decidir sem pagar o custo
    do checkout (que, em clone parcial, baixa blobs da rede).
    Levanta CommitDiscarded(reasons, detalhe) pra EXCLUIR o commit (o
    run_linters grava os codigos de motivo no --status-log), ou
    RepoDiscarded pra pular o projeto inteiro. Se devolver um dict (o
    "plano" do commit), ele vai pro --status-log; a chave "extra" (dict)
    e copiada pra cada violacao gravada.
  - last_run_info (atributo, dict): preenchido por run(); copiado pro
    --status-log do commit (ex: own_config_load_failed).
  - is_build_supported_remote(cls, session, owner, repo, branch) -> bool
    classmethod, filtro de projeto via API do GitHub na etapa 1 (ex:
    checkstyle exige pom.xml na raiz) -- o repo fora nem chega a ser
    clonado.

Alguns linters precisam de passo extra antes de rodar de verdade (ex: o
ESLint so resolve plugin/parser da config propria do projeto se as
dependencias do package.json estiverem instaladas). Isso fica dentro do
run() do proprio adapter -- nao vira parte do contrato geral porque a
maioria dos linters (pylint, checkstyle) nao precisa disso.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests

from common import run_cmd

GITHUB_API = "https://api.github.com"


class RepoDiscarded(Exception):
    """O PROJETO inteiro foi descartado por uma regra do adapter (ex:
    checkstyle com property ${...} nao resolvida na config). Diferente
    de erro: e uma decisao esperada, nao uma falha. O run_linters.py
    registra uma vez e pula todo commit restante do repo, sem checkout e
    sem nenhuma chamada ao git."""


class CommitDiscarded(Exception):
    """So ESTE commit fica fora do dataset por uma regra do adapter (ex:
    pom.xml que troca a config do checkstyle, config com ${var}, projeto
    sem pom.xml). Decisao esperada, nao falha. `reasons` e a lista de
    codigos (strings curtas e estaveis, ex: "nao_maven"); o primeiro e o
    motivo principal. O run_linters.py grava tudo no --status-log, entao
    nada some em silencio. O commit NAO entra no --progress-log: a regra
    e barata de reavaliar e vale reavaliar se ela mudar."""

    def __init__(self, reasons, detail: str = ""):
        self.reasons: list[str] = [reasons] if isinstance(reasons, str) else list(reasons)
        self.reason: str = self.reasons[0]
        self.detail = detail
        super().__init__(detail or self.reason)


def _git_show_text(repo_dir: Path, ref: str, path: str) -> Optional[str]:
    """Conteudo de `path` em `ref` (commit, branch...) lido direto do
    banco de objetos do git -- NAO precisa de checkout nem de working
    tree. Em clone parcial, busca so esse blob na rede (um arquivo
    pequeno), nao o repositorio inteiro.

    None = o arquivo (ou o ref) simplesmente nao existe. Qualquer outro
    erro -- em especial o timeout do run_cmd, que tambem vira
    RuntimeError -- e RE-LEVANTADO: tratar "rede travada" como "arquivo
    nao existe" faria o caller seguir adiante como se o pom.xml nao
    tivesse override, silenciosamente."""
    try:
        return run_cmd(["git", "show", f"{ref}:{path}"], cwd=repo_dir).stdout
    except RuntimeError as exc:
        msg = str(exc)
        # "exists on disk, but not in <rev>": o git usa essa frase (em vez
        # de "does not exist") quando o arquivo existe na working tree
        # ATUAL mas nao no commit pedido -- caso comum aqui, porque o
        # clone fica no checkout do commit anterior.
        if any(s in msg for s in (
            "does not exist", "exists on disk, but not in", "invalid object name",
            "bad revision", "unknown revision", "Not a valid object name",
        )):
            return None
        raise


def _request_with_retry(
    session: requests.Session, url: str, params: Optional[dict] = None,
    max_retries: int = 3, raise_on_failure: bool = True,
) -> Optional[requests.Response]:
    """GET com espera automatica quando o rate limit da API do GitHub e
    atingido.

    raise_on_failure=True (padrao, usado pela etapa de busca/contribuidores
    no select_repositories.py): levanta excecao se falhar de vez -- correto
    pra uma chamada cujo fracasso invalida a execucao inteira.

    raise_on_failure=False (usado pelas checagens de adocao de linter
    abaixo): devolve None em vez de levantar -- um unico repositorio
    problematico nao pode derrubar a checagem dos outros 499."""
    for _ in range(max_retries):
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 403 and "rate limit" in response.text.lower():
            reset_at = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_seconds = max(reset_at - int(time.time()), 1)
            logging.warning("Rate limit atingido. Aguardando %ss...", wait_seconds)
            time.sleep(wait_seconds + 1)
            continue
        if raise_on_failure:
            response.raise_for_status()
        return response
    if raise_on_failure:
        raise RuntimeError(f"Falha ao acessar {url} apos {max_retries} tentativas")
    logging.error("Falha ao acessar %s apos %s tentativas (rate limit persistente)", url, max_retries)
    return None


_TREE_CACHE: dict[tuple[str, str, str], Optional[set[str]]] = {}


def _github_list_all_paths(session: requests.Session, owner: str, repo: str, branch: str) -> Optional[set[str]]:
    """Lista TODOS os caminhos de arquivo do repo (recursivo) numa unica
    chamada de API, usando a git trees API. Usado pra checar adocao de
    linter sem precisar de uma chamada por nome de arquivo candidato --
    com listas de ate 11 candidatos (caso do eslint), isso derruba de ate
    11 chamadas por repositorio pra 1.

    None se a chamada falhar (repo nao encontrado, rate limit persistente
    etc.) -- quem chama deve tratar como "nao sei" e cair pro fallback
    arquivo-por-arquivo, nao como "lista vazia".

    Resultado cacheado por (owner, repo, branch): a etapa 1 pergunta
    "adota o linter?" e "tem pom.xml?" pro mesmo repo, e sem cache seriam
    duas chamadas de API pra mesma arvore. So resultados bem-sucedidos
    ficam no cache (falha transitoria nao pode virar "nao sei" permanente)."""
    key = (owner, repo, branch)
    if key in _TREE_CACHE:
        return _TREE_CACHE[key]
    response = _request_with_retry(
        session, f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"}, raise_on_failure=False,
    )
    if response is None or response.status_code != 200:
        return None
    try:
        data = response.json()
        paths = {item["path"] for item in data.get("tree", []) if item.get("type") == "blob"}
    except (ValueError, KeyError):
        return None
    if len(_TREE_CACHE) > 200:
        _TREE_CACHE.clear()
    _TREE_CACHE[key] = paths
    return paths


def _github_file_exists(session: requests.Session, owner: str, repo: str, path: str, branch: str) -> bool:
    """Checa via API (sem clonar) se um arquivo existe naquele caminho, no
    HEAD da branch informada. Fallback usado so quando _github_list_all_paths
    nao esta disponivel -- prefira aquela pra checar varios candidatos."""
    response = _request_with_retry(
        session, f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", params={"ref": branch}, raise_on_failure=False,
    )
    return response is not None and response.status_code == 200


def _github_file_content(session: requests.Session, owner: str, repo: str, path: str, branch: str) -> Optional[str]:
    """Baixa o conteudo (decodificado) de um arquivo via API. None se nao existir."""
    response = _request_with_retry(
        session, f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", params={"ref": branch}, raise_on_failure=False,
    )
    if response is None or response.status_code != 200:
        return None
    data = response.json()
    if data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
    except (ValueError, KeyError):
        return None


class PylintAdapter:
    name = "pylint"
    language = "Python"

    # Pylint procura config nesta ordem (na pasta atual): pylintrc,
    # .pylintrc, pyproject.toml ([tool.pylint...]), setup.cfg ([pylint...])
    # e tox.ini ([pylint...]). Checamos os mesmos arquivos aqui.
    PYLINT_RC_FILENAMES = ["pylintrc", ".pylintrc", "pylintrc.toml", ".pylintrc.toml"]
    EMBEDDED_CONFIG_FILES = {
        "pyproject.toml": "[tool.pylint",
        "setup.cfg": "[pylint",
        "tox.ini": "[pylint",
    }
    RUN_TIMEOUT = 600  # segundos por commit (pylint e lento em arquivos grandes)

    @classmethod
    def add_cli_args(cls, parser):
        parser.add_argument("--pylint-bin", default="pylint", help="Caminho/nome do executavel pylint")

    def __init__(self, args):
        self.pylint_bin = args.pylint_bin

    def get_version(self) -> str:
        result = subprocess.run(
            [self.pylint_bin, "--version"], capture_output=True, encoding="utf-8", errors="replace",
        )
        # Saida: "pylint 3.3.1\nastroid 3.3.5\nPython 3.12.3 ..." -- guarda
        # pylint + astroid (a versao do astroid tambem afeta o resultado).
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        return ", ".join(lines[:2]) if lines else "unknown"

    def _find_own_config(self, repo_dir: Path) -> Optional[str]:
        for fname in self.PYLINT_RC_FILENAMES:
            if (repo_dir / fname).exists():
                return fname
        for fname, marker in self.EMBEDDED_CONFIG_FILES.items():
            path = repo_dir / fname
            if path.exists():
                try:
                    if marker in path.read_text(encoding="utf-8", errors="ignore"):
                        return fname
                except OSError:
                    pass
        return None

    def has_own_config(self, repo_dir: Path) -> bool:
        return self._find_own_config(repo_dir) is not None

    @classmethod
    def is_adopted_remote(cls, session: requests.Session, owner: str, repo: str, branch: str) -> bool:
        """Mesma checagem de has_own_config, mas via API do GitHub, sem
        precisar clonar -- usado na etapa 1 pra pre-filtrar candidatos que
        nunca adotaram o pylint, antes de gastar clone/checkout com eles.

        Prefere uma unica chamada (arvore completa do repo) a checar cada
        candidato individualmente -- ver _github_list_all_paths."""
        all_paths = _github_list_all_paths(session, owner, repo, branch)
        if all_paths is not None:
            if any(fname in all_paths for fname in cls.PYLINT_RC_FILENAMES):
                return True
            candidates = [f for f in cls.EMBEDDED_CONFIG_FILES if f in all_paths]
            if not candidates:
                return False
        else:
            # Fallback arquivo-por-arquivo, so se a chamada da arvore falhou
            for fname in cls.PYLINT_RC_FILENAMES:
                if _github_file_exists(session, owner, repo, fname, branch):
                    return True
            candidates = list(cls.EMBEDDED_CONFIG_FILES)
        for fname in candidates:
            content = _github_file_content(session, owner, repo, fname, branch)
            if content is not None and cls.EMBEDDED_CONFIG_FILES[fname] in content:
                return True
        return False

    def run(self, repo_dir: Path, files: list[str]) -> list[dict]:
        if not files:
            return []
        # --exit-zero: pylint sai com bitmask != 0 quando ha mensagens (nao
        # e falha de execucao). Sem --rcfile: deixa o proprio pylint
        # descobrir a config do repo a partir do cwd (mesma logica de
        # has_own_config); sem config propria, roda com o default dele.
        try:
            result = subprocess.run(
                [self.pylint_bin, "--output-format=json", "--exit-zero", *files],
                cwd=repo_dir, capture_output=True, encoding="utf-8", errors="replace",
                timeout=self.RUN_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Pylint excedeu {self.RUN_TIMEOUT}s em {repo_dir}")

        # Com --output-format=json o pylint sempre imprime pelo menos "[]"
        # quando termina normalmente. stdout vazio/invalido = nao terminou
        # (rcfile invalido, opcao desconhecida etc.) -- levanta excecao pra
        # virar "erro" no run_linters.py, em vez de parecer commit limpo.
        if not result.stdout.strip():
            raise RuntimeError(
                f"Pylint nao devolveu saida em {repo_dir} (returncode={result.returncode}): {result.stderr[:500]}"
            )
        try:
            raw_list = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Nao consegui parsear saida JSON do pylint em {repo_dir}: {exc}\nstdout: {result.stdout[:500]}"
            )
        return [self._normalize(raw, repo_dir) for raw in raw_list]

    def _normalize(self, raw: dict, repo_dir: Path) -> dict:
        filename_raw = raw.get("path")
        file_path = filename_raw
        if filename_raw:
            try:
                file_path = str(Path(filename_raw).resolve().relative_to(repo_dir.resolve()))
            except ValueError:
                pass
            file_path = file_path.replace("\\", "/")
        column = raw.get("column")
        return {
            "file_path": file_path,
            "rule_id": raw.get("message-id"),  # ex: C0114 (o nome legivel fica em "symbol")
            "line": raw.get("line"),
            # pylint conta coluna a partir de 0; checkstyle/eslint usam 1.
            "column": column + 1 if isinstance(column, int) else None,
            "message": raw.get("message"),
            # convention | refactor | warning | error | fatal
            "severity": raw.get("type", "warning"),
        }


class CheckstyleAdapter:
    name = "checkstyle"
    language = "Java"

    # Segue Loriot et al. 2022 (Styler): ignora arquivo dentro de pasta
    # com esses nomes, porque violacao de teste/recurso auxiliar nao e o
    # foco (queremos violacao no codigo principal). Comparacao e por
    # componente exato do caminho, nao substring -- ver
    # common.get_changed_files.
    EXCLUDE_DIRS = {"test", "resources"}

    OWN_CONFIG_CANDIDATES = [
        "checkstyle.xml",
        ".checkstyle.xml",
        "config/checkstyle/checkstyle.xml",
        "config/checkstyle/checkstyle_checks.xml",
        "checkstyle/checkstyle.xml",
    ]
    # Ruleset embutido no jar quando o repo nao tem config propria.
    # Alternativa: "/google_checks.xml" (tambem embutido no jar).
    DEFAULT_RULESET = "/sun_checks.xml"
    REPORT_FILENAME = "_checkstyle_report_tmp.xml"

    # Property estilo Maven (${algo}) na config propria: quem normalmente
    # a resolve e o plugin Maven/Gradle, nao o checkstyle -- rodando o jar
    # solto ela fica sem valor e a config nao carrega. Decisao do estudo
    # (inspirada em Loriot et al. 2022, Styler, que descarta o projeto):
    # aqui o commit e EXCLUIDO (motivo "config_variavel"), e NAO
    # substituimos por sun_checks nem tentamos resolver ${config_loc} /
    # ${basedir} -- senao misturariamos "regra do time" com "regra nossa"
    # sem o leitor saber. A checagem e por commit (a config muda ao longo
    # da historia) e ignora comentarios XML.
    UNRESOLVED_VARIABLE_RE = re.compile(r"\$\{[\w.\-]+\}")
    XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

    # Regra "S+" para o pom.xml (ver _analyze_pom_text). Nomes de opcao
    # do <configuration> do maven-checkstyle-plugin que apontam pra
    # supressao ou mudam QUAL config vale:
    POM_BLOCKING_OPTIONS = {
        "configLocation", "checkstyleRules", "propertiesLocation", "propertyExpansion",
        "suppressionsLocation", "suppressionsFile", "suppressionsFileExpression",
    }
    # Mesmas opcoes quando declaradas como property do Maven (<properties>).
    POM_BLOCKING_PROPERTIES = {
        "checkstyle.config.location", "checkstyle.properties.location",
        "checkstyle.suppressions.location", "checkstyle.suppressions.file",
    }

    @classmethod
    def add_cli_args(cls, parser):
        parser.add_argument(
            "--checkstyle-jar", required=True, action="append",
            help="Caminho para checkstyle-X.X.X-all.jar. Pode ser passado varias vezes "
                 "(--checkstyle-jar v1.jar --checkstyle-jar v2.jar) -- nesse caso, tenta "
                 "cada versao na config propria, da mais nova pra mais antiga, ate uma "
                 "funcionar (estrategia usada por Loriot et al. 2022, Styler, pra lidar "
                 "com incompatibilidade de versao sem precisar fixar uma unica).",
        )
        parser.add_argument(
            "--checkstyle-version", default=None,
            help="Sobrescreve a deteccao automatica da versao a partir do nome do jar. "
                 "So se aplica quando um UNICO --checkstyle-jar for passado.",
        )

    def __init__(self, args):
        jars_with_version = []
        for jar_arg in args.checkstyle_jar:
            jar_path = Path(jar_arg)
            if not jar_path.exists():
                raise FileNotFoundError(
                    f"Jar do checkstyle nao encontrado: {jar_path.resolve()} -- baixe em "
                    "https://github.com/checkstyle/checkstyle/releases/latest"
                )
            jars_with_version.append((jar_path, self._parse_version_from_filename(jar_path)))

        if len(jars_with_version) == 1 and args.checkstyle_version:
            jar_path, _ = jars_with_version[0]
            jars_with_version = [(jar_path, args.checkstyle_version)]

        # Ordena da versao mais nova pra mais antiga -- jar cujo nome nao
        # bate no padrao esperado (checkstyle-X.Y.Z-all.jar) fica por
        # ultimo (versao desconhecida = (-1,-1,-1), sempre "mais antiga").
        def version_key(item):
            _, version_str = item
            # Aceita X.Y.Z (padrao atual) e X.Y (releases mais antigas
            # do checkstyle, ex: 8.45) -- parte que faltar conta como 0.
            match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version_str)
            if not match:
                return (-1, -1, -1)
            major, minor, patch = match.groups()
            return (int(major), int(minor), int(patch) if patch else 0)

        jars_with_version.sort(key=version_key, reverse=True)
        self.jar_versions: list[tuple[Path, str]] = jars_with_version
        # jar_path/​_version_override mantidos por compatibilidade com
        # qualquer chamador externo que espere um unico jar (get_version
        # sem lista, etc.) -- sempre a versao mais nova (primeira da lista).
        self.jar_path = self.jar_versions[0][0]
        self._version_override = None
        # Caches por ID DE BLOB (o mesmo conteudo em mil commits = 1 leitura):
        # pom.xml -> resultado de _analyze_pom_text; config -> tem ${...}?
        self._pom_cache: dict[str, dict] = {}
        self._config_var_cache: dict[str, bool] = {}
        # Plano do commit atual (preenchido por precheck, usado por run).
        self._plan: Optional[dict] = None
        # Preenchido por run(): o que o run_linters grava no status-log.
        self.last_run_info: dict = {}

    @staticmethod
    def _parse_version_from_filename(jar_path: Path) -> str:
        # Releases oficiais seguem o padrao checkstyle-X.Y.Z-all.jar. Se
        # o jar foi renomeado, a ordenacao por versao nao funciona pra
        # ele (fica sempre por ultimo) -- renomeie de volta pro padrao
        # oficial, ou passe --checkstyle-version se for um unico jar.
        match = re.search(r"checkstyle-([\d.]+)-all\.jar", jar_path.name, re.IGNORECASE)
        return f"checkstyle {match.group(1)}" if match else "unknown"

    def get_version(self) -> str:
        if len(self.jar_versions) == 1:
            return self.jar_versions[0][1]
        versoes = ", ".join(v for _, v in self.jar_versions)
        return f"{self.jar_versions[0][1]} (com fallback pra: {versoes})"

    def has_own_config(self, repo_dir: Path) -> bool:
        return self._find_own_config(repo_dir) is not None

    def _find_own_config(self, repo_dir: Path) -> Optional[Path]:
        # Depois de um precheck() bem-sucedido, a config e a que o plano
        # do commit decidiu (lida da ARVORE DO COMMIT via git ls-tree) --
        # nao depende de nada que sobrou na working tree.
        if self._plan is not None:
            rel = self._plan.get("config_path")
            return (repo_dir / rel) if rel else None
        for rel in self.OWN_CONFIG_CANDIDATES:
            candidate = repo_dir / rel
            if candidate.exists():
                return candidate
        return None

    # ------------------------------------------------------------------
    # Regras de elegibilidade por commit (todas SEM checkout)
    # ------------------------------------------------------------------

    @staticmethod
    def _local_name(tag: str) -> str:
        """Nome da tag sem o namespace ({http://maven.apache.org/POM/4.0.0}x -> x)."""
        return tag.rsplit("}", 1)[-1]

    def _analyze_pom_text(self, pom_text: str) -> dict:
        """Classifica o que o pom.xml diz sobre o maven-checkstyle-plugin
        (regra "S+"). Nao tenta entender valores nem resolver pom pai /
        perfis / properties: so olha QUAIS opcoes foram declaradas.

        blocking: opcoes que apontam pra arquivo de supressao OU mudam
                  QUAL config vale -- o commit sai (o resultado nao
                  representaria a regra que o time de fato aplicou).
        flags:    qualquer outra opcao declarada (failsOnError,
                  violationSeverity, includes, excludes, consoleOutput...)
                  -- o commit e lintado normalmente e a opcao e so
                  registrada, pra quem quiser filtrar depois.
        parse_error: pom ilegivel pelo parser XML (tratado como sem opcoes).
        """
        info = {"blocking": [], "flags": [], "parse_error": False}
        try:
            root = ET.fromstring(pom_text)
        except ET.ParseError:
            info["parse_error"] = True
            return info

        blocking: set[str] = set()
        flags: set[str] = set()

        # 1) <configuration> do plugin (no plugin, em <execution>,
        #    pluginManagement ou reporting -- plugin.iter pega todos).
        for plugin in root.iter():
            if self._local_name(plugin.tag) != "plugin":
                continue
            artifact_id = next((c for c in plugin if self._local_name(c.tag) == "artifactId"), None)
            if artifact_id is None or (artifact_id.text or "").strip() != "maven-checkstyle-plugin":
                continue
            for conf in plugin.iter():
                if self._local_name(conf.tag) != "configuration":
                    continue
                for option in conf:
                    name = self._local_name(option.tag)
                    if name in self.POM_BLOCKING_OPTIONS:
                        blocking.add(name)
                    else:
                        flags.add(name)

        # 2) As mesmas opcoes podem vir como property do Maven
        #    (<properties><checkstyle.config.location>...). Mesmo efeito,
        #    mesma regra -- senao a mesma decisao teria dois resultados.
        for props in root.iter():
            if self._local_name(props.tag) != "properties":
                continue
            for prop in props:
                name = self._local_name(prop.tag)
                if name in self.POM_BLOCKING_PROPERTIES:
                    blocking.add(name)

        info["blocking"] = sorted(blocking)
        info["flags"] = sorted(flags)
        return info

    @classmethod
    def _config_text_has_variable(cls, config_text: str) -> bool:
        """True se a config tem ${...} FORA de comentario XML. Comentario
        e ignorado porque o checkstyle tambem o ignora: um ${x} citado num
        comentario nao quebra o carregamento (era o falso positivo que
        descartava o redisson inteiro)."""
        return bool(cls.UNRESOLVED_VARIABLE_RE.search(cls.XML_COMMENT_RE.sub("", config_text)))

    def _tree_blobs(self, repo_dir: Path, commit_hash: str, paths: list[str]) -> dict[str, str]:
        """{caminho: id do blob} dos `paths` que existem na ARVORE do
        commit (so raiz/caminhos exatos). Usa `git ls-tree`, que le so
        arvores -- em clone parcial nao baixa nenhum blob."""
        out = run_cmd(["git", "ls-tree", commit_hash, "--", *paths], cwd=repo_dir).stdout
        blobs: dict[str, str] = {}
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                blobs[path] = parts[2]
        return blobs

    def _blob_text(self, repo_dir: Path, blob_sha: str) -> str:
        # Qualquer falha (inclusive timeout) propaga: vira "erro" no
        # status-log, nunca "arquivo inexistente".
        return run_cmd(["git", "cat-file", "blob", blob_sha], cwd=repo_dir).stdout

    def precheck(self, repo_dir: Path, commit_hash: str, branch: str) -> dict:
        """Veredito do commit, ANTES do checkout (so arvores + no maximo
        dois blobs pequenos, ambos cacheados pelo id do blob: o mesmo
        pom.xml/checkstyle.xml em milhares de commits e lido uma vez).

        Ordem e codigos de exclusao (CommitDiscarded.reasons):
          nao_maven                 sem pom.xml na raiz DESTE commit
          pom_config_ou_suppression pom aponta supressao ou troca a config
          config_variavel           config propria com ${...} fora de comentario
        Sem exclusao, devolve o PLANO do commit:
          ruleset      "own" (config do time) ou "default" (sun_checks)
          config_path  qual arquivo (None se default)
          pom_flags    opcoes nao-bloqueantes do plugin no pom
          n_configs    quantos candidatos de config existem no commit
        """
        self._plan = None
        blobs = self._tree_blobs(repo_dir, commit_hash, ["pom.xml", *self.OWN_CONFIG_CANDIDATES])

        if "pom.xml" not in blobs:
            raise CommitDiscarded("nao_maven", "sem pom.xml na raiz do commit")

        reasons: list[str] = []
        details: list[str] = []

        pom_sha = blobs["pom.xml"]
        if pom_sha not in self._pom_cache:
            self._pom_cache[pom_sha] = self._analyze_pom_text(self._blob_text(repo_dir, pom_sha))
        pom_info = self._pom_cache[pom_sha]
        if pom_info["blocking"]:
            reasons.append("pom_config_ou_suppression")
            details.append("pom: " + ", ".join(pom_info["blocking"]))

        found = [p for p in self.OWN_CONFIG_CANDIDATES if p in blobs]
        config_path = found[0] if found else None
        if config_path is not None:
            cfg_sha = blobs[config_path]
            if cfg_sha not in self._config_var_cache:
                self._config_var_cache[cfg_sha] = self._config_text_has_variable(
                    self._blob_text(repo_dir, cfg_sha)
                )
            if self._config_var_cache[cfg_sha]:
                reasons.append("config_variavel")
                details.append(f"{config_path} tem ${{...}}")

        if reasons:
            raise CommitDiscarded(reasons, "; ".join(details))

        plan = {
            "ruleset": "own" if config_path else "default",
            "config_path": config_path,
            "pom_flags": pom_info["flags"],
            "pom_parse_error": pom_info["parse_error"],
            "n_configs": len(found),
            # campos que o run_linters copia pra cada violacao gravada
            "extra": {"pom_checkstyle_options": pom_info["flags"]},
        }
        self._plan = plan
        return plan

    @classmethod
    def is_adopted_remote(cls, session: requests.Session, owner: str, repo: str, branch: str) -> bool:
        """Mesma checagem de has_own_config, mas via API do GitHub, sem
        precisar clonar -- usado na etapa 1 pra pre-filtrar candidatos que
        nunca adotaram o checkstyle, antes de gastar clone/checkout.

        Prefere uma unica chamada (arvore completa do repo) a checar cada
        candidato individualmente -- ver _github_list_all_paths."""
        all_paths = _github_list_all_paths(session, owner, repo, branch)
        if all_paths is not None:
            return any(path in all_paths for path in cls.OWN_CONFIG_CANDIDATES)
        # Fallback arquivo-por-arquivo, so se a chamada da arvore falhou
        return any(
            _github_file_exists(session, owner, repo, path, branch)
            for path in cls.OWN_CONFIG_CANDIDATES
        )

    @classmethod
    def is_build_supported_remote(cls, session: requests.Session, owner: str, repo: str, branch: str) -> bool:
        """Etapa 1: so Maven (pom.xml na raiz, no HEAD). Gradle e outros
        ficam de fora -- decisao do estudo, igual ao Styler: o Maven
        permite saber, so lendo o pom.xml, se a config do checkstyle foi
        alterada; no Gradle isso exigiria interpretar Groovy/Kotlin.

        A regra por commit (precheck) revalida: um repo pode ter virado
        Maven (ou deixado de ser) ao longo da historia.

        True tambem quando a API falha ("nao sei" nao descarta)."""
        all_paths = _github_list_all_paths(session, owner, repo, branch)
        if all_paths is None:
            return True
        return "pom.xml" in all_paths

    def run(self, repo_dir: Path, files: list[str]) -> list[dict]:
        if not files:
            return []

        own_config = self._find_own_config(repo_dir)
        self.last_run_info = {
            "own_config_load_failed": False, "linter_version": None,
            # ruleset que de fato rodou (difere do plano so se a config
            # propria nao carregou em nenhum jar e caiu pro default)
            "ruleset_efetivo": "own" if own_config else "default",
        }

        if own_config:
            # Estrategia de multiplas versoes (Loriot et al. 2022, Styler):
            # tenta a config propria com cada jar disponivel, da versao
            # mais nova pra mais antiga, ate uma funcionar -- cobre
            # incompatibilidade tipo regra/property removida numa versao
            # mais nova (config com ${var} nem chega aqui: foi excluida
            # no precheck).
            for jar_path, version_str in self.jar_versions:
                violations, succeeded, _ = self._invoke(repo_dir, files, str(own_config), jar_path)
                if succeeded:
                    for v in violations:
                        v["linter_version"] = version_str
                    self.last_run_info["linter_version"] = version_str
                    return violations
                logging.warning(
                    "Config propria de %s nao rodou com %s -- tentando outra versao (se houver)",
                    repo_dir, version_str,
                )

            # Esgotou todas as versoes, por outro motivo que nao variavel
            # nao resolvida -- cai pro default em vez de perder o commit.
            newest_jar, newest_version = self.jar_versions[0]
            logging.warning(
                "Config propria de %s falhou em todas as %d versao(oes) disponivel(is) "
                "-- tentando com ruleset default (%s)",
                repo_dir, len(self.jar_versions), self.DEFAULT_RULESET,
            )
            violations, _, _ = self._invoke(repo_dir, files, self.DEFAULT_RULESET, newest_jar)
            # Marcadas como NAO vindas de config propria (equivalem a "rodou
            # no default"), com sinalizador extra pra distinguir de "nunca
            # teve config nenhuma" -- sao coisas diferentes pra analise.
            for v in violations:
                v["has_own_config"] = False
                v["own_config_load_failed"] = True
                v["linter_version"] = newest_version
            self.last_run_info = {
                "own_config_load_failed": True, "linter_version": newest_version, "ruleset_efetivo": "default",
            }
            return violations

        newest_jar, newest_version = self.jar_versions[0]
        violations, _, _ = self._invoke(repo_dir, files, self.DEFAULT_RULESET, newest_jar)
        for v in violations:
            v["linter_version"] = newest_version
        self.last_run_info["linter_version"] = newest_version
        return violations

    def _invoke(
        self, repo_dir: Path, files: list[str], config: str, jar_path: Path,
    ) -> tuple[list[dict], bool, str]:
        """Roda o checkstyle com uma config e um jar especificos. Devolve
        (violacoes, sucesso, stderr) -- sucesso=False sempre que a lista
        vem vazia POR FALHA (relatorio ausente/vazio/malformado), nao
        porque o codigo estava genuinamente limpo (nesse caso a lista
        tambem vem vazia, mas o caller nao tem como -- nem precisa --
        diferenciar, ja que so importa saber se DEVE tentar fallback)."""
        report_path = repo_dir / self.REPORT_FILENAME
        args = ["java", "-jar", str(jar_path), "-c", config, "-f", "xml", "-o", str(report_path), *files]
        # Checkstyle sai com codigo != 0 quando ha violacoes de severidade
        # "error" (comportamento padrao) -- nao e falha de execucao, entao
        # nao exigimos codigo 0 aqui. So tratamos como erro real se o
        # relatorio nem foi gerado, ou foi gerado mas ficou vazio (0
        # bytes) -- os dois casos indicam que o checkstyle crashou/parou
        # no meio do arquivo em vez de terminar normalmente, e o unico
        # jeito de saber PORQUE e olhar o stderr dele, que ate agora a
        # gente descartava sempre que o arquivo existia (mesmo vazio).
        result = subprocess.run(args, cwd=repo_dir, capture_output=True, encoding="utf-8", errors="replace")

        if not report_path.exists():
            logging.error(
                "Checkstyle nao gerou relatorio em %s (jar=%s, config=%s, returncode=%s)\nstderr: %s",
                repo_dir, jar_path.name, config, result.returncode, result.stderr,
            )
            return [], False, result.stderr

        if report_path.stat().st_size == 0:
            logging.error(
                "Checkstyle gerou relatorio VAZIO (0 bytes) em %s pros arquivos %s "
                "(jar=%s, config=%s, returncode=%s) -- provavel crash no meio do processamento.\nstderr: %s",
                repo_dir, files, jar_path.name, config, result.returncode, result.stderr,
            )
            report_path.unlink(missing_ok=True)
            return [], False, result.stderr

        violations = self._parse_xml(report_path, repo_dir)
        report_path.unlink(missing_ok=True)
        return violations, True, result.stderr

    def _parse_xml(self, report_path: Path, repo_dir: Path) -> list[dict]:
        try:
            root = ET.parse(report_path).getroot()
        except ET.ParseError as exc:
            # exc ja vem com "linha X, coluna Y" do proprio parser -- isso
            # e o dado que faltava pra diagnosticar de verdade (antes so
            # logavamos o caminho, sem dizer ONDE nem PORQUE quebrou).
            # Le com errors="replace" (nao "strict") justamente porque a
            # causa mais provavel disso e byte invalido pra UTF-8 dentro
            # do arquivo -- ler estrito ia so trocar um erro por outro.
            try:
                raw = report_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = "(nao foi possivel reler o arquivo pra diagnostico)"
            logging.error(
                "Relatorio XML do checkstyle malformado em %s\n"
                "Erro do parser: %s\n"
                "Primeiros 2000 caracteres do arquivo (bytes invalidos viram '\ufffd'):\n%s",
                report_path, exc, raw[:2000],
            )
            return []

        violations = []
        for file_elem in root.findall("file"):
            filename_raw = file_elem.get("name")
            file_path = filename_raw
            if filename_raw:
                try:
                    file_path = str(Path(filename_raw).resolve().relative_to(repo_dir.resolve()))
                    file_path = file_path.replace("\\", "/")
                except ValueError:
                    pass

            for error_elem in file_elem.findall("error"):
                line_raw = error_elem.get("line")
                column_raw = error_elem.get("column")
                violations.append({
                    "file_path": file_path,
                    "line": int(line_raw) if line_raw else None,
                    "column": int(column_raw) if column_raw else None,
                    "rule_id": error_elem.get("source"),
                    "message": error_elem.get("message"),
                    "severity": error_elem.get("severity", "error"),
                })
        return violations


class _ConfigLoadFailure(Exception):
    """O eslint nao conseguiu NEM CARREGAR a config (plugin/parser/preset
    ausente ou incompativel, config invalida): stdout vazio ou nao-JSON.
    Diferente de "rodou e achou violacao" (exit 1, JSON valido). Quem
    chama decide: com config propria cai no default; no default vira erro."""


def _run_in_group(args: list[str], cwd: Path, timeout: int, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Como subprocess.run(capture_output=True, timeout=...), mas MATA O
    GRUPO DE PROCESSOS inteiro no timeout. O subprocess.run so mata o
    filho direto; o `npm` dispara processos netos (node, scripts) que
    continuam segurando os pipes de stdout/stderr, e o communicate() que
    o subprocess.run faz depois do kill fica esperando eles pra sempre --
    foi o que parou o job do eslint por ~9h. Aqui o filho vira lider de
    uma sessao nova (start_new_session) e o killpg derruba todos juntos.
    Levanta subprocess.TimeoutExpired, igual ao subprocess.run."""
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # lider da sessao: pid == pgid
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # Algum neto escapou do grupo e ainda segura o pipe: desiste de
            # ler em vez de travar o job.
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _git_tree_blobs(repo_dir: Path, commit_hash: str, paths: list[str]) -> dict[str, str]:
    """{caminho: id do blob} dos `paths` que existem na ARVORE do commit.
    `git ls-tree` le so arvores: em clone parcial nao baixa blob nenhum."""
    out = run_cmd(["git", "ls-tree", commit_hash, "--", *paths], cwd=repo_dir).stdout
    blobs: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


class ESLintAdapter:
    name = "eslint"
    language = "JavaScript"  # so .js/.jsx/.mjs/.cjs, ver common.LANGUAGE_EXTENSIONS

    # Config "legado" (ESLint <9) e config "flat" (ESLint 9+, o formato
    # novo) coexistem no mundo real -- um repo minerado ao longo da
    # historia pode ter usado um formato num commit antigo e outro num
    # commit recente. Checamos os dois.
    LEGACY_CONFIG_FILENAMES = [
        ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs",
        ".eslintrc.json", ".eslintrc.yaml", ".eslintrc.yml", ".eslintrc",
    ]
    FLAT_CONFIG_FILENAMES = [
        "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    ]

    SEVERITY_MAP = {0: "off", 1: "warning", 2: "error"}

    # Mesma regra dos outros linters: nao lintar arquivo de teste (violacao
    # de teste nao e o foco). Pastas por componente exato do caminho, como
    # no checkstyle (test, resources) + as variantes de teste comuns em JS.
    # EXCLUDE_SUFFIXES cobre o teste colocalizado ao lado do codigo
    # (foo.test.js, foo.spec.js), que em JS nao tem pasta propria.
    EXCLUDE_DIRS = {"test", "tests", "__tests__", "resources"}
    EXCLUDE_SUFFIXES = (".test.js", ".spec.js", ".test.jsx", ".spec.jsx", ".test.mjs", ".spec.mjs", ".test.cjs", ".spec.cjs")

    RUN_TIMEOUT = 600  # segundos por commit
    # O eslint avisa (ruleId null) quando um arquivo explicitamente pedido
    # cai num padrao de ignore do PROPRIO projeto (.eslintignore / ignores
    # da flat config). Nao e violacao de regra: gravamos como linha propria,
    # com rule_id "(eslint ignored)" e severity "ignored", filtravel.
    IGNORED_PREFIX = "File ignored"
    IGNORED_RULE_ID = "(eslint ignored)"
    # Dependencias que nao existem no registro npm (pacote local do monorepo
    # etc.) quebram o `npm install` inteiro; saem do package.json instalado.
    UNINSTALLABLE_PROTOCOLS = ("workspace:", "file:", "link:", "portal:")
    DEPS_MARKER = ".babel_deps_ok"

    @classmethod
    def add_cli_args(cls, parser):
        parser.add_argument("--eslint-bin", default="eslint", help="Nome/caminho do executavel eslint (instalado globalmente)")
        parser.add_argument(
            "--eslint-version", default=None,
            help="Sobrescreve a deteccao automatica da versao (a partir de 'eslint --version')",
        )
        parser.add_argument(
            "--skip-npm-install", action="store_true",
            help="Nao instala dependencia nenhuma (mais rapido, mas a config propria do projeto "
                 "quase sempre falha ao carregar e cai no default marcado own_config_load_failed)",
        )
        parser.add_argument(
            "--deps-dir", default=None,
            help="Onde ficam as dependencias instaladas, UMA pasta por repositorio (padrao: "
                 "'eslint_deps' ao lado de --repos-dir). Use um disco local (ex: $TMPDIR) se o NFS "
                 "da home for lento; se a pasta sumir, reinstala sozinho.",
        )
        parser.add_argument(
            "--install-timeout", type=int, default=900,
            help="Segundos maximos pra instalar as dependencias de UM repositorio (a instalacao "
                 "roda uma vez por repositorio, nao por commit). Padrao: 900.",
        )

    def __init__(self, args):
        self.eslint_bin = args.eslint_bin
        self._version_override = args.eslint_version
        self.skip_install = args.skip_npm_install
        deps_dir = getattr(args, "deps_dir", None)
        # SEMPRE absoluto: o node_modules do repo vira um symlink pra ca, e
        # symlink relativo e resolvido a partir da pasta do symlink (o
        # repo), nao do cwd -- um --deps-dir relativo deixava o link quebrado.
        self.deps_dir = (Path(deps_dir) if deps_dir else Path(getattr(args, "repos_dir", "repos_cache")).resolve().parent / "eslint_deps").resolve()
        self.install_timeout = getattr(args, "install_timeout", 900)
        # Estado da instalacao POR REPOSITORIO (chave: str(repo_dir)):
        # {"status": "ok"|"failed"|"none", "target": Path, "detail": str}.
        # Decidido uma vez por execucao -- falha NAO e repetida a cada commit.
        self._deps_state: dict[str, dict] = {}
        self._pkg_cache: dict[str, bool] = {}   # blob do package.json -> tem "eslintConfig"?
        self._plan: Optional[dict] = None       # plano do commit atual (precheck -> run)
        self._branch: Optional[str] = None
        self._version_cache: Optional[str] = None
        self.last_run_info: dict = {}

    def get_version(self) -> str:
        if self._version_override:
            return self._version_override
        if self._version_cache is None:
            result = subprocess.run(
                [self.eslint_bin, "--version"], capture_output=True, encoding="utf-8", errors="replace",
            )
            self._version_cache = result.stdout.strip() or "unknown"
        return self._version_cache

    def _find_own_config(self, repo_dir: Path) -> Optional[str]:
        # Depois de um precheck(), vale a config que o plano decidiu pela
        # ARVORE DO COMMIT (git), nao o que sobrou na working tree.
        if self._plan is not None:
            return self._plan.get("config_path")
        for fname in self.FLAT_CONFIG_FILENAMES + self.LEGACY_CONFIG_FILENAMES:
            if (repo_dir / fname).exists():
                return fname
        package_json = repo_dir / "package.json"
        if package_json.exists():
            try:
                if '"eslintConfig"' in package_json.read_text(encoding="utf-8", errors="ignore"):
                    return "package.json"
            except OSError:
                pass
        return None

    def has_own_config(self, repo_dir: Path) -> bool:
        return self._find_own_config(repo_dir) is not None

    @classmethod
    def is_adopted_remote(cls, session: requests.Session, owner: str, repo: str, branch: str) -> bool:
        """Prefere uma unica chamada (arvore completa do repo) a checar
        os 11 candidatos individualmente -- ver _github_list_all_paths.
        Essa e a checagem mais cara das tres (mais nomes candidatos), a
        que mais se beneficia dessa otimizacao."""
        all_paths = _github_list_all_paths(session, owner, repo, branch)
        candidates = cls.FLAT_CONFIG_FILENAMES + cls.LEGACY_CONFIG_FILENAMES
        if all_paths is not None:
            if any(fname in all_paths for fname in candidates):
                return True
            if "package.json" not in all_paths:
                return False
        else:
            # Fallback arquivo-por-arquivo, so se a chamada da arvore falhou
            for fname in candidates:
                if _github_file_exists(session, owner, repo, fname, branch):
                    return True
        content = _github_file_content(session, owner, repo, "package.json", branch)
        return content is not None and '"eslintConfig"' in content

    @classmethod
    def is_build_supported_remote(cls, session: requests.Session, owner: str, repo: str, branch: str) -> bool:
        """Etapa 1: so projeto Node (package.json na raiz, no HEAD) -- o
        analogo do "so Maven" do checkstyle. "Nao sei" (API falhou) nao
        descarta; a regra por commit (precheck) revalida."""
        all_paths = _github_list_all_paths(session, owner, repo, branch)
        if all_paths is None:
            return True
        return "package.json" in all_paths

    # ------------------------------------------------------------------
    # Veredito por commit (sem checkout)
    # ------------------------------------------------------------------

    def precheck(self, repo_dir: Path, commit_hash: str, branch: str) -> dict:
        """Codigos de exclusao (CommitDiscarded.reasons):
          nao_node   sem package.json na raiz DESTE commit
        Sem exclusao devolve o plano:
          ruleset      "own" (config do time) ou "default" (eslint:recommended)
          config_path  nome do arquivo (ou "package.json" p/ eslintConfig); None se default
          config_kind  "flat" | "legacy" | "package" | None
          n_configs    quantos arquivos de config existem no commit
        Se a config nao CARREGAR depois (plugin ausente...), o run() cai no
        default e marca own_config_load_failed -- isso nao e exclusao."""
        self._plan = None
        self._branch = branch
        blobs = _git_tree_blobs(
            repo_dir, commit_hash,
            ["package.json", *self.FLAT_CONFIG_FILENAMES, *self.LEGACY_CONFIG_FILENAMES],
        )
        if "package.json" not in blobs:
            raise CommitDiscarded("nao_node", "sem package.json na raiz do commit")

        found = [p for p in self.FLAT_CONFIG_FILENAMES + self.LEGACY_CONFIG_FILENAMES if p in blobs]
        config_path = found[0] if found else None
        kind = None
        if config_path is not None:
            kind = "flat" if config_path in self.FLAT_CONFIG_FILENAMES else "legacy"
        else:
            sha = blobs["package.json"]
            if sha not in self._pkg_cache:
                text = run_cmd(["git", "cat-file", "blob", sha], cwd=repo_dir).stdout
                self._pkg_cache[sha] = '"eslintConfig"' in text
            if self._pkg_cache[sha]:
                config_path, kind = "package.json", "package"

        plan = {
            "ruleset": "own" if config_path else "default",
            "config_path": config_path,
            "config_kind": kind,
            "n_configs": len(found) + (1 if kind == "package" else 0),
            "extra": {},
        }
        self._plan = plan
        return plan

    # ------------------------------------------------------------------
    # Dependencias: UMA instalacao por repositorio, nas versoes da PONTA
    # ------------------------------------------------------------------

    def _sanitize_package_json(self, text: str) -> Optional[str]:
        """package.json que sera instalado: sem scripts, sem workspaces e
        sem dependencia que so existe localmente (workspace:/file:/...)."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        data.pop("scripts", None)
        data.pop("workspaces", None)
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps = data.get(key)
            if isinstance(deps, dict):
                data[key] = {
                    name: ver for name, ver in deps.items()
                    if not (isinstance(ver, str) and ver.startswith(self.UNINSTALLABLE_PROTOCOLS))
                }
        return json.dumps(data, indent=2)

    def _install(self, deps_root: Path, package_json_text: str, lock_text: Optional[str]) -> tuple[bool, str]:
        """Instala as dependencias em `deps_root` (fora da working tree do
        repo). SEGURANCA: --ignore-scripts, pra nao rodar codigo de
        install de pacote de terceiro. O timeout mata o grupo de
        processos inteiro (ver _run_in_group)."""
        shutil.rmtree(deps_root, ignore_errors=True)
        deps_root.mkdir(parents=True, exist_ok=True)
        sanitized = self._sanitize_package_json(package_json_text)
        if sanitized is None:
            return False, "package.json invalido"
        (deps_root / "package.json").write_text(sanitized, encoding="utf-8")
        if lock_text:
            (deps_root / "package-lock.json").write_text(lock_text, encoding="utf-8")
        cmd = ["npm", "install", "--no-audit", "--no-fund", "--ignore-scripts", "--legacy-peer-deps", "--prefer-offline"]
        try:
            result = _run_in_group(cmd, deps_root, self.install_timeout)
        except subprocess.TimeoutExpired:
            return False, f"npm install excedeu {self.install_timeout}s"
        if result.returncode != 0:
            return False, result.stderr[-400:]
        # Projeto sem dependencia nenhuma: o npm nao cria node_modules.
        (deps_root / "node_modules").mkdir(exist_ok=True)
        (deps_root / self.DEPS_MARKER).write_text("ok", encoding="utf-8")
        return True, "ok"

    def _ensure_tip_deps(self, repo_dir: Path) -> dict:
        key = str(repo_dir)
        if key in self._deps_state:
            return self._deps_state[key]
        deps_root = self.deps_dir / repo_dir.name
        target = deps_root / "node_modules"
        if (deps_root / self.DEPS_MARKER).exists() and target.exists():
            state = {"status": "ok", "target": target, "detail": "reaproveitado de execucao anterior"}
        else:
            ref = f"origin/{self._branch}"
            pkg_text = _git_show_text(repo_dir, ref, "package.json")
            if pkg_text is None:
                state = {"status": "none", "target": target, "detail": f"sem package.json em {ref}"}
            else:
                lock_text = _git_show_text(repo_dir, ref, "package-lock.json")
                logging.info("%s: instalando dependencias da ponta (%s), uma vez por repositorio...", repo_dir.name, ref)
                ok, detail = self._install(deps_root, pkg_text, lock_text)
                state = {"status": "ok" if ok else "failed", "target": target, "detail": detail}
                if not ok:
                    logging.warning(
                        "%s: instalacao das dependencias falhou -- NAO sera repetida neste repositorio; "
                        "commits cuja config precisar de plugin caem no default (own_config_load_failed). %s",
                        repo_dir.name, detail,
                    )
        self._deps_state[key] = state
        return state

    def _prepare_deps(self, repo_dir: Path) -> str:
        """Deixa repo_dir/node_modules apontando pra instalacao da ponta.
        Devolve a origem: "tip" (instalacao da ponta), "repo" (o proprio
        commit versiona node_modules), "none" (sem dependencias)."""
        state = {"status": "none"} if self.skip_install else self._ensure_tip_deps(repo_dir)
        nm = repo_dir / "node_modules"
        tracked = bool(run_cmd(["git", "ls-tree", "HEAD", "--", "node_modules"], cwd=repo_dir).stdout.strip())
        if tracked:
            return "repo"
        if nm.is_symlink():
            if state["status"] == "ok" and os.readlink(nm) == str(state["target"]):
                return "tip"
            nm.unlink()
        elif nm.exists():
            # Sobra de instalacao antiga dentro do clone (rodada anterior):
            # remove, pra o resultado depender so desta execucao.
            logging.info("Removendo node_modules antigo de %s", repo_dir.name)
            shutil.rmtree(nm, ignore_errors=True)
        if state["status"] == "ok":
            nm.symlink_to(state["target"], target_is_directory=True)
            if not nm.exists():  # link criado mas destino inexistente
                nm.unlink()
                raise RuntimeError(f"node_modules de {repo_dir.name} apontaria pra {state['target']}, que nao existe")
            return "tip"
        return "none"

    # ------------------------------------------------------------------
    # Execucao
    # ------------------------------------------------------------------

    # Ruleset default, gerado uma vez e reaproveitado -- equivalente ao
    # sun_checks.xml do checkstyle, so que pro ESLint precisa ser
    # construido (nao vem embutido como arquivo pronto na instalacao).
    # "eslint:recommended" e resolvido pelo proprio nucleo do ESLint, sem
    # precisar de pacote externo instalado -- por isso usamos formato
    # legado aqui (funciona com so uma string, nao precisa de import).
    DEFAULT_CONFIG_FILENAME = "_babel_eslint_default_config.json"
    DEFAULT_CONFIG_CONTENT = json.dumps({
        "extends": "eslint:recommended",
        "parserOptions": {"ecmaVersion": "latest", "sourceType": "script"},
        "env": {"browser": True, "node": True, "es2021": True},
    })

    def _invoke(self, repo_dir: Path, files: list[str], config_name: Optional[str]) -> list[dict]:
        """Roda o eslint com a config propria (config_name) ou com o
        default (None). Levanta _ConfigLoadFailure quando o eslint nem
        chegou a rodar (stdout vazio/nao-JSON); RuntimeError em timeout."""
        # O ESLint teve uma mudanca de formato de config real e disruptiva:
        # legado (.eslintrc.*) vs "flat" (eslint.config.*). Ate a v9, dava
        # pra escolher qual usar via ESLINT_USE_FLAT_CONFIG; a v10 REMOVEU
        # o suporte a config legado -- use eslint@9 (versao do cluster).
        env = os.environ.copy()
        config_args: list[str] = []
        if config_name in self.FLAT_CONFIG_FILENAMES:
            env["ESLINT_USE_FLAT_CONFIG"] = "true"
        elif config_name is not None:  # legado, incluindo eslintConfig no package.json
            env["ESLINT_USE_FLAT_CONFIG"] = "false"
        else:
            default_config_path = repo_dir / self.DEFAULT_CONFIG_FILENAME
            if not default_config_path.exists():
                default_config_path.write_text(self.DEFAULT_CONFIG_CONTENT, encoding="utf-8")
            # --no-eslintrc: sem isso, o ESLint legado SOMA o -c com os
            # .eslintrc*/eslintConfig do projeto, e o "default" deixava de
            # ser eslint:recommended puro (e falhava se o preset do time
            # nao estivesse instalado).
            config_args = ["--no-eslintrc", "-c", str(default_config_path)]
            env["ESLINT_USE_FLAT_CONFIG"] = "false"

        try:
            result = _run_in_group(
                [self.eslint_bin, "--format", "json", *config_args, *files],
                cwd=repo_dir, timeout=self.RUN_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Eslint excedeu {self.RUN_TIMEOUT}s em {repo_dir}")

        # eslint sai com codigo 1 quando ha violacao (nao e falha) e 2 em
        # erro real. Com --format json ele SEMPRE imprime pelo menos "[]"
        # quando termina de rodar; stdout vazio/nao-parseavel = nunca
        # rodou (config nao carregou, plugin faltando...).
        if not result.stdout.strip():
            err = " ".join(result.stderr.split())  # uma linha so, pra nao quebrar o log
            raise _ConfigLoadFailure(f"sem saida (returncode={result.returncode}): {err[:500]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise _ConfigLoadFailure(f"saida nao-JSON ({exc}): {result.stdout[:300]}")

    def run(self, repo_dir: Path, files: list[str]) -> list[dict]:
        if not files:
            return []

        own_config = self._find_own_config(repo_dir)
        deps_source = self._prepare_deps(repo_dir)
        self.last_run_info = {
            "own_config_load_failed": False,
            "ruleset_efetivo": "own" if own_config else "default",
            "deps_source": deps_source,
            # copiado pelo run_linters pra CADA violacao gravada
            "record_extra": {"deps_source": deps_source},
        }

        raw_files = None
        fell_back = False
        if own_config:
            try:
                raw_files = self._invoke(repo_dir, files, own_config)
            except _ConfigLoadFailure as exc:
                # Mesma politica do checkstyle: config propria que nao
                # carrega nao perde o commit -- roda no default (eslint:
                # recommended) e deixa MARCADO, pra analise separar.
                logging.warning(
                    "Config propria (%s) de %s nao carregou -- rodando com eslint:recommended: %s",
                    own_config, repo_dir.name, str(exc)[:300],
                )
                fell_back = True
                self.last_run_info["own_config_load_failed"] = True
                self.last_run_info["ruleset_efetivo"] = "default"
        if raw_files is None:
            try:
                raw_files = self._invoke(repo_dir, files, None)
            except _ConfigLoadFailure as exc:
                raise RuntimeError(f"Eslint nao rodou nem com o default em {repo_dir}: {exc}")

        violations: list[dict] = []
        for raw_file in raw_files:
            violations.extend(self._normalize_file(raw_file, repo_dir))
        if fell_back:
            for v in violations:
                v["has_own_config"] = False
                v["own_config_load_failed"] = True
        return violations

    def _normalize_file(self, raw_file: dict, repo_dir: Path) -> list[dict]:
        filename_raw = raw_file.get("filePath")
        file_path = filename_raw
        if filename_raw:
            try:
                file_path = str(Path(filename_raw).resolve().relative_to(repo_dir.resolve()))
                file_path = file_path.replace("\\", "/")
            except ValueError:
                pass

        out = []
        for msg in raw_file.get("messages", []):
            rule_id = msg.get("ruleId")  # None p/ erro de parse sem regra especifica
            severity = self.SEVERITY_MAP.get(msg.get("severity"), "unknown")
            message = msg.get("message")
            if rule_id is None and isinstance(message, str) and message.startswith(self.IGNORED_PREFIX):
                rule_id, severity = self.IGNORED_RULE_ID, "ignored"
            out.append({
                "file_path": file_path,
                "line": msg.get("line"),
                "column": msg.get("column"),
                "rule_id": rule_id,
                "message": message,
                "severity": severity,
            })
        return out


# Registro de linters suportados. Adicione a classe nova aqui quando
# implementar o proximo -- o run_linters.py descobre o subcomando sozinho.
ADAPTERS = {
    PylintAdapter.name: PylintAdapter,
    CheckstyleAdapter.name: CheckstyleAdapter,
    ESLintAdapter.name: ESLintAdapter,
}
