"""
common.py

Funcoes compartilhadas entre os scripts de linter da etapa 3 do pipeline
"Babel" (run_linters_pylint.py, run_linters_checkstyle.py, e os que vierem
depois). Extraido do run_linters.py original quando o Java/checkstyle
entrou no projeto -- ver conversa do dia da etapa 3 do Python.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# Extensoes de arquivo por linguagem, usadas para filtrar quais arquivos
# de um commit interessam a cada linter (estilo sufixo, ex: ".py", nao
# glob -- diferente do pathspec do git log usado na etapa 2).
#
# "JavaScript" lista SO extensoes de JavaScript (.js/.jsx e as variantes
# de modulo .mjs/.cjs). TypeScript fica de fora de proposito: a regra do
# estudo e linta so arquivo da linguagem do repo, igual Python (.py) e
# Java (.java). Alem disso, o ESLint com o parser padrao nao entende
# sintaxe TS e geraria "Parsing error" como se fosse violacao. A etapa 2
# (extract_commit_history.py) so seleciona commits que tocam *.js/*.jsx.
LANGUAGE_EXTENSIONS = {
    "Python": [".py"],
    "Java": [".java"],
    "JavaScript": [".js", ".jsx", ".mjs", ".cjs"],
}


DEFAULT_GIT_TIMEOUT = 120  # segundos


def run_cmd(args: list[str], cwd: Path, timeout: int = DEFAULT_GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Roda um comando exigindo codigo de saida 0. Para ferramentas cujo
    exit code nao-zero e esperado (ex: linter que encontrou violacoes),
    NAO use esta funcao -- rode subprocess.run diretamente (ver PylintAdapter
    com --exit-zero e run_checkstyle, que tratam isso explicitamente).

    encoding="utf-8" + errors="replace" em vez de text=True puro: no
    Windows, text=True usa a codificacao padrao do console (geralmente
    cp1252 em maquinas em portugues), que quebra com caractere nao-ASCII
    na saida do git/java (nome de arquivo acentuado, etc.) e derruba a
    thread interna do subprocess com UnicodeDecodeError.

    timeout existe porque um clone parcial (--filter=blob:none) busca
    blob sob demanda da rede durante checkout -- se a rede engasgar sem
    derrubar a conexao (nao da erro, so nunca responde), o processo
    fica esperando pra sempre. Sem timeout, isso trava nao so o commit
    atual mas TODOS os commits seguintes na fila (o loop e sequencial),
    de qualquer repositorio, nao so o que engasgou."""
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{' '.join(args)} excedeu o timeout de {timeout}s em {cwd} -- "
            f"provavelmente rede travada buscando blob (clone parcial)"
        )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} falhou em {cwd}:\n{result.stderr}")
    return result


def checkout_commit(repo_dir: Path, commit_hash: str) -> None:
    # Se um processo anterior foi morto (kill, timeout, queda de sessao)
    # bem no meio de um checkout, o git pode deixar o .git/index.lock
    # orfao pra tras -- travando TODO checkout futuro nesse repositorio
    # ate alguem remover manualmente. Como cada execucao processa um
    # repositorio de cada vez, sequencialmente (nunca duas checkouts
    # concorrentes no mesmo repo_dir dentro do mesmo processo), e seguro
    # remover essa trava de cara aqui, antes de tentar.
    lock_file = repo_dir / ".git" / "index.lock"
    if lock_file.exists():
        logging.warning("Removendo .git/index.lock orfao (sobra de execucao anterior interrompida): %s", lock_file)
        lock_file.unlink(missing_ok=True)
    run_cmd(["git", "checkout", "--force", "--detach", commit_hash], cwd=repo_dir)


def get_changed_files(
    repo_dir: Path, commit_hash: str, extensions: list[str], exclude_dirs: Optional[set[str]] = None,
    exclude_suffixes: Optional[tuple[str, ...]] = None,
) -> list[str]:
    """Arquivos adicionados/modificados neste commit especifico (exclui removidos),
    restritos as extensoes da linguagem alvo.

    exclude_dirs: nomes de pasta (componente exato do caminho, nao
    substring) a excluir -- ex: {"test", "resources"}, seguindo Loriot
    et al. 2022 (Styler), que ignora arquivo dentro dessas pastas por
    quererem violacao que aconteceu no codigo principal, nao em teste.
    Comparacao e por COMPONENTE de caminho (ex: "src/test/java/Foo.java"
    tem "test" como componente), nao por substring do nome do arquivo
    (um arquivo chamado "TestUtils.java" na pasta principal NAO e
    excluido so por ter "test" no nome).

    exclude_suffixes: sufixos de NOME de arquivo a excluir (ex:
    (".test.js", ".spec.js")) -- para linguagens em que teste fica ao
    lado do codigo, sem pasta propria. Comparacao por final do nome.

    Limitacao conhecida: commits de merge tendem a nao aparecer aqui sem
    parents explicitos, entao sao pulados silenciosamente (contam como
    "0 arquivos mudados"). Para a versao mini isso e aceitavel."""
    result = run_cmd(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r",
         "--diff-filter=AM", commit_hash],
        cwd=repo_dir,
    )
    all_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    matched = [f for f in all_files if any(f.endswith(ext) for ext in extensions)]
    if exclude_dirs:
        matched = [f for f in matched if not set(f.split("/")) & exclude_dirs]
    if exclude_suffixes:
        matched = [f for f in matched if not f.endswith(exclude_suffixes)]
    return matched


def _win_long_path(path: Path) -> str:
    """No Windows, contorna o limite classico de 260 caracteres do MAX_PATH
    prefixando o caminho absoluto com \\\\?\\ (extended-length path) -- isso
    faz o Windows ignorar o limite pra chamadas de arquivo. Sem efeito em
    outros SOs (Linux/Mac nao tem esse limite)."""
    if os.name != "nt":
        return str(path)
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    return "\\\\?\\" + resolved


def archive_file(
    repo_dir: Path, dataset_dir: Path, language: str, linter: str,
    repo_full_name: str, branch: str, commit_hash: str, file_rel_path: str,
) -> str:
    """Copia o arquivo flagrado do working tree (checkout atual) pra
    arvore do dataset: Babel/<lang>/<linter>/<repo>/<branch>/<commit>/<path>.

    IMPORTANTE: file_rel_path precisa ser relativo ao repo_dir -- se vier
    absoluto (ex: alguns linters devolvem o path assim na saida deles),
    juntar com pathlib faz o prefixo do dataset_dir ser descartado e
    src/dest colapsam no mesmo arquivo. Relativize antes de chamar isso.

    Duas mitigacoes pro limite de 260 caracteres do Windows (MAX_PATH),
    que estoura facil aqui por causa da profundidade real: dataset_dir +
    linguagem + linter + repo + branch + commit (40 chars em hash cheio)
    + a arvore de pacotes original do projeto (Java em particular tende
    a ser fundo, tipo com/empresa/modulo/sub/Classe.java):
      1. usa so os 12 primeiros caracteres do commit_hash no nome da
         pasta local (o registro em violations.jsonl continua com o
         hash INTEIRO -- isso so afeta onde o arquivo fica salvo em disco,
         12 caracteres de hash ja sao unicos o suficiente pra esse volume
         de commits).
      2. usa o prefixo de extended-length path (\\\\?\\) do Windows nas
         chamadas de mkdir/copy, que ignora o limite de 260 caracteres.
    """
    short_hash = commit_hash[:12]
    src = repo_dir / file_rel_path
    dest = (
        dataset_dir / language / linter / repo_full_name.replace("/", "__")
        / branch / short_hash / file_rel_path
    )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        os.makedirs(_win_long_path(dest.parent), exist_ok=True)

    if not src.exists():
        return str(dest)
    try:
        shutil.copy2(src, dest)
    except OSError:
        # Fallback pro extended-length path do Windows (MAX_PATH) -- so
        # tenta de novo se a copia direta falhou por causa disso.
        shutil.copy2(_win_long_path(src), _win_long_path(dest))
    return str(dest)
