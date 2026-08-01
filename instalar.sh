#!/usr/bin/env bash
# Prepara um Ubuntu 22.04 limpo pra rodar o projeto.
#   bash instalar.sh
# Instala p4c, BMv2, Mininet, iperf3, tcpdump, Scapy e Flask.
set -euo pipefail

echo ">>> [1/5] Pacotes base do sistema"
sudo apt-get update
sudo apt-get install -y \
    curl gnupg ca-certificates lsb-release \
    python3 python3-pip python3-dev \
    mininet iperf3 tcpdump ethtool net-tools bridge-utils

echo ">>> [2/5] Repositório oficial do p4lang"
. /etc/os-release
echo "deb http://download.opensuse.org/repositories/home:/p4lang/xUbuntu_${VERSION_ID}/ /" \
    | sudo tee /etc/apt/sources.list.d/home_p4lang.list > /dev/null
curl -fsSL "https://download.opensuse.org/repositories/home:p4lang/xUbuntu_${VERSION_ID}/Release.key" \
    | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_p4lang.gpg > /dev/null

echo ">>> [3/5] Compilador P4 e switch BMv2"
sudo apt-get update
sudo apt-get install -y p4lang-p4c p4lang-bmv2

echo ">>> [4/5] Bibliotecas Python"
pip3 install --user --upgrade "scapy>=2.5.0" "flask>=2.3" "mininet"

echo ">>> [5/5] Verificação"
for binario in p4c simple_switch simple_switch_CLI mn iperf3 tcpdump; do
    if command -v "$binario" > /dev/null; then
        echo "  ok   $binario"
    else
        echo "  ERRO $binario não encontrado"
    fi
done

python3 -c "import scapy, flask; print('  ok   scapy', scapy.__version__)"

echo
echo "Concluído. Próximo passo:"
echo "  bash p4/compilar.sh"
echo "  sudo python3 topologia/topologia.py --cenario 4"
