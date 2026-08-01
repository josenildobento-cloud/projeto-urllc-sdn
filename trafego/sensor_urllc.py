#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Gerador e medidor de tráfego com Scapy.
#
# Manda pacote TCP com número de sequência e o instante de envio no payload.
# O refletor do outro lado devolve o mesmo payload e daí dá pra calcular o
# atraso fim-a-fim (RTT / 2). As métricas agregadas vão pra um JSON Lines
# que o controlador e o painel ficam lendo.
#
# python3 sensor_urllc.py --destino 10.0.0.3 --porta 9000 --classe urllc \
#     --taxa-mbps 2 --saida /tmp/painel_sdn/metricas_urllc.jsonl

import argparse
import json
import os
import struct
import threading
import time

from scapy.all import IP, TCP, Ether, Raw, AsyncSniffer, conf

conf.verb = 0

ASSINATURA = b"SONDA1"
TAMANHO_CABECALHO_SONDA = len(ASSINATURA) + 4 + 8   # assinatura + seq + tempo

# Mesmo mapa fixo de topologia.py e controlador.py. A rede não responde ARP
# (o P4 só roteia por IP, ninguém responde a requisição), então a resolução
# automática do Scapy nunca converge - cada envio fica preso no timeout, e
# o cache do Scapy expira em 120s então isso volta a acontecer de novo.
# Mais simples montar o quadro Ethernet na mão e mandar por socket L2.
MACS = {
    "10.0.0.1": "00:00:00:00:00:01",
    "10.0.0.2": "00:00:00:00:00:02",
    "10.0.0.3": "00:00:00:00:00:03",
    "10.0.0.4": "00:00:00:00:00:04",
}


def montar_payload(sequencia, tamanho):
    """Monta o payload: assinatura + sequência + instante de envio + enchimento."""
    corpo = ASSINATURA + struct.pack("!I", sequencia) + struct.pack(
        "!d", time.time())
    enchimento = max(tamanho - len(corpo), 0)
    return corpo + (b"\x00" * enchimento)


def interpretar_payload(dados):
    """Devolve (sequência, instante_envio) ou None se não for uma sonda."""
    if len(dados) < TAMANHO_CABECALHO_SONDA or not dados.startswith(ASSINATURA):
        return None
    inicio = len(ASSINATURA)
    sequencia = struct.unpack("!I", dados[inicio:inicio + 4])[0]
    instante = struct.unpack("!d", dados[inicio + 4:inicio + 12])[0]
    return sequencia, instante


class ColetorMetricas:
    """Acumula as amostras da janela corrente de forma protegida por lock."""

    def __init__(self):
        self.trava = threading.Lock()
        self.latencias_ms = []
        self.bytes_recebidos = 0
        self.enviados = 0
        self.recebidos = 0
        self.enviados_total = 0
        self.recebidos_total = 0

    def registrar_envio(self):
        with self.trava:
            self.enviados += 1
            self.enviados_total += 1

    def registrar_recebimento(self, latencia_ms, tamanho):
        with self.trava:
            self.latencias_ms.append(latencia_ms)
            self.bytes_recebidos += tamanho
            self.recebidos += 1
            self.recebidos_total += 1

    def fechar_janela(self, duracao):
        """Devolve o resumo da janela e zera os acumuladores."""
        with self.trava:
            amostras = sorted(self.latencias_ms)
            enviados = self.enviados
            recebidos = self.recebidos
            bytes_recebidos = self.bytes_recebidos
            self.latencias_ms = []
            self.bytes_recebidos = 0
            self.enviados = 0
            self.recebidos = 0
            totais = (self.enviados_total, self.recebidos_total)

        perdidos = max(enviados - recebidos, 0)
        if amostras:
            media = sum(amostras) / len(amostras)
            indice_p95 = min(int(len(amostras) * 0.95), len(amostras) - 1)
            resumo = {
                "latencia_media_ms": round(media, 3),
                "latencia_minima_ms": round(amostras[0], 3),
                "latencia_maxima_ms": round(amostras[-1], 3),
                "latencia_p95_ms": round(amostras[indice_p95], 3),
            }
        else:
            resumo = {
                "latencia_media_ms": 0.0,
                "latencia_minima_ms": 0.0,
                "latencia_maxima_ms": 0.0,
                "latencia_p95_ms": 0.0,
            }

        resumo.update({
            "throughput_mbps": round((bytes_recebidos * 8) / duracao / 1e6, 3),
            "pacotes_enviados": enviados,
            "pacotes_recebidos": recebidos,
            "pacotes_perdidos": perdidos,
            "perda_percentual": round(
                (perdidos / enviados * 100) if enviados else 0.0, 2),
            "enviados_acumulado": totais[0],
            "recebidos_acumulado": totais[1],
        })
        return resumo


def iniciar_recepcao(coletor, porta_sonda, dividir_rtt):
    """Sobe o sniffer assíncrono que capta as respostas do refletor."""

    def tratar_pacote(pacote):
        if not pacote.haslayer(TCP) or not pacote.haslayer(Raw):
            return
        interpretado = interpretar_payload(bytes(pacote[Raw].load))
        if not interpretado:
            return
        _, instante_envio = interpretado
        atraso_ms = (time.time() - instante_envio) * 1000.0
        if dividir_rtt:
            atraso_ms /= 2.0
        coletor.registrar_recebimento(atraso_ms, len(pacote))

    sniffer = AsyncSniffer(filter="tcp and src port %d" % porta_sonda,
                           prn=tratar_pacote, store=False)
    sniffer.start()
    return sniffer


def iniciar_envio(coletor, destino, porta_sonda, taxa_mbps, tamanho):
    """Envia pacotes TCP na taxa configurada até o processo ser encerrado."""
    if taxa_mbps <= 0:
        return None

    pacotes_por_segundo = max((taxa_mbps * 1e6) / (tamanho * 8), 1.0)
    intervalo = 1.0 / pacotes_por_segundo
    mac_destino = MACS.get(destino, "ff:ff:ff:ff:ff:ff")
    socket_l2 = conf.L2socket()

    def laco():
        sequencia = 0
        proximo = time.perf_counter()
        while True:
            pacote = (Ether(dst=mac_destino) /
                      IP(dst=destino) /
                      TCP(sport=40000 + (sequencia % 1000),
                          dport=porta_sonda, flags="PA",
                          seq=1, ack=1) /
                      Raw(load=montar_payload(sequencia, tamanho)))
            try:
                socket_l2.send(pacote)
                coletor.registrar_envio()
            except OSError:
                pass
            sequencia = (sequencia + 1) % 0xFFFFFFFF
            proximo += intervalo
            atraso = proximo - time.perf_counter()
            if atraso > 0:
                time.sleep(atraso)
            else:
                proximo = time.perf_counter()

    thread = threading.Thread(target=laco, daemon=True)
    thread.start()
    return thread


def principal():
    analisador = argparse.ArgumentParser(
        description="Sonda de latência e throughput baseada em Scapy")
    analisador.add_argument("--destino", required=True)
    analisador.add_argument("--porta", type=int, default=9000)
    analisador.add_argument("--classe", default="urllc")
    analisador.add_argument("--taxa-mbps", type=float, default=2.0)
    analisador.add_argument("--tamanho-pacote", type=int, default=512)
    analisador.add_argument("--janela", type=float, default=0.25,
                            help="Janela de agregação em segundos")
    analisador.add_argument("--saida", required=True)
    analisador.add_argument("--rtt-completo", action="store_true",
                            help="Reporta o RTT inteiro em vez de RTT/2")
    argumentos = analisador.parse_args()

    os.makedirs(os.path.dirname(argumentos.saida), exist_ok=True)

    coletor = ColetorMetricas()
    iniciar_recepcao(coletor, argumentos.porta, not argumentos.rtt_completo)
    time.sleep(0.5)
    iniciar_envio(coletor, argumentos.destino, argumentos.porta,
                  argumentos.taxa_mbps, argumentos.tamanho_pacote)

    print("[sensor:%s] enviando para %s:%d a %.2f Mbps"
          % (argumentos.classe, argumentos.destino, argumentos.porta,
             argumentos.taxa_mbps), flush=True)

    with open(argumentos.saida, "a", buffering=1) as arquivo:
        while True:
            inicio = time.time()
            time.sleep(argumentos.janela)
            duracao = time.time() - inicio

            resumo = coletor.fechar_janela(duracao)
            resumo["instante"] = time.time()
            resumo["classe"] = argumentos.classe
            resumo["destino"] = argumentos.destino
            arquivo.write(json.dumps(resumo, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        principal()
    except KeyboardInterrupt:
        pass
