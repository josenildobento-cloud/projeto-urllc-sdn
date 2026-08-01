#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Espelha as sondas recebidas de volta pro remetente.
#
# Roda do lado da Rede B (servidor e médico). Captura os TCP destinados à
# porta da sonda e reenvia o mesmo payload invertendo origem/destino, assim
# o sensor calcula o RTT sem precisar de relógios sincronizados.
#
# python3 refletor_urllc.py --porta 9000

import argparse

from scapy.all import IP, TCP, Ether, Raw, sniff, conf

conf.verb = 0

ASSINATURA = b"SONDA1"

# Mesmo mapa fixo de topologia.py / controlador.py. Rede sem ARP (o P4 só
# roteia por IP), então sem o MAC pronto de antemão a resposta ficaria presa
# no timeout de resolução do Scapy. Por isso o quadro Ethernet é montado na
# mão e mandado direto por socket de camada 2.
MACS = {
    "10.0.0.1": "00:00:00:00:00:01",
    "10.0.0.2": "00:00:00:00:00:02",
    "10.0.0.3": "00:00:00:00:00:03",
    "10.0.0.4": "00:00:00:00:00:04",
}


def criar_tratador(porta_sonda, socket_l2, contador):
    """Devolve a função que reflete cada pacote de sonda recebido."""

    def refletir(pacote):
        if not (pacote.haslayer(IP) and pacote.haslayer(TCP)
                and pacote.haslayer(Raw)):
            return
        payload = bytes(pacote[Raw].load)
        if not payload.startswith(ASSINATURA):
            return

        ip_origem = pacote[IP].dst
        ip_destino = pacote[IP].src
        mac_destino = MACS.get(ip_destino, "ff:ff:ff:ff:ff:ff")
        resposta = (Ether(dst=mac_destino) /
                    IP(src=ip_origem, dst=ip_destino) /
                    TCP(sport=porta_sonda, dport=pacote[TCP].sport,
                        flags="PA", seq=1, ack=1) /
                    Raw(load=payload))
        try:
            socket_l2.send(resposta)
            contador["refletidos"] += 1
        except OSError:
            contador["falhas"] += 1

        if contador["refletidos"] % 2000 == 0:
            print("[refletor:%d] %d pacotes refletidos"
                  % (porta_sonda, contador["refletidos"]), flush=True)

    return refletir


def principal():
    analisador = argparse.ArgumentParser(description="Refletor de sondas Scapy")
    analisador.add_argument("--porta", type=int, default=9000)
    argumentos = analisador.parse_args()

    contador = {"refletidos": 0, "falhas": 0}
    socket_l2 = conf.L2socket()
    tratador = criar_tratador(argumentos.porta, socket_l2, contador)

    print("[refletor:%d] aguardando sondas" % argumentos.porta, flush=True)
    sniff(filter="tcp and dst port %d" % argumentos.porta,
          prn=tratador, store=False)


if __name__ == "__main__":
    try:
        principal()
    except KeyboardInterrupt:
        pass
