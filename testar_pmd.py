"""
testar_pmd.py -- teste de fumaca do PMDAdapter com PMD de verdade.

Cria um repositorio git temporario com um commit por cenario (pom sem
plugin, ruleset proprio, ruleset embutido, ${var}, URL remota, arquivo
inexistente, excludeFromFailureFile, ruleset legado que so o PMD 6 le,
NOPMD, erro de sintaxe...) e confere o veredito de cada um.

Uso:  python3 testar_pmd.py --pmd-home /opt/pmd/pmd-bin-7.29.0 --pmd-home /opt/pmd/pmd-bin-6.55.0
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from linter_adapters import ADAPTERS, CommitDiscarded

POM_NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def pom(plugin_conf=None, plugin=True):
    plug = ""
    if plugin:
        conf = f"<configuration>{plugin_conf}</configuration>" if plugin_conf is not None else ""
        plug = (f"<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
                f"<artifactId>maven-pmd-plugin</artifactId><version>3.21.0</version>{conf}</plugin></plugins></build>")
    return (f'<project {POM_NS}><modelVersion>4.0.0</modelVersion><groupId>t</groupId>'
            f'<artifactId>t</artifactId><version>1</version>{plug}</project>')


def rs(rules, extra=""):
    body = "".join(f'<rule ref="{r}"/>' for r in rules)
    return ('<?xml version="1.0"?><ruleset name="t" xmlns="http://pmd.sourceforge.net/ruleset/2.0.0">'
            f'<description>t</description>{extra}{body}</ruleset>')


FOO = """package a;
import java.util.List;
public class Foo {
    private int unused;
    public void bar(int x) { int y = 3; if (x == 1) { } }
}
"""
FOO_NOPMD = """package a;
import java.util.List; // NOPMD
public class Foo {
    private int unused; // NOPMD
}
"""
BAD = "package a;\npublic class Bad { void f( { }\n"

PRIV = "category/java/bestpractices.xml/UnusedPrivateField"
IMPORT = "category/java/codestyle.xml/UnnecessaryImport"

# (nome, arquivos {caminho: texto}, esperado)  esperado = ("excluido", {motivos}) | ("ok", ruleset, {regras})
SCENARIOS = [
    ("sem_pom", {"src/main/java/a/Foo.java": FOO}, ("excluido", {"nao_maven"})),
    ("pom_sem_plugin", {"pom.xml": pom(plugin=False), "src/main/java/a/Foo.java": FOO}, ("ok", "default", None)),
    ("plugin_sem_rulesets", {"pom.xml": pom(plugin_conf=""), "src/main/java/a/Foo.java": FOO}, ("ok", "default", None)),
    ("ruleset_arquivo_repo", {"pom.xml": pom("<rulesets><ruleset>config/pmd.xml</ruleset></rulesets>"),
                              "config/pmd.xml": rs([PRIV, IMPORT]), "src/main/java/a/Foo.java": FOO},
     ("ok", "own", {"UnusedPrivateField", "UnnecessaryImport"})),
    ("ruleset_embutido", {"pom.xml": pom("<rulesets><ruleset>category/java/bestpractices.xml</ruleset></rulesets>"),
                          "src/main/java/a/Foo.java": FOO}, ("ok", "own", {"UnusedPrivateField", "UnusedLocalVariable"})),
    ("ruleset_com_variavel", {"pom.xml": pom("<rulesets><ruleset>${project.basedir}/pmd.xml</ruleset></rulesets>"),
                              "src/main/java/a/Foo.java": FOO}, ("excluido", {"config_variavel"})),
    ("ruleset_variavel_no_arquivo", {"pom.xml": pom("<rulesets><ruleset>pmd.xml</ruleset></rulesets>"),
                                     "pmd.xml": rs([PRIV], "<property name=\"x\" value=\"${foo}\"/>"),
                                     "src/main/java/a/Foo.java": FOO}, ("excluido", {"config_variavel"})),
    ("ruleset_url", {"pom.xml": pom("<rulesets><ruleset>https://example.com/pmd.xml</ruleset></rulesets>"),
                     "src/main/java/a/Foo.java": FOO}, ("excluido", {"ruleset_remoto"})),
    ("ruleset_inexistente", {"pom.xml": pom("<rulesets><ruleset>nao/existe.xml</ruleset></rulesets>"),
                             "src/main/java/a/Foo.java": FOO}, ("excluido", {"ruleset_nao_encontrado"})),
    ("exclude_from_failure", {"pom.xml": pom("<excludeFromFailureFile>x.properties</excludeFromFailureFile>"),
                              "src/main/java/a/Foo.java": FOO}, ("excluido", {"pom_config_ou_suppression"})),
    ("flag_nao_bloqueante", {"pom.xml": pom("<minimumPriority>3</minimumPriority><includeTests>true</includeTests>"
                                            "<rulesets><ruleset>category/java/bestpractices.xml</ruleset></rulesets>"),
                             "src/main/java/a/Foo.java": FOO}, ("ok", "own", {"UnusedPrivateField", "UnusedLocalVariable"})),
    ("ruleset_legado_so_pmd6", {"pom.xml": pom("<rulesets><ruleset>old.xml</ruleset></rulesets>"),
                                "old.xml": rs(["rulesets/java/unusedcode.xml"]),
                                "src/main/java/a/Foo.java": FOO}, ("ok", "own", {"UnusedPrivateField", "UnusedLocalVariable"})),
    ("nopmd_suprime", {"pom.xml": pom("<rulesets><ruleset>config/pmd.xml</ruleset></rulesets>"),
                       "config/pmd.xml": rs([PRIV, IMPORT]), "src/main/java/a/Foo.java": FOO_NOPMD}, ("ok", "own", set())),
    ("erro_de_sintaxe", {"pom.xml": pom("<rulesets><ruleset>config/pmd.xml</ruleset></rulesets>"),
                         "config/pmd.xml": rs([PRIV, IMPORT]), "src/main/java/a/Foo.java": FOO,
                         "src/main/java/a/Bad.java": BAD}, ("ok", "own", {"UnusedPrivateField", "UnnecessaryImport"})),
]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pmd-home", action="append", required=True)
    args = ap.parse_args()
    adapter = ADAPTERS["pmd"](args)
    print("versoes (da mais nova pra mais antiga):", [v["label"] for v in adapter.versions])

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
        for nome, arquivos, esperado in SCENARIOS:
            for p in repo.rglob("*"):
                if p.is_file() and ".git" not in p.parts:
                    p.unlink()
            for caminho, texto in arquivos.items():
                f = repo / caminho
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(texto, encoding="utf-8")
            git(repo, "add", "-A"); git(repo, "commit", "-q", "--allow-empty", "-m", nome)
            h = git(repo, "rev-parse", "HEAD")
            java_files = sorted(c for c in arquivos if c.endswith(".java"))
            try:
                plan = adapter.precheck(repo, h, "main")
            except CommitDiscarded as exc:
                got = ("excluido", set(exc.reasons))
                ok = esperado[0] == "excluido" and got[1] == esperado[1]
                print(("OK  " if ok else "FALHA"), f"{nome:28s} excluido {sorted(exc.reasons)}  ({exc.detail[:60]})")
                failures += 0 if ok else 1
                continue
            git(repo, "checkout", "-q", h)
            vios = adapter.run(repo, java_files)
            regras = {v["rule_id"] for v in vios}
            info = adapter.last_run_info
            ok = esperado[0] == "ok" and plan["ruleset"] == esperado[1] and (esperado[2] is None or regras == esperado[2])
            versoes = {v["linter_version"] for v in vios}
            print(("OK  " if ok else "FALHA"), f"{nome:28s} ruleset={plan['ruleset']:7s} motivo={plan['ruleset_motivo']} "
                  f"viol={len(vios)} regras={sorted(regras)[:4]} versoes={sorted(versoes)} "
                  f"falhou={info.get('own_config_load_failed')} erros_proc={info.get('pmd_erros_de_processamento')}")
            if vios:
                v = vios[0]
                print("      exemplo:", {k: v[k] for k in ("file_path", "line", "column", "rule_id", "severity")}, "|", v["message"][:50])
            failures += 0 if ok else 1
    print("\nRESULTADO:", "TUDO OK" if failures == 0 else f"{failures} cenario(s) com falha")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
