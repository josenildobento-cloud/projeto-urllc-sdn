#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Plano de controle SDN (closed loop) da rede de transporte.
#
# A cada iteração: mede a latência do robô, compara com o SLA, decide se
# precisa atuar e reescreve as tabelas do P4. Os níveis de atuação são
# escalonados com histerese (sobe rápido, desce devagar):
#   0 - QoS base (uRLLC na fila expressa, eMBB na fila de fundo)
#   1 - eMBB desviado para o link de backup
#   2 - eMBB descartado no ingresso dos switches de borda
#
# python3 controlador/controlador.py --cenario 4 --limiar-ms 5

import argparse
import collections
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cliente_p4 import ClienteP4                      # noqa: E402

DIR_DADOS = "/tmp/painel_sdn"
API_INFRA = "http://127.0.0.1:8090"

CLASSE_URLLC = 1
CLASSE_EMBB = 2

PORTAS_THRIFT = {"s1": 9091, "s2": 9092, "s3": 9093, "s4": 9094}

MACS = {
    "10.0.0.1": "00:00:00:00:00:01",   # robô
    "10.0.0.2": "00:00:00:00:00:02",   # pc
    "10.0.0.3": "00:00:00:00:00:03",   # servidor
    "10.0.0.4": "00:00:00:00:00:04",   # médico
}

# Portas de saída de cada switch para cada destino final
ENCAMINHAMENTO = {
    "s1": {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3, "10.0.0.4": 3},
    "s2": {"10.0.0.1": 1, "10.0.0.2": 1, "10.0.0.3": 2, "10.0.0.4": 2},
    "s3": {"10.0.0.1": 1, "10.0.0.2": 1, "10.0.0.3": 2, "10.0.0.4": 2},
    "s4": {"10.0.0.1": 1, "10.0.0.2": 1, "10.0.0.3": 3, "10.0.0.4": 4},
}

# Portas que levam ao link principal / backup nos switches de borda
CAMINHO_PRINCIPAL = {"s1": 3, "s4": 1}
CAMINHO_BACKUP = {"s1": 4, "s4": 2}

# Destinos que cada switch de borda alcança pela rede de transporte
DESTINOS_REMOTOS = {
    "s1": ["10.0.0.3", "10.0.0.4"],
    "s4": ["10.0.0.1", "10.0.0.2"],
}


def registrar_decisao(nivel, mensagem, detalhes=None):
    """Grava uma linha no log de decisões SDN exibido pelo painel."""
    registro = {
        "instante": time.time(),
        "origem": "controlador",
        "nivel": nivel,
        "mensagem": mensagem,
        "detalhes": detalhes or {},
    }
    caminho = os.path.join(DIR_DADOS, "decisoes_sdn.jsonl")
    with open(caminho, "a") as arquivo:
        arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")
    print("[%s] %s" % (nivel, mensagem), flush=True)


def ler_ultima_metrica(arquivo):
    """Lê a última linha JSON de um arquivo de métricas do gerador de tráfego."""
    caminho = os.path.join(DIR_DADOS, arquivo)
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, "rb") as entrada:
            entrada.seek(0, os.SEEK_END)
            tamanho = entrada.tell()
            bloco = min(4096, tamanho)
            entrada.seek(tamanho - bloco)
            linhas = entrada.read().decode("utf-8", "ignore").strip().split("\n")
        for linha in reversed(linhas):
            linha = linha.strip()
            if linha.startswith("{"):
                return json.loads(linha)
    except (ValueError, OSError):
        return None
    return None


def consultar_infraestrutura():
    """Consulta o estado dos enlaces na API da topologia."""
    try:
        with urllib.request.urlopen("%s/estado" % API_INFRA, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:                                  # noqa: BLE001
        return None


def enlace_principal_ativo(estado):
    if not estado:
        return True
    for enlace in estado.get("enlaces", []):
        par = {enlace["origem"], enlace["destino"]}
        if par == {"s1", "s2"} or par == {"s2", "s4"}:
            if not enlace["ativo"]:
                return False
    return True


class ControladorSDN:

    def __init__(self, cenario, limiar_ms, intervalo=0.5):
        self.cenario = cenario
        self.limiar_ms = limiar_ms
        self.intervalo = intervalo
        self.nivel_atuacao = 0
        self.caminho_urllc = "principal"
        self.caminho_embb = "principal"
        self.historico_latencia = collections.deque(maxlen=6)
        self.amostras_ok = 0
        self.telemetria_anterior = {}
        self.instante_anterior = time.time()
        self.ultimo_comando = 0.0
        self.switches = {nome: ClienteP4(nome, porta)
                         for nome, porta in PORTAS_THRIFT.items()}

    def instalar_regras_base(self):
        """Classificação + encaminhamento em todos os switches."""
        for nome, switch in self.switches.items():
            switch.limpar_tabela("tabela_classificacao")
            switch.limpar_tabela("tabela_encaminhamento")
            switch.limpar_tabela("tabela_caminho")
            switch.limpar_tabela("tabela_politica")

            self.instalar_classificacao(switch)

            for ip, porta in ENCAMINHAMENTO[nome].items():
                switch.adicionar_regra(
                    "tabela_encaminhamento",
                    ["%s/32" % ip],
                    "encaminhar",
                    [porta, MACS[ip]],
                    apelido="fwd_%s" % ip,
                )
        registrar_decisao("info", "Regras base instaladas nos 4 roteadores P4")

    @staticmethod
    def instalar_classificacao(switch):
        """Mapeia portas TCP para as classes de tráfego (chaves ternárias)."""
        curinga = "0&&&0x0"
        tcp = "6&&&0xff"

        regras = [
            # (porta_origem, porta_destino, classe, dscp, prioridade)
            ("9000&&&0xffff", curinga, CLASSE_URLLC, 46, 20),
            (curinga, "9000&&&0xffff", CLASSE_URLLC, 46, 20),
            ("9100&&&0xffff", curinga, CLASSE_EMBB, 10, 15),
            (curinga, "9100&&&0xffff", CLASSE_EMBB, 10, 15),
            ("5201&&&0xffff", curinga, CLASSE_EMBB, 10, 15),
            (curinga, "5201&&&0xffff", CLASSE_EMBB, 10, 15),
        ]
        for origem, destino, classe, dscp, prioridade in regras:
            switch.adicionar_regra(
                "tabela_classificacao",
                [origem, destino, tcp],
                "marcar_classe",
                [classe, dscp],
                prioridade=prioridade,
            )

    def aplicar_qos(self, prioridade_urllc=7, prioridade_embb=1):
        """Coloca o uRLLC na fila expressa e o eMBB na fila de fundo."""
        for switch in self.switches.values():
            switch.limpar_tabela("tabela_politica")
            switch.adicionar_regra("tabela_politica", [CLASSE_URLLC],
                                   "definir_prioridade", [prioridade_urllc],
                                   apelido="pol_urllc")
            switch.adicionar_regra("tabela_politica", [CLASSE_EMBB],
                                   "definir_prioridade", [prioridade_embb],
                                   apelido="pol_embb")

    def descartar_embb(self, ativar):
        """Liga ou desliga o policiamento por descarte da classe eMBB."""
        for nome in ("s1", "s4"):
            switch = self.switches[nome]
            if ativar:
                switch.modificar_regra("pol_embb", "descartar_pacote", [])
            else:
                switch.modificar_regra("pol_embb", "definir_prioridade", [1])
        self.registrar_atuacao(
            "Policiamento do eMBB %s nos roteadores de borda"
            % ("ATIVADO" if ativar else "desativado"))

    def definir_caminho(self, classe, destino_caminho):
        """Direciona uma classe pelo link principal ou pelo link de backup."""
        mapa = (CAMINHO_BACKUP if destino_caminho == "backup"
                else CAMINHO_PRINCIPAL)
        nome_classe = "urllc" if classe == CLASSE_URLLC else "embb"

        for nome in ("s1", "s4"):
            switch = self.switches[nome]
            for ip in DESTINOS_REMOTOS[nome]:
                apelido = "cam_%s_%s_%s" % (nome, nome_classe, ip)
                switch.remover_regra(apelido)
                switch.adicionar_regra(
                    "tabela_caminho",
                    [classe, "%s/32" % ip],
                    "definir_caminho",
                    [mapa[nome], MACS[ip]],
                    apelido=apelido,
                )
        if classe == CLASSE_URLLC:
            self.caminho_urllc = destino_caminho
        else:
            self.caminho_embb = destino_caminho

        self.registrar_atuacao("Classe %s desviada para o link %s"
                               % (nome_classe.upper(), destino_caminho))

    def registrar_atuacao(self, mensagem):
        registrar_decisao("atuacao", mensagem, {
            "nivel_atuacao": self.nivel_atuacao,
            "caminho_urllc": self.caminho_urllc,
            "caminho_embb": self.caminho_embb,
        })

    def configurar_cenario(self):
        descricoes = {
            1: "Operação normal: roteamento estático pelo link principal, "
               "sem QoS e sem laço fechado",
            2: "Congestão extrema: sem QoS, tudo pelo link principal "
               "(evidencia a violação do SLA)",
            3: "Auto-reroute: uRLLC no link principal, eMBB no link de backup",
            4: "QoS + SDN dinâmico: laço fechado com escalonamento de atuações",
        }
        registrar_decisao("info", "Cenário %d - %s"
                          % (self.cenario, descricoes[self.cenario]))

        if self.cenario == 3:
            self.definir_caminho(CLASSE_EMBB, "backup")
        elif self.cenario == 4:
            self.aplicar_qos()
            self.registrar_atuacao("QoS base aplicada: uRLLC fila 7, eMBB fila 1")

    def coletar_telemetria_p4(self):
        """Lê os registradores do P4 e converte bytes em Mbps."""
        agora = time.time()
        decorrido = max(agora - self.instante_anterior, 1e-3)
        resultado = {}

        for nome, switch in self.switches.items():
            atual = switch.coletar_telemetria((CLASSE_URLLC, CLASSE_EMBB))
            anterior = self.telemetria_anterior.get(nome, {})
            for classe, valores in atual.items():
                bytes_antes = anterior.get(classe, {}).get("bytes", valores["bytes"])
                delta = max(valores["bytes"] - bytes_antes, 0)
                valores["throughput_mbps"] = round(
                    (delta * 8) / decorrido / 1e6, 3)
            resultado[nome] = atual
            self.telemetria_anterior[nome] = atual

        self.instante_anterior = agora
        return resultado

    def publicar_estado(self, urllc, embb, telemetria, motivo):
        estado = {
            "instante": time.time(),
            "cenario": self.cenario,
            "limiar_ms": self.limiar_ms,
            "nivel_atuacao": self.nivel_atuacao,
            "caminho_urllc": self.caminho_urllc,
            "caminho_embb": self.caminho_embb,
            "ultimo_motivo": motivo,
            "robo": urllc or {},
            "background": embb or {},
            "telemetria_p4": telemetria,
        }
        caminho = os.path.join(DIR_DADOS, "estado_painel.json")
        with open(caminho + ".tmp", "w") as arquivo:
            json.dump(estado, arquivo, ensure_ascii=False)
        os.replace(caminho + ".tmp", caminho)

    def avaliar_sla(self, metrica_urllc):
        # Se a violação for leve (< 1,5x o limiar) escalona um nível por vez.
        # Se for severa, pula direto pro nível máximo (reroute + policiamento
        # juntos) em vez de esperar um ciclo inteiro só no reroute - ganha
        # uma amostra de reação quando o congestionamento já está feio.
        if not metrica_urllc:
            return "sem amostras de latência"

        latencia = metrica_urllc.get("latencia_media_ms", 0.0)
        self.historico_latencia.append(latencia)

        violando = latencia > self.limiar_ms
        violacao_severa = latencia > (self.limiar_ms * 1.5)
        confortavel = latencia < (self.limiar_ms * 0.7)

        if violando:
            self.amostras_ok = 0

            if self.nivel_atuacao < 2 and (violacao_severa or self.nivel_atuacao == 1):
                self.nivel_atuacao = 2
                self.definir_caminho(CLASSE_EMBB, "backup")
                self.descartar_embb(True)
                return ("latência %.2f ms: reroute + policiamento do eMBB "
                        "ativados de uma vez" % latencia)

            if self.nivel_atuacao == 0:
                self.nivel_atuacao = 1
                self.definir_caminho(CLASSE_EMBB, "backup")
                return ("latência %.2f ms acima do SLA: eMBB desviado para o "
                        "backup" % latencia)

            return "latência %.2f ms - nível máximo de atuação" % latencia

        if confortavel:
            self.amostras_ok += 1
            # Relaxa apenas após 6 amostras consecutivas confortáveis
            if self.amostras_ok >= 6 and self.nivel_atuacao > 0:
                self.amostras_ok = 0
                if self.nivel_atuacao == 2:
                    self.nivel_atuacao = 1
                    self.descartar_embb(False)
                    return "SLA folgado: policiamento do eMBB liberado"
                self.nivel_atuacao = 0
                self.definir_caminho(CLASSE_EMBB, "principal")
                return "SLA folgado: eMBB devolvido ao link principal"
        return "SLA atendido (%.2f ms)" % latencia

    def verificar_falha_de_enlace(self):
        """Reencaminha o uRLLC quando o link principal cai."""
        estado = consultar_infraestrutura()
        ativo = enlace_principal_ativo(estado)

        if not ativo and self.caminho_urllc == "principal":
            self.definir_caminho(CLASSE_URLLC, "backup")
            self.definir_caminho(CLASSE_EMBB, "backup")
            registrar_decisao("alerta",
                              "Falha no link principal detectada: todo o "
                              "tráfego migrado para o backup")
            return "falha de enlace: failover para o backup"

        if ativo and self.caminho_urllc == "backup":
            self.definir_caminho(CLASSE_URLLC, "principal")
            registrar_decisao("info",
                              "Link principal restabelecido: uRLLC devolvido "
                              "ao caminho primário")
            return "link principal restabelecido"
        return None

    def verificar_comandos(self):
        """Lê comando.json a cada iteração: é assim que o painel troca de
        cenário ou ajusta o SLA sem reiniciar o laço fechado."""
        caminho = os.path.join(DIR_DADOS, "comando.json")
        if not os.path.exists(caminho):
            return
        try:
            with open(caminho) as arquivo:
                comando = json.load(arquivo)
        except ValueError:
            return

        instante = comando.get("instante", 0)
        if not instante or instante <= self.ultimo_comando:
            return
        self.ultimo_comando = instante

        novo_limiar = comando.get("limiar_ms")
        if novo_limiar is not None and novo_limiar != self.limiar_ms:
            self.limiar_ms = novo_limiar
            registrar_decisao("info", "SLA atualizado para %.2f ms"
                              % self.limiar_ms)

        novo_cenario = comando.get("cenario")
        if novo_cenario is not None and novo_cenario != self.cenario:
            self.trocar_cenario(novo_cenario)

    def resetar_atuacoes(self):
        """Remove QoS/policiamento do cenário anterior e devolve as duas
        classes ao link principal, antes de aplicar o próximo cenário."""
        for switch in self.switches.values():
            switch.remover_regra("pol_urllc")
            switch.remover_regra("pol_embb")
        self.definir_caminho(CLASSE_URLLC, "principal")
        self.definir_caminho(CLASSE_EMBB, "principal")

    def trocar_cenario(self, novo):
        self.cenario = novo
        self.nivel_atuacao = 0
        self.historico_latencia.clear()
        self.amostras_ok = 0
        self.resetar_atuacoes()
        self.configurar_cenario()
        registrar_decisao("info", "Cenário alterado em tempo real para %d"
                          % novo)

    def executar(self):
        self.instalar_regras_base()
        self.configurar_cenario()
        registrar_decisao("info", "Laço fechado iniciado (limiar de %.1f ms, "
                          "amostragem de %.0f ms)"
                          % (self.limiar_ms, self.intervalo * 1000))

        while True:
            self.verificar_comandos()
            urllc = ler_ultima_metrica("metricas_urllc.jsonl")
            embb = ler_ultima_metrica("metricas_embb.jsonl")
            telemetria = self.coletar_telemetria_p4()

            motivo = "monitorando"
            if self.cenario == 4:
                motivo_falha = self.verificar_falha_de_enlace()
                motivo = motivo_falha or self.avaliar_sla(urllc)
            elif urllc:
                latencia = urllc.get("latencia_media_ms", 0.0)
                motivo = ("SLA violado (%.2f ms)" % latencia
                          if latencia > self.limiar_ms
                          else "SLA atendido (%.2f ms)" % latencia)

            self.publicar_estado(urllc, embb, telemetria, motivo)
            time.sleep(self.intervalo)


def principal():
    analisador = argparse.ArgumentParser(description="Controlador SDN P4")
    analisador.add_argument("--cenario", type=int, default=1,
                            choices=[1, 2, 3, 4])
    analisador.add_argument("--limiar-ms", type=float, default=5.0)
    analisador.add_argument("--intervalo", type=float, default=0.5)
    argumentos = analisador.parse_args()

    os.makedirs(DIR_DADOS, exist_ok=True)
    controlador = ControladorSDN(argumentos.cenario, argumentos.limiar_ms,
                                 argumentos.intervalo)
    try:
        controlador.executar()
    except KeyboardInterrupt:
        registrar_decisao("info", "Controlador encerrado pelo operador")


if __name__ == "__main__":
    principal()
