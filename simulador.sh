#!/usr/bin/env bash
# Liga/desliga toda a simulação com um comando só.
#
#   bash simulador.sh start [cenario]   # padrão: cenário 1
#   bash simulador.sh stop
#   bash simulador.sh status
#
# Sobe a topologia Mininet+BMv2+controlador (precisa de sudo) em modo
# daemon e o painel Flask, cada um em segundo plano com log próprio em
# /tmp/painel_sdn/. "stop" encerra os dois e faz a faxina do Mininet
# (equivale a "sudo mn -c").
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR_DADOS=/tmp/painel_sdn
PID_TOPOLOGIA="$DIR_DADOS/topologia.pid"
PID_PAINEL="$DIR_DADOS/painel.pid"
JSON_P4="$RAIZ/p4/build/urllc.json"
URL_PAINEL="http://localhost:5000"


# o processo da topologia roda como root (sudo). "kill -0" sem sudo num pid
# de outro usuário dá "permissão negada", que fica igual a "não existe" -
# por isso precisa de sudo aqui também, senão "start" acha que não tem nada
# rodando e sobe um segundo Mininet em cima do primeiro
esta_rodando() {
    local arquivo_pid="$1" usar_sudo="${2:-}"
    [ -f "$arquivo_pid" ] || return 1
    local pid
    pid="$(cat "$arquivo_pid")"
    if [ "$usar_sudo" = "sudo" ]; then
        sudo kill -0 "$pid" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

parar_pid() {
    local arquivo_pid="$1" usar_sudo="$2" nome="$3"
    if ! esta_rodando "$arquivo_pid" "$usar_sudo"; then
        rm -f "$arquivo_pid"
        return
    fi
    local pid
    pid="$(cat "$arquivo_pid")"
    echo ">>> Parando $nome (pid $pid)..."
    if [ "$usar_sudo" = "sudo" ]; then
        sudo kill -TERM "$pid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for _ in $(seq 1 20); do
        esta_rodando "$arquivo_pid" "$usar_sudo" || break
        sleep 1
    done
    rm -f "$arquivo_pid"
}

iniciar() {
    local cenario="${1:-1}"
    mkdir -p "$DIR_DADOS"

    if esta_rodando "$PID_TOPOLOGIA" sudo; then
        echo "O simulador já está em execução (topologia pid $(cat "$PID_TOPOLOGIA"))."
        echo "Painel em $URL_PAINEL"
        exit 0
    fi

    if [ ! -f "$JSON_P4" ]; then
        echo ">>> P4 ainda não compilado, compilando..."
        bash "$RAIZ/p4/compilar.sh"
    fi

    echo ">>> Iniciando topologia (Mininet + BMv2 + controlador), cenário $cenario..."
    sudo bash -c "
        nohup python3 '$RAIZ/topologia/topologia.py' --cenario $cenario --daemon \
            > '$DIR_DADOS/topologia.log' 2>&1 &
        echo \$! > '$PID_TOPOLOGIA'
    "

    echo ">>> Aguardando a rede estabilizar..."
    sleep 10

    echo ">>> Iniciando o painel..."
    nohup python3 "$RAIZ/painel/servidor.py" > "$DIR_DADOS/painel.log" 2>&1 &
    echo $! > "$PID_PAINEL"

    sleep 1
    echo
    echo "Simulador em execução. Painel: $URL_PAINEL"
    echo "Logs em $DIR_DADOS/{topologia,painel}.log"
    echo "Para parar: bash simulador.sh stop"
}

parar() {
    parar_pid "$PID_PAINEL" "" "painel"
    parar_pid "$PID_TOPOLOGIA" "sudo" "topologia"

    echo ">>> Faxina de segurança (processos e estado residual do Mininet)..."
    sudo pkill -f "topologia/topologia.py" 2>/dev/null || true
    sudo pkill -f "controlador/controlador.py" 2>/dev/null || true
    sudo pkill -f sensor_urllc.py 2>/dev/null || true
    sudo pkill -f refletor_urllc.py 2>/dev/null || true
    sudo pkill -f simple_switch 2>/dev/null || true
    sudo pkill iperf3 2>/dev/null || true
    sudo pkill tcpdump 2>/dev/null || true
    sudo mn -c > /dev/null 2>&1 || true

    echo "Simulador parado."
}

status() {
    if esta_rodando "$PID_TOPOLOGIA" sudo; then
        echo "topologia: em execução (pid $(cat "$PID_TOPOLOGIA"))"
    else
        echo "topologia: parada"
    fi
    if esta_rodando "$PID_PAINEL"; then
        echo "painel:    em execução (pid $(cat "$PID_PAINEL")) — $URL_PAINEL"
    else
        echo "painel:    parado"
    fi
}

case "${1:-}" in
    start)  iniciar "${2:-1}" ;;
    stop)   parar ;;
    status) status ;;
    *)
        echo "Uso: $0 {start [cenario]|stop|status}"
        exit 1
        ;;
esac
