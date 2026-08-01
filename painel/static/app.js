// painel de SLA uRLLC - consulta o estado publicado pelo controlador e
// opera a infraestrutura (link up/down, troca de cenário etc)

const INTERVALO_ESTADO = 500;      // ms
const INTERVALO_TOPOLOGIA = 3000;  // ms
const INTERVALO_CONSOLE = 1500;    // ms
const PONTOS_GRAFICO = 90;

const COR_ROBO = "#2f6fed";
const COR_FUNDO = "#d97706";
const COR_ALARME = "#dc2626";
const COR_TEXTO_SUAVE = "#6b7688";
const COR_GRADE = "#e3e7f0";

const POSICOES = {
  robo: [60, 70], pc: [60, 190],
  s1: [180, 130], s2: [320, 60], s3: [320, 200], s4: [460, 130],
  servidor: [580, 70], medico: [580, 190],
};

const CENARIOS = [
  { numero: 1, titulo: "1: Operação normal",
    descricao: "Apenas tráfego crítico do robô, sem QoS e sem laço fechado." },
  { numero: 2, titulo: "2: Congestão extrema",
    descricao: "Sem QoS: tráfego crítico compete com o background." },
  { numero: 3, titulo: "3: Auto-Reroute",
    descricao: "Robô no link principal, background desviado para o backup." },
  { numero: 4, titulo: "4: QoS + SDN Dinâmico",
    descricao: "Laço fechado decide e migra sozinho, com failover." },
];

let limiarAtual = 5.0;
let noSelecionado = null;
let ultimoInstanteLog = 0;
let parametrosAtuais = {};
let parametrosPreenchidos = false;
let cenarioAtivo = null;
let trocandoCenario = false;

// gráficos
function criarGrafico(elemento, rotuloY, comLimiar) {
  const conjuntos = [
    {
      label: "Robô A (uRLLC)", data: [], borderColor: COR_ROBO,
      backgroundColor: "rgba(47,111,237,.08)", borderWidth: 2,
      pointRadius: 0, tension: 0.25, fill: true,
    },
    {
      label: "Background (eMBB)", data: [], borderColor: COR_FUNDO,
      borderWidth: 2, pointRadius: 0, tension: 0.25, fill: false,
    },
  ];

  if (comLimiar) {
    conjuntos.push({
      label: "Limiar de SLA", data: [], borderColor: COR_ALARME,
      borderWidth: 1.5, borderDash: [6, 5], pointRadius: 0, fill: false,
    });
  }

  return new Chart(elemento, {
    type: "line",
    data: { labels: [], datasets: conjuntos },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: {
          labels: { color: COR_TEXTO_SUAVE, boxWidth: 12, font: { size: 11 } },
        },
      },
      scales: {
        x: { ticks: { display: false }, grid: { color: COR_GRADE } },
        y: {
          title: { display: true, text: rotuloY, color: COR_TEXTO_SUAVE },
          ticks: { color: COR_TEXTO_SUAVE, font: { size: 11 } },
          grid: { color: COR_GRADE },
          beginAtZero: true,
        },
      },
    },
  });
}

// Chart.js vem de CDN (index.html). Se a VM do laboratório não tiver saída
// pra internet, "Chart" fica undefined e sem o try/catch o script inteiro
// para aqui - o resto do painel (cenários, parâmetros, diagrama, log) não
// deveria depender disso.
let graficoLatencia = null;
let graficoThroughput = null;
try {
  graficoLatencia = criarGrafico(
    document.getElementById("grafico-latencia"), "ms", true);
  graficoThroughput = criarGrafico(
    document.getElementById("grafico-throughput"), "Mbps", false);
} catch (erro) {
  console.error("Chart.js não pôde ser iniciado:", erro);
  document.getElementById("erro-graficos").hidden = false;
}

function acrescentarPonto(grafico, rotulo, valores) {
  if (!grafico) return;
  grafico.data.labels.push(rotulo);
  valores.forEach((valor, indice) => grafico.data.datasets[indice].data.push(valor));
  if (grafico.data.labels.length > PONTOS_GRAFICO) {
    grafico.data.labels.shift();
    grafico.data.datasets.forEach((conjunto) => conjunto.data.shift());
  }
  grafico.update("none");
}

// formatadores
function formatarNumero(valor, decimais = 2) {
  if (valor === null || valor === undefined || Number.isNaN(valor)) return "—";
  return Number(valor).toLocaleString("pt-BR", {
    minimumFractionDigits: decimais, maximumFractionDigits: decimais,
  });
}

function formatarHora(instante) {
  return new Date(instante * 1000).toLocaleTimeString("pt-BR");
}

// gauges tipo velocímetro (arco de 180°), o comprimento do traço colorido
// é controlado via stroke-dasharray/stroke-dashoffset. getTotalLength() é
// caro então cacheia por gauge em vez de chamar de novo a cada 500ms.
const COMPRIMENTO_ARCOS = {};
function comprimentoArco(idGauge) {
  if (!(idGauge in COMPRIMENTO_ARCOS)) {
    const elemento = document.getElementById(idGauge);
    COMPRIMENTO_ARCOS[idGauge] = elemento ? elemento.getTotalLength() : 0;
  }
  return COMPRIMENTO_ARCOS[idGauge];
}

function atualizarGauge(idGauge, percentual, texto, alarme) {
  const arco = document.getElementById(idGauge);
  if (!arco) return;
  const comprimento = comprimentoArco(idGauge);
  const pct = Number.isFinite(percentual) ? Math.max(0, Math.min(100, percentual)) : 0;

  arco.style.strokeDasharray = String(comprimento);
  arco.style.strokeDashoffset = String(comprimento * (1 - pct / 100));
  arco.classList.toggle("alarme", !!alarme);
  arco.classList.toggle("inativo", texto === "—" || texto === "— ms");

  const valorEl = document.getElementById(idGauge.replace("gauge-", "valor-"));
  if (valorEl) valorEl.textContent = texto;
}

// indicadores
function atualizarContrato(estado, parametros) {
  const robo = estado.robo || {};
  const latencia = robo.latencia_media_ms;
  limiarAtual = estado.limiar_ms ?? parametros.limiar_ms ?? 5.0;
  const limiteRobo = parametros.limite_robo || 2.0;

  const temAmostra = typeof latencia === "number" && robo.pacotes_recebidos > 0;
  const violando = temAmostra && latencia > limiarAtual;

  const ocupacao = temAmostra ? Math.min((latencia / limiarAtual) * 100, 100) : 0;
  const slaPct = temAmostra ? (violando ? 0 : 100 - ocupacao) : 100;

  atualizarGauge("gauge-sla", slaPct,
    temAmostra ? `${formatarNumero(slaPct, 0)}%` : "100%", violando);
  atualizarGauge("gauge-latencia", ocupacao,
    temAmostra ? `${formatarNumero(latencia)} ms` : "— ms", violando);
  atualizarGauge("gauge-vazao",
    robo.throughput_mbps ? Math.min((robo.throughput_mbps / limiteRobo) * 100, 100) : 0,
    `${formatarNumero(robo.throughput_mbps)} Mbps`, false);

  document.getElementById("tile-descartes-robo").textContent = robo.pacotes_perdidos ?? 0;
  document.getElementById("p95-robo").textContent = `${formatarNumero(robo.latencia_p95_ms)} ms`;
  document.getElementById("caminho-robo").textContent = estado.caminho_urllc || "—";
  document.getElementById("nivel-atuacao").textContent = `${estado.nivel_atuacao ?? "—"}`;
  document.getElementById("motivo-decisao").textContent = estado.ultimo_motivo || "—";

  document.getElementById("contrato").classList.toggle("violando", violando);
}

function atualizarBackground(estado) {
  const fundo = estado.background || {};
  document.getElementById("latencia-fundo").textContent =
    `${formatarNumero(fundo.latencia_media_ms)} ms`;
  document.getElementById("throughput-fundo").textContent =
    `${formatarNumero(fundo.throughput_mbps)} Mbps`;
  document.getElementById("perdidos-fundo").textContent =
    `${fundo.pacotes_perdidos ?? "—"} (${formatarNumero(fundo.perda_percentual, 1)}%)`;
  document.getElementById("caminho-fundo").textContent =
    estado.caminho_embb || "—";
  document.getElementById("tile-vazao-fundo").textContent =
    `${formatarNumero(fundo.throughput_mbps)} Mbps`;
  document.getElementById("tile-descartes-fundo").textContent =
    fundo.pacotes_perdidos ?? 0;
}

function atualizarTelemetria(estado) {
  const corpo = document.getElementById("corpo-telemetria");
  const telemetria = estado.telemetria_p4 || {};
  const linhas = [];

  Object.keys(telemetria).sort().forEach((roteador) => {
    Object.entries(telemetria[roteador]).forEach(([classe, valores]) => {
      const nome = classe === "1" ? "uRLLC" : "eMBB";
      const estilo = classe === "1" ? "classe-urllc" : "classe-embb";
      linhas.push(`<tr>
        <td>${roteador}</td>
        <td class="${estilo}">${nome}</td>
        <td>${formatarNumero(valores.throughput_mbps, 2)}</td>
        <td>${valores.descartes ?? 0}</td>
        <td>${valores.atraso_fila_us ?? 0}</td>
      </tr>`);
    });
  });

  corpo.innerHTML = linhas.length ? linhas.join("")
    : '<tr><td colspan="5">sem dados</td></tr>';
}

function atualizarLog(decisoes) {
  if (!decisoes || !decisoes.length) return;
  const novas = decisoes.filter((d) => d.instante > ultimoInstanteLog);
  if (!novas.length) return;
  ultimoInstanteLog = novas[novas.length - 1].instante;

  const lista = document.getElementById("log-decisoes");
  lista.querySelector(".vazio")?.remove();

  novas.forEach((decisao) => {
    const item = document.createElement("li");
    item.innerHTML = `
      <span class="instante">${formatarHora(decisao.instante)}</span>
      <span class="nivel nivel-${decisao.nivel}">${decisao.nivel}</span>
      <span class="mensagem">${decisao.mensagem}</span>`;
    lista.prepend(item);
  });

  while (lista.children.length > 120) lista.lastElementChild.remove();
}

// cenários
function montarCenarios() {
  const area = document.getElementById("grade-cenarios");
  area.innerHTML = "";
  CENARIOS.forEach((cenario) => {
    const botao = document.createElement("button");
    botao.type = "button";
    botao.className = "cartao-cenario";
    botao.dataset.numero = cenario.numero;
    botao.innerHTML = `<span class="titulo">${cenario.titulo}</span>
      <span class="descricao">${cenario.descricao}</span>`;
    botao.addEventListener("click", () => trocarCenario(cenario.numero));
    area.appendChild(botao);
  });
}

function marcarCenarioAtivo(numero) {
  if (!numero || numero === cenarioAtivo) return;
  cenarioAtivo = numero;
  document.querySelectorAll(".cartao-cenario").forEach((botao) => {
    botao.classList.toggle("ativo", Number(botao.dataset.numero) === numero);
  });
}

function trocarCenario(numero) {
  if (trocandoCenario || numero === cenarioAtivo) return;
  trocandoCenario = true;
  const retorno = document.getElementById("retorno-cenario");
  retorno.textContent = `ativando cenário ${numero}…`;
  fetch(`/api/cenario/${numero}`)
    .then((r) => r.json())
    .then((resposta) => {
      retorno.textContent = resposta.erro
        ? `não foi possível trocar de cenário: ${resposta.erro}`
        : "";
      atualizarEstado();
    })
    .catch((erro) => {
      retorno.textContent = `falha na requisição: ${erro}`;
    })
    .finally(() => { trocandoCenario = false; });
}

// parâmetros
function preencherParametros(parametros) {
  if (parametrosPreenchidos || parametros.limiar_ms === undefined) return;
  document.getElementById("param-limiar").value = parametros.limiar_ms;
  document.getElementById("param-banda-principal").value = parametros.banda_principal;
  document.getElementById("param-banda-backup").value = parametros.banda_backup;
  document.getElementById("param-limite-robo").value = parametros.limite_robo;
  document.getElementById("param-limite-background").value = parametros.limite_background;
  parametrosPreenchidos = true;
}

document.getElementById("form-parametros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  const retorno = document.getElementById("retorno-parametros");
  const corpo = {
    limiar_ms: parseFloat(document.getElementById("param-limiar").value),
    banda_principal: parseFloat(document.getElementById("param-banda-principal").value),
    banda_backup: parseFloat(document.getElementById("param-banda-backup").value),
    limite_robo: parseFloat(document.getElementById("param-limite-robo").value),
    limite_background: parseFloat(document.getElementById("param-limite-background").value),
  };

  retorno.textContent = "aplicando…";
  fetch("/api/parametros", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  }).then((r) => r.json()).then((resposta) => {
    retorno.textContent = resposta.erro
      ? `não foi possível aplicar: ${resposta.erro}`
      : "parâmetros aplicados.";
  }).catch((erro) => { retorno.textContent = `falha na requisição: ${erro}`; });
});

// diagrama
function desenharDiagrama(topologia) {
  const area = document.getElementById("diagrama");
  if (topologia.erro) {
    area.innerHTML = `<p class="retorno">${topologia.erro}</p>`;
    return;
  }

  const nos = topologia.nos || [];
  const enlaces = topologia.enlaces || [];
  const extras = nos.filter((no) => !POSICOES[no.nome]);
  extras.forEach((no, indice) => {
    POSICOES[no.nome] = [90 + indice * 120, 270];
  });

  const partes = [];
  enlaces.forEach((enlace) => {
    const origem = POSICOES[enlace.origem];
    const destino = POSICOES[enlace.destino];
    if (!origem || !destino) return;
    const transporte = enlace.origem.startsWith("s") && enlace.destino.startsWith("s");
    const classe = `enlace ${enlace.ativo ? "ativo" : "caido"}`;
    partes.push(`<line class="${classe}" x1="${origem[0]}" y1="${origem[1]}"
      x2="${destino[0]}" y2="${destino[1]}"
      ${transporte ? `data-origem="${enlace.origem}" data-destino="${enlace.destino}"
      data-ativo="${enlace.ativo}"` : 'style="cursor:default"'}></line>`);
  });

  nos.forEach((no) => {
    const [x, y] = POSICOES[no.nome];
    const cor = no.tipo === "switch" ? "#eef1f8" : "#ffffff";
    const traco = no.nome === "robo" ? COR_ROBO : "#94a3b8";
    if (no.tipo === "switch") {
      partes.push(`<rect x="${x - 26}" y="${y - 16}" width="52" height="32"
        rx="3" fill="${cor}" stroke="${traco}"></rect>`);
    } else {
      partes.push(`<circle cx="${x}" cy="${y}" r="17" fill="${cor}"
        stroke="${traco}" stroke-width="2"></circle>`);
    }
    partes.push(`<text class="rotulo-no" x="${x}" y="${y + 4}"
      text-anchor="middle">${no.nome}</text>`);
  });

  partes.push(`<text class="rotulo-enlace" x="320" y="34" text-anchor="middle">
    LINK PRINCIPAL</text>`);
  partes.push(`<text class="rotulo-enlace" x="320" y="238" text-anchor="middle">
    LINK DE BACKUP</text>`);

  area.innerHTML = `<svg viewBox="0 0 640 300"
    xmlns="http://www.w3.org/2000/svg">${partes.join("")}</svg>`;

  area.querySelectorAll("line[data-origem]").forEach((linha) => {
    linha.addEventListener("click", () => {
      const acao = linha.dataset.ativo === "true" ? "down" : "up";
      fetch(`/api/enlace/${linha.dataset.origem}/${linha.dataset.destino}/${acao}`)
        .then(() => atualizarTopologia());
    });
  });
}

function atualizarTopologia() {
  fetch("/api/topologia").then((r) => r.json()).then((topologia) => {
    desenharDiagrama(topologia);
    montarAbas(topologia.nos || []);
  }).catch(() => {});
}

// console de inspeção
function montarAbas(nos) {
  const area = document.getElementById("abas-tcpdump");
  if (area.dataset.montado === String(nos.length)) return;
  area.dataset.montado = String(nos.length);
  area.innerHTML = "";

  nos.forEach((no) => {
    const botao = document.createElement("button");
    botao.type = "button";
    botao.textContent = no.nome;
    botao.addEventListener("click", () => {
      noSelecionado = no.nome;
      area.querySelectorAll("button").forEach((b) =>
        b.classList.remove("selecionada"));
      botao.classList.add("selecionada");
      carregarConsole();
    });
    area.appendChild(botao);
  });
}

function carregarConsole() {
  if (!noSelecionado) return;
  fetch(`/api/tcpdump/${noSelecionado}`).then((r) => r.json()).then((dados) => {
    const console_ = document.getElementById("console-tcpdump");
    const linhas = dados.linhas || [];
    console_.textContent = linhas.length
      ? linhas.join("\n")
      : `sem captura disponível para ${noSelecionado}`;
    console_.scrollTop = console_.scrollHeight;
  }).catch(() => {});
}

// laço de UI
function atualizarEstado() {
  fetch("/api/estado").then((r) => r.json()).then((dados) => {
    const estado = dados.estado || {};
    const parametros = dados.parametros || {};
    parametrosAtuais = parametros;
    preencherParametros(parametros);

    const pill = document.getElementById("pill-conexao");
    const textoConexao = document.getElementById("texto-conexao");

    if (!estado.instante) {
      pill.classList.add("desconectado");
      textoConexao.textContent = "aguardando controlador";
      return;
    }

    pill.classList.remove("desconectado");
    textoConexao.textContent = `atualizado ${formatarHora(estado.instante)}`;
    document.getElementById("rotulo-cenario").textContent =
      `cenário ${estado.cenario ?? "—"}`;
    marcarCenarioAtivo(estado.cenario);

    atualizarContrato(estado, parametros);
    atualizarBackground(estado);
    atualizarTelemetria(estado);
    atualizarLog(dados.decisoes);

    const rotulo = formatarHora(estado.instante);
    acrescentarPonto(graficoLatencia, rotulo, [
      estado.robo?.latencia_media_ms ?? null,
      estado.background?.latencia_media_ms ?? null,
      limiarAtual,
    ]);
    acrescentarPonto(graficoThroughput, rotulo, [
      estado.robo?.throughput_mbps ?? null,
      estado.background?.throughput_mbps ?? null,
    ]);
  }).catch(() => {
    document.getElementById("pill-conexao").classList.add("desconectado");
    document.getElementById("texto-conexao").textContent =
      "painel sem contato com o servidor";
  });
}

montarCenarios();
atualizarEstado();
atualizarTopologia();
setInterval(atualizarEstado, INTERVALO_ESTADO);
setInterval(atualizarTopologia, INTERVALO_TOPOLOGIA);
setInterval(carregarConsole, INTERVALO_CONSOLE);
