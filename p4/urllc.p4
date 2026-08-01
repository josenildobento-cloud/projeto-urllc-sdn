/* -*- P4_16 -*- */
/*
 * Plano de dados dos roteadores da rede de transporte.
 *
 * Classifica cada pacote em uRLLC (robô) ou eMBB (background), marca DSCP
 * e prioridade de fila, encaminha por LPM (podendo sobrescrever a saída
 * pra escolher entre link principal e backup), aplica a política de cada
 * classe (permite/descarta) e expõe telemetria em registradores lidos
 * pelo controlador.
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> TIPO_IPV4 = 0x0800;
const bit<8>  PROTO_TCP = 6;
const bit<8>  PROTO_UDP = 17;

const bit<8>  CLASSE_OUTRA = 0;
const bit<8>  CLASSE_URLLC = 1;
const bit<8>  CLASSE_EMBB  = 2;

/* DSCP EF (Expedited Forwarding) para uRLLC, AF11 para eMBB */
const bit<6>  DSCP_EF   = 46;
const bit<6>  DSCP_AF11 = 10;

const bit<32> TAM_REGISTRADOR = 8;   /* indexado pela classe (0..7) */

typedef bit<9>  id_porta_t;
typedef bit<48> mac_t;
typedef bit<32> ipv4_t_addr;

header ethernet_t {
    mac_t   destino;
    mac_t   origem;
    bit<16> tipo;
}

header ipv4_t {
    bit<4>      versao;
    bit<4>      ihl;
    bit<6>      dscp;
    bit<2>      ecn;
    bit<16>     tamanho_total;
    bit<16>     identificacao;
    bit<3>      flags;
    bit<13>     deslocamento;
    bit<8>      ttl;
    bit<8>      protocolo;
    bit<16>     checksum;
    ipv4_t_addr origem;
    ipv4_t_addr destino;
}

header tcp_t {
    bit<16> porta_origem;
    bit<16> porta_destino;
    bit<32> numero_sequencia;
    bit<32> numero_ack;
    bit<4>  offset;
    bit<3>  reservado;
    bit<9>  flags;
    bit<16> janela;
    bit<16> checksum;
    bit<16> ponteiro_urgente;
}

header udp_t {
    bit<16> porta_origem;
    bit<16> porta_destino;
    bit<16> tamanho;
    bit<16> checksum;
}

struct metadados_t {
    bit<8>  classe;           /* 0 = outra, 1 = uRLLC, 2 = eMBB */
    bit<16> porta_l4_origem;
    bit<16> porta_l4_destino;
    bit<1>  ja_classificado;
    bit<1>  descartar;        /* sinaliza descarte já decidido */
}

struct cabecalhos_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
    udp_t      udp;
}

parser AnalisadorPacote(packet_in pacote,
                        out cabecalhos_t hdr,
                        inout metadados_t meta,
                        inout standard_metadata_t padrao) {

    state start {
        meta.classe          = CLASSE_OUTRA;
        meta.porta_l4_origem = 0;
        meta.porta_l4_destino = 0;
        meta.ja_classificado = 0;
        meta.descartar       = 0;
        transition analisar_ethernet;
    }

    state analisar_ethernet {
        pacote.extract(hdr.ethernet);
        transition select(hdr.ethernet.tipo) {
            TIPO_IPV4: analisar_ipv4;
            default:   accept;
        }
    }

    state analisar_ipv4 {
        pacote.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocolo) {
            PROTO_TCP: analisar_tcp;
            PROTO_UDP: analisar_udp;
            default:   accept;
        }
    }

    state analisar_tcp {
        pacote.extract(hdr.tcp);
        meta.porta_l4_origem  = hdr.tcp.porta_origem;
        meta.porta_l4_destino = hdr.tcp.porta_destino;
        transition accept;
    }

    state analisar_udp {
        pacote.extract(hdr.udp);
        meta.porta_l4_origem  = hdr.udp.porta_origem;
        meta.porta_l4_destino = hdr.udp.porta_destino;
        transition accept;
    }
}

control VerificarChecksum(inout cabecalhos_t hdr, inout metadados_t meta) {
    apply { }
}

control ProcessamentoEntrada(inout cabecalhos_t hdr,
                             inout metadados_t meta,
                             inout standard_metadata_t padrao) {

    /* contadores de telemetria lidos pelo controlador via Thrift */
    register<bit<64>>(TAM_REGISTRADOR) contador_pacotes;
    register<bit<64>>(TAM_REGISTRADOR) contador_bytes;
    register<bit<64>>(TAM_REGISTRADOR) contador_descartes;

    action descartar_pacote() {
        bit<64> atual;
        contador_descartes.read(atual, (bit<32>) meta.classe);
        contador_descartes.write((bit<32>) meta.classe, atual + 1);
        meta.descartar = 1;
        mark_to_drop(padrao);
    }

    /* Marca a classe do fluxo e o DSCP correspondente */
    action marcar_classe(bit<8> classe, bit<6> dscp) {
        meta.classe          = classe;
        meta.ja_classificado = 1;
        hdr.ipv4.dscp        = dscp;
    }

    /* Encaminhamento IPv4 padrão (tabela LPM) */
    action encaminhar(id_porta_t porta, mac_t mac_destino) {
        padrao.egress_spec   = porta;
        hdr.ethernet.destino = mac_destino;
        hdr.ipv4.ttl         = hdr.ipv4.ttl - 1;
    }

    /* Sobrescreve a porta de saída: usado para escolher principal x backup */
    action definir_caminho(id_porta_t porta, mac_t mac_destino) {
        padrao.egress_spec   = porta;
        hdr.ethernet.destino = mac_destino;
    }

    /* Define a fila de prioridade do egresso (0 = menor, 7 = maior).
       O BMv2 precisa ter sido compilado com suporte a filas de prioridade;
       o DSCP marcado em marcar_classe garante a diferenciação também no tc,
       caso o alvo ignore standard_metadata.priority. */
    action definir_prioridade(bit<3> prioridade) {
        padrao.priority = prioridade;
    }

    action nao_fazer_nada() { }

    /* classificação por portas L4, ternária (aceita curinga em um dos lados) */
    table tabela_classificacao {
        key = {
            meta.porta_l4_origem  : ternary;
            meta.porta_l4_destino : ternary;
            hdr.ipv4.protocolo    : ternary;
        }
        actions = {
            marcar_classe;
            nao_fazer_nada;
        }
        size = 128;
        default_action = nao_fazer_nada();
    }

    /* política por classe, o controlador reescreve essa tabela em tempo real */
    table tabela_politica {
        key = { meta.classe : exact; }
        actions = {
            definir_prioridade;
            descartar_pacote;
            nao_fazer_nada;
        }
        size = 16;
        default_action = nao_fazer_nada();
    }

    /* encaminhamento base */
    table tabela_encaminhamento {
        key = { hdr.ipv4.destino : lpm; }
        actions = {
            encaminhar;
            descartar_pacote;
        }
        size = 512;
        default_action = descartar_pacote();
    }

    /* engenharia de tráfego: rota por classe (principal x backup).
       só recebe entrada nos switches de borda (s1 e s4) */
    table tabela_caminho {
        key = {
            meta.classe      : exact;
            hdr.ipv4.destino : lpm;
        }
        actions = {
            definir_caminho;
            nao_fazer_nada;
        }
        size = 64;
        default_action = nao_fazer_nada();
    }

    apply {
        if (!hdr.ipv4.isValid()) {
            return;
        }

        tabela_classificacao.apply();

        /* contabiliza pra telemetria */
        bit<64> pacotes;
        bit<64> bytes;
        contador_pacotes.read(pacotes, (bit<32>) meta.classe);
        contador_pacotes.write((bit<32>) meta.classe, pacotes + 1);
        contador_bytes.read(bytes, (bit<32>) meta.classe);
        contador_bytes.write((bit<32>) meta.classe,
                             bytes + (bit<64>) padrao.packet_length);

        /* a política pode decidir o descarte aqui */
        tabela_politica.apply();
        if (meta.descartar == 1) {
            return;
        }

        tabela_encaminhamento.apply();
        if (meta.descartar == 1) {
            return;
        }

        /* engenharia de tráfego sobrescreve a saída, se houver regra */
        tabela_caminho.apply();

        if (hdr.ipv4.ttl == 0) {
            descartar_pacote();
        }
    }
}

control ProcessamentoSaida(inout cabecalhos_t hdr,
                           inout metadados_t meta,
                           inout standard_metadata_t padrao) {

    /* telemetria de fila do próprio switch, usada no artigo */
    register<bit<32>>(TAM_REGISTRADOR) atraso_fila_us;
    register<bit<32>>(TAM_REGISTRADOR) profundidade_fila;

    apply {
        if (hdr.ipv4.isValid()) {
            atraso_fila_us.write((bit<32>) meta.classe, padrao.deq_timedelta);
            profundidade_fila.write((bit<32>) meta.classe,
                                    (bit<32>) padrao.deq_qdepth);
        }
    }
}

control CalcularChecksum(inout cabecalhos_t hdr, inout metadados_t meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.versao, hdr.ipv4.ihl, hdr.ipv4.dscp, hdr.ipv4.ecn,
              hdr.ipv4.tamanho_total, hdr.ipv4.identificacao, hdr.ipv4.flags,
              hdr.ipv4.deslocamento, hdr.ipv4.ttl, hdr.ipv4.protocolo,
              hdr.ipv4.origem, hdr.ipv4.destino },
            hdr.ipv4.checksum,
            HashAlgorithm.csum16);
    }
}

control MontadorPacote(packet_out pacote, in cabecalhos_t hdr) {
    apply {
        pacote.emit(hdr.ethernet);
        pacote.emit(hdr.ipv4);
        pacote.emit(hdr.tcp);
        pacote.emit(hdr.udp);
    }
}

V1Switch(
    AnalisadorPacote(),
    VerificarChecksum(),
    ProcessamentoEntrada(),
    ProcessamentoSaida(),
    CalcularChecksum(),
    MontadorPacote()
) main;
