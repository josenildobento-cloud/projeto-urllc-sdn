#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Transforma as métricas brutas de uma execução em estatísticas.
# Gera a tabela que vai pra seção de Avaliação do artigo e um CSV pra plotar.
#
# python3 experimentos/consolidar.py --cenario 4 --descartar-inicio 10

import argparse
import csv
import json
import os
import statistics

DIR_DADOS = "/tmp/painel_sdn"
DIR_RESULTADOS = "resultados"


def carregar_amostras(arquivo, descartar_inicio):
    """Lê o JSONL e descarta os primeiros segundos (aquecimento)."""
    caminho = os.path.join(DIR_DADOS, arquivo)
    if not os.path.exists(caminho):
        return []

    amostras = []
    with open(caminho) as entrada:
        for linha in entrada:
            linha = linha.strip()
            if linha.startswith("{"):
                try:
                    amostras.append(json.loads(linha))
                except ValueError:
                    continue

    if not amostras:
        return []

    inicio = amostras[0]["instante"] + descartar_inicio
    return [a for a in amostras if a["instante"] >= inicio
            and a["pacotes_recebidos"] > 0]


def resumir(amostras, limiar_ms):
    """Calcula as estatísticas de interesse para a avaliação."""
    if not amostras:
        return {"amostras": 0}

    latencias = [a["latencia_media_ms"] for a in amostras]
    vazoes = [a["throughput_mbps"] for a in amostras]
    enviados = sum(a["pacotes_enviados"] for a in amostras)
    recebidos = sum(a["pacotes_recebidos"] for a in amostras)
    violacoes = [valor for valor in latencias if valor > limiar_ms]

    latencias_ordenadas = sorted(latencias)
    indice_p95 = min(int(len(latencias_ordenadas) * 0.95),
                     len(latencias_ordenadas) - 1)
    indice_p99 = min(int(len(latencias_ordenadas) * 0.99),
                     len(latencias_ordenadas) - 1)

    return {
        "amostras": len(amostras),
        "latencia_media_ms": round(statistics.fmean(latencias), 3),
        "latencia_mediana_ms": round(statistics.median(latencias), 3),
        "latencia_desvio_ms": round(
            statistics.pstdev(latencias) if len(latencias) > 1 else 0.0, 3),
        "latencia_p95_ms": round(latencias_ordenadas[indice_p95], 3),
        "latencia_p99_ms": round(latencias_ordenadas[indice_p99], 3),
        "latencia_maxima_ms": round(max(latencias), 3),
        "throughput_medio_mbps": round(statistics.fmean(vazoes), 3),
        "pacotes_enviados": enviados,
        "pacotes_recebidos": recebidos,
        "perda_percentual": round(
            ((enviados - recebidos) / enviados * 100) if enviados else 0.0, 3),
        "violacoes_sla": len(violacoes),
        "conformidade_sla_percentual": round(
            (1 - len(violacoes) / len(latencias)) * 100, 2),
    }


def exportar_serie(amostras, caminho_csv):
    """Salva a série temporal em CSV para gerar os gráficos do artigo."""
    if not amostras:
        return
    base = amostras[0]["instante"]
    with open(caminho_csv, "w", newline="") as saida:
        escritor = csv.writer(saida)
        escritor.writerow(["segundo", "latencia_media_ms", "latencia_p95_ms",
                           "throughput_mbps", "pacotes_perdidos"])
        for amostra in amostras:
            escritor.writerow([
                round(amostra["instante"] - base, 3),
                amostra["latencia_media_ms"],
                amostra["latencia_p95_ms"],
                amostra["throughput_mbps"],
                amostra["pacotes_perdidos"],
            ])


def imprimir_tabela(cenario, resumo_robo, resumo_fundo):
    print("\n=== Cenário %d ===" % cenario)
    cabecalho = "%-34s %14s %14s" % ("métrica", "robô (uRLLC)", "fundo (eMBB)")
    print(cabecalho)
    print("-" * len(cabecalho))
    for chave in resumo_robo:
        print("%-34s %14s %14s" % (chave, resumo_robo.get(chave, "—"),
                                   resumo_fundo.get(chave, "—")))


def principal():
    analisador = argparse.ArgumentParser(
        description="Consolida as métricas de uma execução")
    analisador.add_argument("--cenario", type=int, required=True)
    analisador.add_argument("--limiar-ms", type=float, default=5.0)
    analisador.add_argument("--descartar-inicio", type=float, default=10.0,
                            help="Segundos iniciais ignorados (aquecimento)")
    argumentos = analisador.parse_args()

    os.makedirs(DIR_RESULTADOS, exist_ok=True)

    robo = carregar_amostras("metricas_urllc.jsonl", argumentos.descartar_inicio)
    fundo = carregar_amostras("metricas_embb.jsonl", argumentos.descartar_inicio)

    resumo_robo = resumir(robo, argumentos.limiar_ms)
    resumo_fundo = resumir(fundo, argumentos.limiar_ms)
    imprimir_tabela(argumentos.cenario, resumo_robo, resumo_fundo)

    prefixo = os.path.join(DIR_RESULTADOS, "cenario%d" % argumentos.cenario)
    exportar_serie(robo, "%s_robo.csv" % prefixo)
    exportar_serie(fundo, "%s_fundo.csv" % prefixo)

    with open("%s_resumo.json" % prefixo, "w") as saida:
        json.dump({"cenario": argumentos.cenario,
                   "limiar_ms": argumentos.limiar_ms,
                   "robo": resumo_robo,
                   "background": resumo_fundo}, saida, indent=2,
                  ensure_ascii=False)

    print("\nArquivos gerados em %s/" % DIR_RESULTADOS)


if __name__ == "__main__":
    principal()
