#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Camada fina sobre o simple_switch_CLI (Thrift) do BMv2. O controlador
# fala com os switches só por aqui: insere regra, troca regra e lê
# registrador de telemetria.

import re
import subprocess

PREFIXO_ENTRADA = "ProcessamentoEntrada"
PREFIXO_SAIDA = "ProcessamentoSaida"


class ClienteP4:
    """Representa a conexão de controle com um switch BMv2."""

    def __init__(self, nome, porta_thrift):
        self.nome = nome
        self.porta_thrift = porta_thrift
        self.identificadores = {}   # apelido da regra -> handle devolvido

    def executar(self, comandos):
        """Envia uma lista de comandos ao simple_switch_CLI e devolve a saída."""
        if isinstance(comandos, str):
            comandos = [comandos]
        entrada = "\n".join(comandos) + "\n"
        try:
            processo = subprocess.run(
                ["simple_switch_CLI", "--thrift-port", str(self.porta_thrift)],
                input=entrada, capture_output=True, text=True, timeout=10)
            return processo.stdout
        except subprocess.TimeoutExpired:
            return ""

    # tabelas
    def adicionar_regra(self, tabela, chaves, acao, parametros,
                        prioridade=None, apelido=None):
        """Insere uma entrada e guarda o handle para remoção posterior."""
        comando = "table_add %s.%s %s %s => %s" % (
            PREFIXO_ENTRADA, tabela, acao, " ".join(str(c) for c in chaves),
            " ".join(str(p) for p in parametros))
        if prioridade is not None:
            comando += " %d" % prioridade

        saida = self.executar(comando)
        encontrado = re.search(r"handle (\d+)", saida)
        if encontrado and apelido:
            self.identificadores[apelido] = (tabela, int(encontrado.group(1)))
        return saida

    def remover_regra(self, apelido):
        if apelido not in self.identificadores:
            return ""
        tabela, handle = self.identificadores.pop(apelido)
        return self.executar("table_delete %s.%s %d"
                             % (PREFIXO_ENTRADA, tabela, handle))

    def modificar_regra(self, apelido, acao, parametros):
        """Troca a ação de uma entrada já existente, sem removê-la."""
        if apelido not in self.identificadores:
            return ""
        tabela, handle = self.identificadores[apelido]
        return self.executar("table_modify %s.%s %s %d => %s"
                             % (PREFIXO_ENTRADA, tabela, acao, handle,
                                " ".join(str(p) for p in parametros)))

    def definir_acao_padrao(self, tabela, acao, parametros=()):
        return self.executar("table_set_default %s.%s %s %s"
                             % (PREFIXO_ENTRADA, tabela, acao,
                                " ".join(str(p) for p in parametros)))

    def limpar_tabela(self, tabela):
        return self.executar("table_clear %s.%s" % (PREFIXO_ENTRADA, tabela))

    # telemetria
    def ler_registrador(self, nome, indice, no_egresso=False):
        prefixo = PREFIXO_SAIDA if no_egresso else PREFIXO_ENTRADA
        saida = self.executar("register_read %s.%s %d"
                              % (prefixo, nome, indice))
        encontrado = re.search(r"=\s*(\d+)", saida)
        return int(encontrado.group(1)) if encontrado else 0

    def zerar_registrador(self, nome, no_egresso=False):
        prefixo = PREFIXO_SAIDA if no_egresso else PREFIXO_ENTRADA
        return self.executar("register_reset %s.%s" % (prefixo, nome))

    def coletar_telemetria(self, classes=(1, 2)):
        """Devolve pacotes, bytes, descartes e atraso de fila por classe."""
        resultado = {}
        for classe in classes:
            resultado[classe] = {
                "pacotes": self.ler_registrador("contador_pacotes", classe),
                "bytes": self.ler_registrador("contador_bytes", classe),
                "descartes": self.ler_registrador("contador_descartes", classe),
                "atraso_fila_us": self.ler_registrador(
                    "atraso_fila_us", classe, no_egresso=True),
                "profundidade_fila": self.ler_registrador(
                    "profundidade_fila", classe, no_egresso=True),
            }
        return resultado
