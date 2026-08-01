#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Painel web de monitoramento e controle. Lê o estado publicado pelo
# controlador e faz ponte com a API de infraestrutura da topologia Mininet.
#
# python3 painel/servidor.py    -> http://localhost:5000

import json
import os
import urllib.request

from flask import Flask, jsonify, render_template, request

DIR_DADOS = "/tmp/painel_sdn"
API_INFRA = "http://127.0.0.1:8090"

aplicacao = Flask(__name__)


def ler_json(nome_arquivo, padrao=None):
    caminho = os.path.join(DIR_DADOS, nome_arquivo)
    if not os.path.exists(caminho):
        return padrao if padrao is not None else {}
    try:
        with open(caminho) as arquivo:
            return json.load(arquivo)
    except ValueError:
        return padrao if padrao is not None else {}


def ler_decisoes(quantidade=60):
    caminho = os.path.join(DIR_DADOS, "decisoes_sdn.jsonl")
    if not os.path.exists(caminho):
        return []
    with open(caminho) as arquivo:
        linhas = arquivo.readlines()[-quantidade:]
    decisoes = []
    for linha in linhas:
        linha = linha.strip()
        if linha.startswith("{"):
            try:
                decisoes.append(json.loads(linha))
            except ValueError:
                continue
    return decisoes


def chamar_infraestrutura(rota):
    try:
        with urllib.request.urlopen("%s/%s" % (API_INFRA, rota),
                                    timeout=3) as resposta:
            return json.loads(resposta.read().decode("utf-8"))
    except Exception as excecao:                       # noqa: BLE001
        return {"erro": "topologia indisponível: %s" % excecao}


def chamar_infraestrutura_post(rota, corpo):
    try:
        dados = json.dumps(corpo).encode("utf-8")
        requisicao = urllib.request.Request(
            "%s/%s" % (API_INFRA, rota), data=dados,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(requisicao, timeout=5) as resposta:
            return json.loads(resposta.read().decode("utf-8"))
    except Exception as excecao:                       # noqa: BLE001
        return {"erro": "topologia indisponível: %s" % excecao}


@aplicacao.route("/")
def pagina_inicial():
    return render_template("index.html")


@aplicacao.route("/api/estado")
def rota_estado():
    return jsonify({
        "estado": ler_json("estado_painel.json"),
        "parametros": ler_json("parametros.json"),
        "decisoes": ler_decisoes(40),
    })


@aplicacao.route("/api/topologia")
def rota_topologia():
    return jsonify(chamar_infraestrutura("estado"))


@aplicacao.route("/api/enlace/<origem>/<destino>/<acao>")
def rota_enlace(origem, destino, acao):
    return jsonify(chamar_infraestrutura("enlace/%s/%s/%s"
                                        % (origem, destino, acao)))


@aplicacao.route("/api/tcpdump/<nome_no>")
def rota_tcpdump(nome_no):
    return jsonify(chamar_infraestrutura("tcpdump/%s" % nome_no))


@aplicacao.route("/api/cenario/<int:numero>")
def rota_cenario(numero):
    return jsonify(chamar_infraestrutura("cenario/%d" % numero))


@aplicacao.route("/api/parametros", methods=["POST"])
def rota_parametros():
    dados = request.get_json(silent=True) or {}
    return jsonify(chamar_infraestrutura_post("parametros", dados))


if __name__ == "__main__":
    os.makedirs(DIR_DADOS, exist_ok=True)
    aplicacao.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
