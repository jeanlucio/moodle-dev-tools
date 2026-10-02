#!/usr/bin/env bash
# moodle-security-audit-calibrate — mede o quanto o moodle-security-audit se aproxima do
# MDL Shield, rodando a auditoria no commit exato que uma revisão pública do MDL Shield
# avaliou e comparando achado por achado.
#
# Cada revisão pública aponta o repositório e o commit revisados (todo achado tem link para
# github.com/<dono>/<repo>/blob/<sha>/<arquivo>#L<linha>), o que a torna gabarito: sem isso,
# uma mudança no catálogo de regras só pode ser julgada plugin a plugin, no olho.
#
# Uso:
#   moodle-security-audit-calibrate list
#   moodle-security-audit-calibrate fetch <revisão>
#   moodle-security-audit-calibrate run <revisão> [--with-phpstan] [-- opções da auditoria]
#   moodle-security-audit-calibrate compare <revisão>
#   moodle-security-audit-calibrate summary
#
#   <revisão> : slug do `list` (ex. tool_courserating_2026-10-02,
#               publisher/exputo/mod_profilefield), URL completa do mdlshield.com, ou o
#               caminho de um export Markdown do dashboard (revisões privadas dos seus
#               plugins).
#
#   list    : revisões públicas, marcando as já baixadas/comparadas.
#   fetch   : baixa a revisão e grava o gabarito (sem cota de IA).
#   run     : clona o commit revisado, roda a auditoria e compara (GASTA cota de IA — uma
#             auditoria completa por revisão). PHPStan desligado por padrão: o clone fica
#             fora da árvore do Moodle e o PHPStan só geraria ruído.
#   compare : refaz a comparação com a última auditoria já rodada (sem cota).
#   summary : uma linha por revisão comparada (nota MDL × local, recall por tipo).
#
# Tudo fica em ~/.moodle-security-audit-cache/calibration/<revisão>/ — páginas de terceiros
# não entram neste repositório.

set -euo pipefail

case "${1:-}" in
    -h|--help|'') sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

python3 -u "$(dirname "$(readlink -f "$0")")/security_audit_calibrate.py" "$@"
