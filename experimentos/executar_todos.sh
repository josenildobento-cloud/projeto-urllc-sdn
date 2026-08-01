#!/usr/bin/env bash
# Roda os quatro cenários em sequência e consolida tudo.
#
#   sudo bash experimentos/executar_todos.sh [duracao_segundos] [limiar_ms]
#
# Cada cenário sobe a topologia, deixa rodar pela duração indicada, encerra
# o Mininet e consolida em resultados/. O limiar de SLA é o mesmo pros 4
# cenários pra tabela comparativa da Avaliação sair justa (ver calibração
# no README).
set -uo pipefail

DURACAO="${1:-90}"
LIMIAR_MS="${2:-5}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${RAIZ}"

if [[ $EUID -ne 0 ]]; then
  echo "Execute com sudo." >&2
  exit 1
fi

if [[ ! -f p4/build/urllc.json ]]; then
  echo ">>> Compilando o programa P4"
  bash p4/compilar.sh
fi

for CENARIO in 1 2 3 4; do
  echo
  echo ">>> Cenário ${CENARIO} — ${DURACAO}s de coleta"

  mn -c > /dev/null 2>&1

  ( sleep "${DURACAO}"; echo "exit" ) | \
    python3 topologia/topologia.py \
      --cenario "${CENARIO}" \
      --banda-principal 10 \
      --banda-backup 5 \
      --limite-robo 2 \
      --limite-background 20 \
      --limiar-ms "${LIMIAR_MS}"

  sleep 3
  python3 experimentos/consolidar.py --cenario "${CENARIO}" --limiar-ms "${LIMIAR_MS}"

  cp /tmp/painel_sdn/decisoes_sdn.jsonl \
     "resultados/cenario${CENARIO}_decisoes.jsonl" 2>/dev/null || true
done

echo
echo ">>> Todos os cenários concluídos. Resultados em resultados/"
ls -1 resultados/
