#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Emulação da rede de transporte 5G com switches P4 (BMv2) no Mininet.
#
#   Rede A                Rede de Transporte              Rede B
#                        +---- s2 ----+   (principal)
#   robo --+              |            |           +-- servidor
#          +--- s1 ------+            +--- s4 -----+
#   pc   --+              |            |           +-- medico
#                        +---- s3 ----+   (backup)
#
# sudo python3 topologia/topologia.py --cenario 4

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from mininet.net import Mininet
from mininet.node import Host, Switch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_DADOS = "/tmp/painel_sdn"
ARQUIVO_JSON_P4 = os.path.join(RAIZ, "p4", "build", "urllc.json")
PORTA_API = 8090

# Subrede única, os switches P4 fazem encaminhamento por IP
HOSTS = {
    "robo":     {"ip": "10.0.0.1/24", "mac": "00:00:00:00:00:01"},
    "pc":       {"ip": "10.0.0.2/24", "mac": "00:00:00:00:00:02"},
    "servidor": {"ip": "10.0.0.3/24", "mac": "00:00:00:00:00:03"},
    "medico":   {"ip": "10.0.0.4/24", "mac": "00:00:00:00:00:04"},
}

PORTAS_THRIFT = {"s1": 9091, "s2": 9092, "s3": 9093, "s4": 9094}

# Parâmetros vivos da simulação: alterados em tempo real pelo painel, sem
# precisar reiniciar a topologia (ver rotas /cenario e /parametros).
ESTADO_SIMULACAO = {
    "cenario": 1,
    "banda_principal": 10.0,
    "banda_backup": 5.0,
    "limite_robo": 2.0,
    "limite_background": 20.0,
    "limiar_ms": 5.0,
}


class SwitchP4(Switch):
    """Switch de software BMv2 (simple_switch) executando o programa P4."""

    def __init__(self, nome, arquivo_json=None, porta_thrift=None,
                 id_dispositivo=0, **kwargs):
        Switch.__init__(self, nome, **kwargs)
        self.arquivo_json = arquivo_json
        self.porta_thrift = porta_thrift
        self.id_dispositivo = id_dispositivo
        self.arquivo_log = os.path.join(DIR_DADOS, "bmv2_%s.log" % nome)

    @classmethod
    def setup(cls):
        pass

    def start(self, controllers=None):
        argumentos = [
            "simple_switch",
            "--device-id", str(self.id_dispositivo),
            "--thrift-port", str(self.porta_thrift),
        ]
        for numero, interface in self.intfs.items():
            if not interface.IP():
                argumentos.extend(["-i", "%d@%s" % (numero, interface.name)])
        argumentos.extend(["--log-level", "error", self.arquivo_json])
        # --priority-queues é do alvo simple_switch, não do bmv2 geral, por
        # isso vem depois do "--". Sem isso o BMv2 usa 1 fila por porta e
        # todo pacote com prioridade > 0 (definir_prioridade) é descartado
        # de forma silenciosa na saída - já caí nessa uma vez.
        argumentos.extend(["--", "--priority-queues", "8"])

        comando = " ".join(argumentos) + " > %s 2>&1 &" % self.arquivo_log
        info("[topologia] iniciando %s: %s\n" % (self.name, comando))
        self.cmd(comando)
        time.sleep(1)

    def stop(self, deleteIntfs=True):
        self.cmd("kill %simple_switch")
        self.cmd("wait")
        Switch.stop(self, deleteIntfs)


def construir_rede(banda_principal, banda_backup, atraso_principal,
                   atraso_backup):
    """Cria a topologia e devolve o objeto Mininet já iniciado."""
    rede = Mininet(controller=None, link=TCLink, autoSetMacs=False)

    # roteadores da rede de transporte
    switches = {}
    for indice, nome in enumerate(["s1", "s2", "s3", "s4"]):
        switches[nome] = rede.addSwitch(
            nome, cls=SwitchP4,
            arquivo_json=ARQUIVO_JSON_P4,
            porta_thrift=PORTAS_THRIFT[nome],
            id_dispositivo=indice,
        )

    hosts = {}
    for nome, config in HOSTS.items():
        hosts[nome] = rede.addHost(nome, cls=Host,
                                   ip=config["ip"], mac=config["mac"])

    # a ordem dos addLink define a numeração das portas no BMv2
    # Rede A -> s1  (portas 1 e 2)
    rede.addLink(hosts["robo"], switches["s1"], port1=0, port2=1,
                 bw=1000, delay="0.05ms")
    rede.addLink(hosts["pc"], switches["s1"], port1=0, port2=2,
                 bw=1000, delay="0.05ms")

    # s1 -> s2 (principal, porta 3) e s1 -> s3 (backup, porta 4)
    rede.addLink(switches["s1"], switches["s2"], port1=3, port2=1,
                 bw=banda_principal, delay=atraso_principal, max_queue_size=200)
    rede.addLink(switches["s1"], switches["s3"], port1=4, port2=1,
                 bw=banda_backup, delay=atraso_backup, max_queue_size=200)

    # s2 -> s4 (porta 1 de s4) e s3 -> s4 (porta 2 de s4)
    rede.addLink(switches["s2"], switches["s4"], port1=2, port2=1,
                 bw=banda_principal, delay=atraso_principal, max_queue_size=200)
    rede.addLink(switches["s3"], switches["s4"], port1=2, port2=2,
                 bw=banda_backup, delay=atraso_backup, max_queue_size=200)

    # s4 -> Rede B (portas 3 e 4)
    rede.addLink(switches["s4"], hosts["servidor"], port1=3, port2=0,
                 bw=1000, delay="0.05ms")
    rede.addLink(switches["s4"], hosts["medico"], port1=4, port2=0,
                 bw=1000, delay="0.05ms")

    rede.start()
    return rede


def preparar_hosts(rede):
    """ARP estático, desligamento de offloads e supressão de RST do kernel."""
    for nome, config in HOSTS.items():
        no = rede.get(nome)
        interface = no.defaultIntf().name

        # offload atrapalha a montagem de pacote pelo Scapy
        no.cmd("ethtool -K %s tx off rx off sg off tso off gso off gro off "
               "2>/dev/null" % interface)
        no.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1")

        # sem isso o kernel responde RST às sondas (não existe socket aberto)
        no.cmd("iptables -A OUTPUT -p tcp --tcp-flags RST RST -j DROP")

        # arp estático pros outros hosts
        for outro, cfg_outro in HOSTS.items():
            if outro == nome:
                continue
            ip_outro = cfg_outro["ip"].split("/")[0]
            no.cmd("arp -s %s %s" % (ip_outro, cfg_outro["mac"]))


def iniciar_captura(rede):
    """Sobe um tcpdump persistente por nó, consumido pelo console do painel."""
    alvos = list(HOSTS.keys()) + ["s1", "s2", "s3", "s4"]
    for nome in alvos:
        no = rede.get(nome)
        interface = (no.defaultIntf().name if nome in HOSTS
                     else "%s-eth1" % nome)
        arquivo = os.path.join(DIR_DADOS, "tcpdump_%s.txt" % nome)
        no.cmd("tcpdump -i %s -nn -l -q ip > %s 2>/dev/null &"
               % (interface, arquivo))


class ApiInfraestrutura(BaseHTTPRequestHandler):
    rede = None
    estado_enlaces = {}

    def log_message(self, formato, *args):
        pass

    def _responder(self, dados, codigo=200):
        corpo = json.dumps(dados).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        partes = [p for p in self.path.split("?")[0].split("/") if p]

        if not partes or partes[0] == "estado":
            return self._responder(self.montar_estado())

        if partes[0] == "enlace" and len(partes) == 4:
            return self._responder(self.alterar_enlace(partes[1], partes[2],
                                                       partes[3]))

        if partes[0] == "tcpdump" and len(partes) == 2:
            return self._responder(self.ler_tcpdump(partes[1]))

        if partes[0] == "cenario" and len(partes) == 2:
            return self._responder(self.trocar_cenario(partes[1]))

        return self._responder({"erro": "rota desconhecida"}, 404)

    def do_POST(self):
        partes = [p for p in self.path.split("?")[0].split("/") if p]
        tamanho = int(self.headers.get("Content-Length", 0) or 0)
        corpo = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            dados = json.loads(corpo or b"{}")
        except ValueError:
            dados = {}

        if partes and partes[0] == "parametros":
            return self._responder(self.atualizar_parametros(dados))

        return self._responder({"erro": "rota desconhecida"}, 404)

    def montar_estado(self):
        nos = []
        for nome, config in HOSTS.items():
            nos.append({"nome": nome, "tipo": "host",
                        "ip": config["ip"].split("/")[0]})
        for nome in ["s1", "s2", "s3", "s4"]:
            nos.append({"nome": nome, "tipo": "switch",
                        "thrift": PORTAS_THRIFT.get(nome)})

        enlaces = []
        for enlace in self.rede.links:
            no1 = enlace.intf1.node.name
            no2 = enlace.intf2.node.name
            chave = "%s-%s" % (no1, no2)
            enlaces.append({
                "origem": no1,
                "destino": no2,
                "ativo": self.estado_enlaces.get(chave, True),
            })
        return {"nos": nos, "enlaces": enlaces, "instante": time.time()}

    def alterar_enlace(self, no1, no2, acao):
        if acao not in ("up", "down"):
            return {"erro": "ação inválida (use up ou down)"}
        try:
            self.rede.configLinkStatus(no1, no2, acao)
        except Exception as excecao:            # noqa: BLE001
            return {"erro": str(excecao)}
        self.estado_enlaces["%s-%s" % (no1, no2)] = (acao == "up")
        self.estado_enlaces["%s-%s" % (no2, no1)] = (acao == "up")
        registrar_evento("infraestrutura",
                         "Enlace %s <-> %s alterado para %s" % (no1, no2, acao))
        return {"ok": True, "origem": no1, "destino": no2, "acao": acao}

    def ler_tcpdump(self, nome_no):
        arquivo = os.path.join(DIR_DADOS, "tcpdump_%s.txt" % nome_no)
        if not os.path.exists(arquivo):
            return {"no": nome_no, "linhas": []}
        saida = subprocess.run(["tail", "-n", "60", arquivo],
                               capture_output=True, text=True)
        return {"no": nome_no,
                "linhas": saida.stdout.strip().split("\n")[-60:]}

    def trocar_cenario(self, valor):
        """Ativa outro cenário sem reiniciar a topologia: reconfigura o
        tráfego de fundo aqui e avisa o controlador via comando.json."""
        try:
            numero = int(valor)
        except ValueError:
            return {"erro": "cenário inválido"}
        if numero not in (1, 2, 3, 4):
            return {"erro": "cenário deve ser 1, 2, 3 ou 4"}

        ESTADO_SIMULACAO["cenario"] = numero
        salvar_parametros()
        aplicar_trafego(self.rede)
        publicar_comando(cenario=numero)
        registrar_evento("infraestrutura", "Cenário alterado para %d" % numero)
        return {"ok": True, "cenario": numero}

    def atualizar_parametros(self, dados):
        try:
            banda_principal = float(dados.get(
                "banda_principal", ESTADO_SIMULACAO["banda_principal"]))
            banda_backup = float(dados.get(
                "banda_backup", ESTADO_SIMULACAO["banda_backup"]))
            limite_robo = float(dados.get(
                "limite_robo", ESTADO_SIMULACAO["limite_robo"]))
            limite_background = float(dados.get(
                "limite_background", ESTADO_SIMULACAO["limite_background"]))
            limiar_ms = float(dados.get(
                "limiar_ms", ESTADO_SIMULACAO["limiar_ms"]))
        except (TypeError, ValueError):
            return {"erro": "parâmetros inválidos"}

        reconfigurar_banda(self.rede, banda_principal, banda_backup)
        ESTADO_SIMULACAO.update({
            "banda_principal": banda_principal,
            "banda_backup": banda_backup,
            "limite_robo": limite_robo,
            "limite_background": limite_background,
            "limiar_ms": limiar_ms,
        })
        salvar_parametros()
        aplicar_trafego(self.rede)
        publicar_comando(limiar_ms=limiar_ms)
        registrar_evento(
            "infraestrutura",
            "Parâmetros atualizados: principal %sMbps · backup %sMbps · "
            "robô %sMbps · background %sMbps · SLA %sms"
            % (banda_principal, banda_backup, limite_robo,
               limite_background, limiar_ms))
        return {"ok": True, **ESTADO_SIMULACAO}


def iniciar_api(rede):
    ApiInfraestrutura.rede = rede
    servidor = HTTPServer(("0.0.0.0", PORTA_API), ApiInfraestrutura)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    info("[topologia] API de infraestrutura em http://localhost:%d\n"
         % PORTA_API)


def registrar_evento(origem, mensagem):
    """Grava um evento no log compartilhado com o painel."""
    linha = json.dumps({"instante": time.time(), "origem": origem,
                        "nivel": "info", "mensagem": mensagem},
                       ensure_ascii=False)
    with open(os.path.join(DIR_DADOS, "decisoes_sdn.jsonl"), "a") as arquivo:
        arquivo.write(linha + "\n")


def salvar_parametros():
    """Publica ESTADO_SIMULACAO para o painel ler em /api/estado."""
    caminho = os.path.join(DIR_DADOS, "parametros.json")
    with open(caminho + ".tmp", "w") as arquivo:
        json.dump(ESTADO_SIMULACAO, arquivo)
    os.replace(caminho + ".tmp", caminho)


def publicar_comando(**kwargs):
    """Avisa o controlador (processo separado) de uma mudança de cenário ou
    de SLA. O controlador lê este arquivo a cada iteração do laço."""
    caminho = os.path.join(DIR_DADOS, "comando.json")
    atual = {}
    if os.path.exists(caminho):
        try:
            with open(caminho) as arquivo:
                atual = json.load(arquivo)
        except ValueError:
            atual = {}
    atual.update(kwargs)
    atual["instante"] = time.time()
    with open(caminho + ".tmp", "w") as arquivo:
        json.dump(atual, arquivo)
    os.replace(caminho + ".tmp", caminho)


def reconfigurar_banda(rede, banda_principal, banda_backup):
    """Reaplica a banda dos enlaces principal/backup via tc, sem religar
    os switches P4."""
    pares_principal = ({"s1", "s2"}, {"s2", "s4"})
    pares_backup = ({"s1", "s3"}, {"s3", "s4"})
    for enlace in rede.links:
        par = {enlace.intf1.node.name, enlace.intf2.node.name}
        if par in pares_principal:
            nova_banda = banda_principal
        elif par in pares_backup:
            nova_banda = banda_backup
        else:
            continue
        enlace.intf1.config(bw=nova_banda)
        enlace.intf2.config(bw=nova_banda)


def iniciar_refletores(rede):
    """Sobe os refletores (respondem às sondas pra medir RTT). Só roda uma
    vez no boot, já que são apenas listeners e não dependem de cenário."""
    servidor = rede.get("servidor")
    medico = rede.get("medico")
    caminho_trafego = os.path.join(RAIZ, "trafego")

    servidor.cmd("python3 %s/refletor_urllc.py --porta 9000 > "
                 "%s/refletor_urllc.log 2>&1 &"
                 % (caminho_trafego, DIR_DADOS))
    medico.cmd("python3 %s/refletor_urllc.py --porta 9100 > "
               "%s/refletor_embb.log 2>&1 &" % (caminho_trafego, DIR_DADOS))
    time.sleep(2)


def parar_trafego(rede):
    """Encerra sondas e iperf3 antes de reiniciá-los com novos parâmetros."""
    rede.get("robo").cmd("pkill -f sensor_urllc.py")
    rede.get("pc").cmd("pkill -f sensor_urllc.py")
    rede.get("pc").cmd("pkill -f 'iperf3 -c'")
    rede.get("medico").cmd("pkill -f 'iperf3 -s'")
    time.sleep(0.3)


def aplicar_trafego(rede):
    """(Re)inicia as sondas uRLLC e o tráfego de fundo eMBB conforme
    ESTADO_SIMULACAO. Chamada no boot e sempre que o painel troca de
    cenário ou altera parâmetros, sem precisar reiniciar a topologia."""
    parar_trafego(rede)

    robo = rede.get("robo")
    pc = rede.get("pc")
    medico = rede.get("medico")
    caminho_trafego = os.path.join(RAIZ, "trafego")

    cenario = ESTADO_SIMULACAO["cenario"]
    limite_robo = ESTADO_SIMULACAO["limite_robo"]
    limite_background = ESTADO_SIMULACAO["limite_background"]

    # sonda uRLLC: robô -> servidor, é ela que mede a latência fim-a-fim
    robo.cmd("python3 %s/sensor_urllc.py --destino 10.0.0.3 --porta 9000 "
             "--classe urllc --taxa-mbps %s --saida %s/metricas_urllc.jsonl "
             "> %s/sensor_urllc.log 2>&1 &"
             % (caminho_trafego, limite_robo, DIR_DADOS, DIR_DADOS))

    # sonda de referência do tráfego comum (pc -> medico)
    pc.cmd("python3 %s/sensor_urllc.py --destino 10.0.0.4 --porta 9100 "
           "--classe embb --taxa-mbps 0.5 --saida %s/metricas_embb.jsonl "
           "> %s/sensor_embb.log 2>&1 &"
           % (caminho_trafego, DIR_DADOS, DIR_DADOS))

    # eMBB volumoso via iperf3, não roda no cenário 1
    if cenario >= 2:
        medico.cmd("iperf3 -s -p 5201 > %s/iperf_servidor.log 2>&1 &"
                   % DIR_DADOS)
        time.sleep(1)
        pc.cmd("iperf3 -c 10.0.0.4 -p 5201 -t 3600 -b %sM -P 4 "
               "> %s/iperf_cliente.log 2>&1 &" % (limite_background, DIR_DADOS))

    registrar_evento("trafego",
                     "Cargas ativas (cenário %d): robô %s Mbps, "
                     "background %s Mbps" % (cenario, limite_robo,
                                             limite_background))


def iniciar_controlador(cenario, limiar_ms):
    caminho = os.path.join(RAIZ, "controlador", "controlador.py")
    comando = ("python3 %s --cenario %d --limiar-ms %s > %s/controlador.log "
               "2>&1 &" % (caminho, cenario, limiar_ms, DIR_DADOS))
    subprocess.Popen(comando, shell=True)
    info("[topologia] controlador iniciado (cenário %d)\n" % cenario)


def principal():
    analisador = argparse.ArgumentParser(
        description="Simulador SDN/P4 para tráfego uRLLC em rede 5G")
    analisador.add_argument("--cenario", type=int, default=1, choices=[1, 2, 3, 4],
                            help="1=normal 2=congestão 3=reroute 4=QoS dinâmico")
    analisador.add_argument("--banda-principal", type=float, default=10.0,
                            help="Banda do link principal em Mbps")
    analisador.add_argument("--banda-backup", type=float, default=5.0,
                            help="Banda do link de backup em Mbps")
    analisador.add_argument("--limite-robo", type=float, default=2.0,
                            help="Limite de tráfego do robô em Mbps")
    analisador.add_argument("--limite-background", type=float, default=20.0,
                            help="Limite de tráfego de background em Mbps")
    analisador.add_argument("--limiar-ms", type=float, default=5.0,
                            help="SLA de latência máxima do robô em ms")
    analisador.add_argument("--atraso-principal", default="0.5ms")
    analisador.add_argument("--atraso-backup", default="2ms")
    analisador.add_argument("--daemon", action="store_true",
                            help="Roda sem o prompt interativo do Mininet; "
                                 "encerra ao receber SIGTERM (usado pelo "
                                 "simulador.sh).")
    argumentos = analisador.parse_args()

    if os.geteuid() != 0:
        sys.exit("Execute com sudo.")
    if not os.path.exists(ARQUIVO_JSON_P4):
        sys.exit("Compile o P4 primeiro: bash p4/compilar.sh")

    os.makedirs(DIR_DADOS, exist_ok=True)
    for arquivo in ["metricas_urllc.jsonl", "metricas_embb.jsonl",
                    "decisoes_sdn.jsonl", "comando.json"]:
        open(os.path.join(DIR_DADOS, arquivo), "w").close()

    ESTADO_SIMULACAO.update({
        "cenario": argumentos.cenario,
        "banda_principal": argumentos.banda_principal,
        "banda_backup": argumentos.banda_backup,
        "limite_robo": argumentos.limite_robo,
        "limite_background": argumentos.limite_background,
        "limiar_ms": argumentos.limiar_ms,
    })
    salvar_parametros()

    setLogLevel("info")
    rede = construir_rede(argumentos.banda_principal, argumentos.banda_backup,
                          argumentos.atraso_principal, argumentos.atraso_backup)
    preparar_hosts(rede)
    iniciar_captura(rede)
    iniciar_api(rede)

    info("[topologia] aguardando os switches P4 estabilizarem...\n")
    time.sleep(3)

    iniciar_controlador(argumentos.cenario, argumentos.limiar_ms)
    time.sleep(4)
    iniciar_refletores(rede)
    aplicar_trafego(rede)

    info("\n=== Cenário %d em execução. ===\n\n" % argumentos.cenario)

    if argumentos.daemon:
        parar_evento = threading.Event()

        def _tratar_sinal(signum, frame):        # noqa: ARG001
            parar_evento.set()

        signal.signal(signal.SIGTERM, _tratar_sinal)
        signal.signal(signal.SIGINT, _tratar_sinal)
        info("[topologia] modo daemon: aguardando SIGTERM para encerrar\n")
        parar_evento.wait()
    else:
        CLI(rede)

    rede.stop()
    subprocess.run("pkill -f controlador.py; pkill -f sensor_urllc; "
                   "pkill -f refletor_urllc; pkill iperf3; pkill tcpdump",
                   shell=True)


if __name__ == "__main__":
    principal()
