#!/usr/bin/env python3
"""
limpar_violations_pylint.py

Deixa so os registros de PYLINT no violations.jsonl (que tambem tem registros
antigos de checkstyle e eslint). NENHUM registro de pylint e filtrado: o que e
pylint fica, inclusive os avisos de config (R0022, W0012...).

Ordem, com parada em qualquer falha ANTES de mexer no original:
  1. copia o original para <backup-dir>/<nome> e confere byte a byte
     (aborta se o backup ja existir -- nunca sobrescreve backup);
  2. le o original e grava os registros mantidos em <nome>.tmp_limpo
     (copia cada linha verbatim; linha ilegivel vai para
     <nome>.nao_parseadas, nunca e descartada);
  3. confere o arquivo novo (todas as linhas parseiam, contagem bate);
  4. confere que o original NAO mudou durante a execucao (tamanho e mtime);
  5. so entao troca o original pelo novo (os.replace, atomico).

Um registro so e removido se for identificado com seguranca como de outro
linter (campo "linter", ou formato da regra / prefixo de linter_version).
Registro que nao da pra classificar e MANTIDO e contado.

Uso:
    python3 limpar_violations_pylint.py --violations violations.jsonl --backup-dir backup_20261102
    (--so-preparar: faz 1-4 e para antes de trocar o original)
"""
import argparse, filecmp, os, re, shutil, sys
from collections import Counter
from pathlib import Path

try:
    import orjson
    def loads(b): return orjson.loads(b)
except ImportError:
    import json
    def loads(b): return json.loads(b)

PYLINT_ID = re.compile(r"^[CRWEFI]\d{4}$")
MANTER = {"pylint", "indefinido"}


def classificar(v):
    l = v.get("linter")
    if l:
        return str(l)
    r = v.get("rule_id") or ""
    if PYLINT_ID.match(r):
        return "pylint"
    if r.startswith("com.puppycrawl") or ".checks." in r:
        return "checkstyle"
    ver = str(v.get("linter_version") or "")
    if ver.startswith("pylint"):
        return "pylint"
    if ver.startswith("checkstyle"):
        return "checkstyle"
    return "indefinido"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--violations", required=True)
    ap.add_argument("--backup-dir", required=True)
    ap.add_argument("--so-preparar", action="store_true")
    args = ap.parse_args()

    src = Path(args.violations)
    bdir = Path(args.backup_dir)
    backup = bdir / src.name
    tmp = src.with_name(src.name + ".tmp_limpo")
    bad = src.with_name(src.name + ".nao_parseadas")
    if not src.exists():
        sys.exit(f"nao existe: {src}")
    if backup.exists():
        sys.exit(f"ABORTADO: o backup ja existe ({backup}); nada foi alterado. Use outro --backup-dir.")
    for p in (tmp, bad):
        if p.exists():
            sys.exit(f"ABORTADO: sobra de execucao anterior ({p}); confira e remova antes de rodar de novo. Nada foi alterado.")

    st0 = src.stat()
    print(f"[1/5] backup: {src} ({st0.st_size:,} bytes) -> {backup}", flush=True)
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, backup)
    if not filecmp.cmp(src, backup, shallow=False):
        sys.exit("ABORTADO: o backup nao e identico ao original; original nao foi alterado.")
    print("      backup conferido (identico byte a byte).", flush=True)

    print("[2/5] separando (mantem so pylint)...", flush=True)
    kept = Counter(); dropped = Counter(); n_bad = 0
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out, open(bad, "wb") as badf:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                if not line.endswith(b"\n"):
                    line += b"\n"
                try:
                    v = loads(line)
                    k = classificar(v)
                except Exception:
                    n_bad += 1
                    badf.write(line)
                    continue
                if k in MANTER:
                    out.write(line); kept[k] += 1
                else:
                    dropped[k] += 1
                if i % 2_000_000 == 0:
                    print(f"      ... {i:,} linhas", flush=True)
    except BaseException:
        print("ERRO durante a leitura; original intacto; removendo temporarios.", file=sys.stderr)
        tmp.unlink(missing_ok=True); bad.unlink(missing_ok=True)
        raise
    if n_bad == 0:
        bad.unlink(missing_ok=True)

    print("[3/5] conferindo o arquivo novo...", flush=True)
    n_new = 0; n_nonpyl = 0
    with open(tmp, "rb") as f:
        for line in f:
            n_new += 1
            if classificar(loads(line)) not in MANTER:
                n_nonpyl += 1
    if n_new != sum(kept.values()) or n_nonpyl:
        tmp.unlink(missing_ok=True)
        sys.exit(f"ABORTADO: conferencia falhou (linhas={n_new}, esperado={sum(kept.values())}, nao-pylint={n_nonpyl}); original intacto.")

    print("[4/5] conferindo que o original nao mudou durante a execucao...", flush=True)
    st1 = src.stat()
    if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
        tmp.unlink(missing_ok=True)
        sys.exit("ABORTADO: o original foi modificado durante a execucao (alguem estava gravando?); nada foi trocado.")

    rel = [
        f"origem: {src} ({st0.st_size:,} bytes)",
        f"backup: {backup}",
        f"mantidos: {sum(kept.values()):,} (pylint {kept['pylint']:,}; nao classificaveis, mantidos por seguranca {kept['indefinido']:,})",
        "removidos: " + (", ".join(f"{k}={c:,}" for k, c in dropped.most_common()) or "nenhum"),
        f"linhas ilegiveis (em {bad.name}, nao descartadas): {n_bad:,}",
        f"arquivo novo: {tmp.stat().st_size:,} bytes",
    ]
    (bdir / "RELATORIO_limpeza.txt").write_text("\n".join(rel) + "\n", encoding="utf-8")
    print("\n".join(rel), flush=True)

    if args.so_preparar:
        print(f"[5/5] --so-preparar: parei antes de trocar. Novo arquivo em {tmp}; original intacto.")
        return
    shutil.copymode(src, tmp)
    os.replace(tmp, src)
    print(f"[5/5] pronto: {src} agora so tem pylint. O original completo esta em {backup}.")


if __name__ == "__main__":
    main()
