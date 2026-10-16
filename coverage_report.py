#!/usr/bin/env python3
"""coverage_report.py — apoio do coverage.sh (moodle-coverage).

Dois subcomandos:

  prepare  monta o phpunit.xml da medição (escrito em stdout) e grava em --scope-out um JSON
           com a classificação de TODO arquivo do plugin: lógica, entry point, declarativo,
           fora do escopo e não medível por PHPUnit.
  report   lê o(s) relatório(s) XML do PHPUnit e imprime a tabela por arquivo e por função,
           separada por essas classes.

Por que classificar: o PHPUnit só relata o que está no <source>. A lista antiga (classes/ mais
alguns arquivos de função) deixava de fora mod_form.php, view.php, index.php, export.php,
db/*.php e o backup, e foi exatamente aí que a auditoria achou bugs que os testes nunca
exercitavam. Aqui o <source> é o plugin inteiro menos exclusões explícitas, e tudo o que fica de
fora do percentual é listado com o motivo, para que nada suma do relatório em silêncio.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = {"c": "https://schema.phpunit.de/coverage/1.0"}

# Pastas na raiz do plugin que nunca entram na medição, e o motivo.
EXCLUDED_TOP_DIRS = {
    "tests": "testes",
    "cli": "scripts de linha de comando (seed e afins)",
    "lang": "strings de idioma (arrays declarativos)",
    "amd": "JavaScript AMD (não é PHP)",
    "yui": "JavaScript YUI (não é PHP)",
    "pix": "imagens e ícones",
    "templates": "templates Mustache (não é PHP)",
    "docs": "site de documentação",
    ".github": "workflows de CI",
}
# Pastas excluídas em qualquer profundidade.
EXCLUDED_ANY_DIRS = {"node_modules": "dependências de desenvolvimento", "vendor": "bibliotecas de terceiros", ".git": "git"}

# Arquivos de db/ que só declaram arrays: carregá-los não é testar comportamento.
DECLARATIVE_DB = {
    "access.php", "caches.php", "events.php", "hooks.php", "log.php", "messages.php", "mobile.php",
    "renamedclasses.php", "legacyclasses.php", "services.php", "tasks.php", "subplugins.php",
}

ENTRY_POINT_RE = re.compile(r"require(?:_once)?\s*\(.*config\.php", re.I)


def read_head(path, lines=80):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return "".join(next(fh, "") for _ in range(lines))
    except OSError:
        return ""


def count_lines(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def thirdparty_locations(host_dir):
    """Caminhos (relativos à raiz do plugin) declarados em thirdpartylibs.xml."""
    path = os.path.join(host_dir, "thirdpartylibs.xml")
    found = []
    if not os.path.isfile(path):
        return found
    try:
        for loc in ET.parse(path).getroot().iter("location"):
            if loc.text and loc.text.strip():
                found.append(loc.text.strip().strip("/"))
    except ET.ParseError:
        pass
    return found


def classify(host_dir):
    """Percorre o plugin e classifica cada arquivo. Devolve o dicionário do escopo."""
    thirdparty = thirdparty_locations(host_dir)
    scope = {
        "logic": [], "entry_points": [], "declarative": [], "not_measured": [],
        "excluded": [], "unmeasurable": [], "behat_features": [], "thirdparty": thirdparty,
    }
    excluded_dirs = {}
    excluded_counts = {}

    for root, dirs, files in os.walk(host_dir):
        rel_root = os.path.relpath(root, host_dir)
        rel_root = "" if rel_root == "." else rel_root
        keep = []
        for d in sorted(dirs):
            rel = os.path.join(rel_root, d) if rel_root else d
            reason = None
            if not rel_root and d in EXCLUDED_TOP_DIRS:
                reason = EXCLUDED_TOP_DIRS[d]
            elif d in EXCLUDED_ANY_DIRS:
                reason = EXCLUDED_ANY_DIRS[d]
            elif any(rel == t or rel.startswith(t + "/") for t in thirdparty):
                reason = "biblioteca de terceiros (thirdpartylibs.xml)"
            if reason:
                excluded_dirs[rel] = reason
                excluded_counts[rel] = sum(
                    1 for _r, _d, fs in os.walk(os.path.join(root, d)) for f in fs if f.endswith(".php")
                )
            else:
                keep.append(d)
        dirs[:] = keep

        for f in sorted(files):
            rel = os.path.join(rel_root, f) if rel_root else f
            if not f.endswith(".php"):
                continue
            if any(rel == t for t in thirdparty):
                scope["excluded"].append({"path": rel, "reason": "biblioteca de terceiros (thirdpartylibs.xml)", "files": 1})
                continue
            if rel == "version.php":
                scope["not_measured"].append({"path": rel, "reason": "declarativo, carregado só pelo core"})
            elif ENTRY_POINT_RE.search(read_head(os.path.join(root, f))):
                scope["entry_points"].append({"path": rel, "lines": count_lines(os.path.join(root, f))})
            elif rel_root == "db" and f in DECLARATIVE_DB:
                scope["declarative"].append(rel)
            else:
                scope["logic"].append(rel)

    for rel, reason in sorted(excluded_dirs.items()):
        scope["excluded"].append({"path": rel + "/", "reason": reason, "files": excluded_counts[rel]})

    # Não medíveis por PHPUnit: JavaScript e templates.
    def tally(label, patterns):
        count, lines = 0, 0
        for root, dirs, files in os.walk(host_dir):
            dirs[:] = [d for d in dirs if d not in ("node_modules", "build", ".git", "vendor")]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, host_dir)
                if any(re.fullmatch(pt, rel) for pt in patterns) and not f.endswith(".min.js"):
                    count += 1
                    lines += count_lines(p)
        if count:
            scope["unmeasurable"].append({"label": label, "files": count, "lines": lines})

    tally("amd/src (JavaScript AMD)", [r"amd/src/.*\.js"])
    tally("yui/src (JavaScript YUI)", [r"yui/src/.*\.js"])
    tally("js/ (JavaScript do app móvel)", [r"js/.*\.js"])
    tally("templates/ (Mustache)", [r"templates/.*\.mustache"])

    behat = os.path.join(host_dir, "tests", "behat")
    if os.path.isdir(behat):
        scope["behat_features"] = sorted(f for f in os.listdir(behat) if f.endswith(".feature"))
    return scope


def build_xml(args, scope):
    includes = []
    excludes = []
    if args.filter:
        includes.append(f'      <directory suffix=".php">{args.container_dir}/{args.filter}</directory>')
    else:
        includes.append(f'      <directory suffix=".php">{args.container_dir}</directory>')
        for rel, reason in sorted((d["path"], d["reason"]) for d in scope["excluded"]):
            tag = "directory" if rel.endswith("/") else "file"
            excludes.append(f"      <{tag}>{args.container_dir}/{rel.rstrip('/')}</{tag}>")
        for entry in scope["entry_points"]:
            excludes.append(f'      <file>{args.container_dir}/{entry["path"]}</file>')
        for item in scope["not_measured"]:
            excludes.append(f'      <file>{args.container_dir}/{item["path"]}</file>')
    exclude_block = ("    <exclude>\n" + "\n".join(excludes) + "\n    </exclude>\n") if excludes else ""

    # failOnDeprecation/failOnWarning ficam false: é uma rodada de MEDIÇÃO, não um gate.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<phpunit
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:noNamespaceSchemaLocation="https://schema.phpunit.de/10.5/phpunit.xsd"
  bootstrap="{args.bootstrap}"
  processIsolation="false"
  backupGlobals="false"
  cacheResult="false"
  failOnDeprecation="false"
  failOnWarning="false"
  beStrictAboutTestsThatDoNotTestAnything="false"
  beStrictAboutOutputDuringTests="true"
  cacheDirectory="{args.cache_dir}"
  backupStaticProperties="false"
>
  <php>
    <const name="PHPUNIT_SEQUENCE_START" value="157000"/>
  </php>
  <extensions>
    <bootstrap class="core\\tests\\phpunit\\moodle_extension"/>
  </extensions>
  <testsuites>
    <testsuite name="{args.franken}">
      <directory suffix="_test.php">{args.tests_dir}</directory>
      <exclude>{args.tests_dir}/fixtures</exclude>
      <exclude>{args.tests_dir}/generator</exclude>
    </testsuite>
  </testsuites>
  <source>
    <include>
{chr(10).join(includes)}
    </include>
{exclude_block}  </source>
</phpunit>
"""


def cmd_prepare(args):
    if args.filter:
        scope = {"filtered": args.filter, "logic": [], "entry_points": [], "declarative": [], "not_measured": [],
                 "excluded": [], "unmeasurable": [], "behat_features": [], "thirdparty": []}
    else:
        scope = classify(args.host_dir)
    with open(args.scope_out, "w", encoding="utf-8") as fh:
        json.dump(scope, fh, indent=1)
    sys.stdout.write(build_xml(args, scope))


# --------------------------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------------------------- #

def parse_xml_dir(xml_dir):
    """Lê os XML por arquivo do PHPUnit. Devolve {caminho relativo: dados}."""
    result = {}
    for root, _dirs, files in os.walk(xml_dir):
        for f in files:
            if not f.endswith(".xml") or f == "index.xml":
                continue
            try:
                node = ET.parse(os.path.join(root, f)).getroot().find("c:file", NS)
            except ET.ParseError:
                continue
            if node is None:
                continue
            rel = (node.get("path", "/").strip("/") + "/" + node.get("name")).lstrip("/")
            lines = node.find("c:totals/c:lines", NS)
            executable = int(lines.get("executable"))
            executed = int(lines.get("executed"))
            units = []
            for fn in node.findall("c:function", NS):
                units.append((fn.get("name") + "()", int(fn.get("executable")), int(fn.get("executed"))))
            for cls in node.findall("c:class", NS):
                short = cls.get("name").split("\\")[-1]
                for m in cls.findall("c:method", NS):
                    units.append((f"{short}::{m.get('name')}()", int(m.get("executable")), int(m.get("executed"))))
            result[rel] = {"executable": executable, "executed": executed, "units": units}
    return result


def pct(executed, executable):
    return 100.0 * executed / executable if executable else 100.0


def bar(value, width=10):
    filled = int(round(width * value / 100.0))
    return "█" * filled + "░" * (width - filled)


def cmd_report(args):
    scope = json.load(open(args.scope, encoding="utf-8"))
    main = parse_xml_dir(args.xml)
    other = parse_xml_dir(args.credited_xml) if args.credited_xml else None
    if scope.get("filtered"):
        # Com --filter o PHPUnit relata caminhos relativos à subpasta; devolve o prefixo para o
        # relatório mostrar o caminho completo a partir da raiz do plugin.
        prefix = scope["filtered"].strip("/") + "/"
        main = {prefix + k: v for k, v in main.items()}
        other = {prefix + k: v for k, v in other.items()} if other else None
    out = []
    w = out.append

    modo = {
        "executed": "EXECUTADA: conta tudo o que os testes executam, sem o filtro de @covers",
        "credited": "CREDITADA: só conta o que a anotação @covers de cada teste lista",
    }[args.mode]
    w(f"Cobertura de {args.plugin} — {modo}")
    if scope.get("filtered"):
        w(f"(escopo restrito a {scope['filtered']}: sem classificação dos demais arquivos)")
    w("")

    if scope.get("filtered"):
        logic = sorted(main)
    else:
        logic = list(scope["logic"])
        # arquivos que o PHPUnit relatou mas a classificação não conhece entram como lógica
        known = set(scope["logic"]) | set(scope["declarative"])
        logic += sorted(p for p in main if p not in known)
    logic = sorted(set(logic))

    rows = []
    for p in logic:
        d = main.get(p)
        if d is None:
            continue
        rows.append((pct(d["executed"], d["executable"]), p, d))
    rows.sort(key=lambda r: (r[0], r[1]))

    total_exec = sum(r[2]["executed"] for r in rows)
    total_able = sum(r[2]["executable"] for r in rows)
    w(f"LÓGICA — conta no percentual: {total_exec}/{total_able} linhas = {pct(total_exec, total_able):.1f}%  ({len(rows)} arquivos)")
    head = f"  {'arquivo':<56} {'linhas':>13} {'%':>6}  "
    if other:
        head += f"{'creditada':>9} {'dif.':>6}  "
    w(head + "funções")
    for value, p, d in rows:
        tested = sum(1 for _n, ex, ed in d["units"] if ex and ed == ex)
        units = len(d["units"])
        line = f"  {p:<56} {d['executed']:>5}/{d['executable']:<7} {value:>5.0f}%  "
        if other:
            o = other.get(p)
            if o:
                ov = pct(o["executed"], o["executable"])
                line += f"{ov:>8.0f}% {value - ov:>+5.0f}  "
            else:
                line += f"{'n/a':>9} {'':>6}  "
        line += f"{tested}/{units}" if units else "—"
        w(line)
    w("")

    never = []
    partial = 0
    for _v, p, d in rows:
        names = [n for n, ex, ed in d["units"] if ex and ed == 0]
        partial += sum(1 for _n, ex, ed in d["units"] if ex and 0 < ed < ex)
        if names:
            never.append((p, names))
    w(f"FUNÇÕES E MÉTODOS NUNCA EXECUTADOS POR NENHUM TESTE ({sum(len(n) for _p, n in never)})")
    if never:
        for p, names in never:
            w(f"  {p}")
            for i in range(0, len(names), 3):
                w("      " + ", ".join(names[i:i + 3]))
    else:
        w("  nenhum")
    w(f"  (e {partial} com execução parcial — use --html para ver as linhas)")
    w("")

    if not scope.get("filtered"):
        eps = scope["entry_points"]
        w(f"ENTRY POINTS — fora do percentual; o PHPUnit não os carrega (chamam config.php), só o Behat/E2E ({len(eps)})")
        if eps:
            for e in sorted(eps, key=lambda e: -e["lines"]):
                w(f"  {e['path']:<56} {e['lines']:>5} linhas")
            feats = scope["behat_features"]
            w(f"  Behat: {len(feats)} feature(s) em tests/behat" + (" — " + ", ".join(feats) if feats else " — NENHUMA"))
        else:
            w("  nenhum")
        w("")

        decl = scope["declarative"]
        w(f"DECLARATIVOS — arrays de db/; \"carregado\" não significa \"testado\" ({len(decl)})")
        for p in decl:
            d = main.get(p)
            if d is None:
                state = "fora do relatório do PHPUnit"
            elif d["executed"] > 0:
                state = f"carregado ({d['executed']}/{d['executable']} linhas)"
            else:
                state = "NUNCA carregado por nenhum teste"
            w(f"  {p:<56} {state}")
        for item in scope["not_measured"]:
            w(f"  {item['path']:<56} não medido: {item['reason']}")
        w("")

        w("FORA DO ESCOPO — não medidos de propósito")
        with_php = [e for e in scope["excluded"] if e["files"] > 0]
        without_php = [e["path"] for e in scope["excluded"] if e["files"] == 0 and e["path"] != ".git/"]
        for e in with_php:
            w(f"  {e['path']:<56} {e['reason']} ({e['files']} arquivo(s) PHP)")
        if without_php:
            w(f"  sem arquivos PHP, não afetam a medição: {', '.join(without_php)}")
        w("")

        w("NÃO MEDÍVEL POR PHPUNIT — dependem de Behat ou de teste de JavaScript")
        if scope["unmeasurable"]:
            for u in scope["unmeasurable"]:
                w(f"  {u['label']:<56} {u['files']:>3} arquivo(s), {u['lines']:>5} linhas")
        else:
            w("  nada")

    print("\n".join(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--host-dir", required=True)
    p.add_argument("--container-dir", required=True)
    p.add_argument("--tests-dir", required=True)
    p.add_argument("--franken", required=True)
    p.add_argument("--bootstrap", required=True)
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--scope-out", required=True)
    p.add_argument("--filter", default="")
    p.set_defaults(func=cmd_prepare)

    r = sub.add_parser("report")
    r.add_argument("--scope", required=True)
    r.add_argument("--plugin", required=True)
    r.add_argument("--xml", required=True)
    r.add_argument("--credited-xml", default="")
    r.add_argument("--mode", choices=["executed", "credited"], default="executed")
    r.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
