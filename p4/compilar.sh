#!/usr/bin/env bash
# Compila urllc.p4 pro alvo BMv2 (v1model).
#   bash p4/compilar.sh
# Gera p4/build/urllc.json e p4/build/urllc.p4info.txt
set -euo pipefail

DIRETORIO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAIDA="${DIRETORIO}/build"
mkdir -p "${SAIDA}"

echo ">>> Compilando urllc.p4 para BMv2"
p4c --target bmv2 \
    --arch v1model \
    --p4runtime-files "${SAIDA}/urllc.p4info.txt" \
    -o "${SAIDA}" \
    "${DIRETORIO}/urllc.p4"

echo ">>> Gerado: ${SAIDA}/urllc.json"
ls -lh "${SAIDA}"
