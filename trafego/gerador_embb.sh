#!/usr/bin/env bash
# Tráfego eMBB (alta demanda de banda).
#
# Uso dentro do Mininet, a partir do host de origem:
#   mininet> pc bash trafego/gerador_embb.sh cliente 10.0.0.4 20 600
#   mininet> medico bash trafego/gerador_embb.sh servidor
#
# Parâmetros do modo cliente: <destino> <banda_mbps> <duracao_s>
set -euo pipefail

MODO="${1:-cliente}"
PORTA_IPERF=5201
DIR_LOG="/tmp/painel_sdn"
mkdir -p "${DIR_LOG}"

case "${MODO}" in
  servidor)
    echo "[embb] servidor iperf3 na porta ${PORTA_IPERF}"
    exec iperf3 -s -p "${PORTA_IPERF}"
    ;;

  cliente)
    DESTINO="${2:-10.0.0.4}"
    BANDA="${3:-20}"
    DURACAO="${4:-600}"
    echo "[embb] ${BANDA} Mbps para ${DESTINO} por ${DURACAO}s"
    exec iperf3 -c "${DESTINO}" -p "${PORTA_IPERF}" \
        -b "${BANDA}M" -t "${DURACAO}" -P 4 --format m \
        --logfile "${DIR_LOG}/iperf_cliente.log"
    ;;

  # Alternativa com streaming de vídeo, citada no enunciado.
  # Requer ffmpeg instalado: sudo apt-get install -y ffmpeg
  video)
    DESTINO="${2:-10.0.0.4}"
    BITRATE="${3:-8000k}"
    echo "[embb] streaming sintético de ${BITRATE} para ${DESTINO}:5004"
    exec ffmpeg -re -f lavfi -i "testsrc=size=1920x1080:rate=30" \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -b:v "${BITRATE}" -f mpegts "udp://${DESTINO}:5004"
    ;;

  *)
    echo "Modo inválido. Use: servidor | cliente | video" >&2
    exit 1
    ;;
esac
