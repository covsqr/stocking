const $ = (id) => document.getElementById(id);

const AUTO_INTERVAL_SECONDS = 300;

const state = {
  lastResult: null,
  poller: null,
};

function parseSymbols() {
  const symbols = $("symbols").value
    .split(/[,\s]+/)
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
  return [...new Set(symbols)].slice(0, 8);
}

function settingsPayload() {
  return {
    symbols: parseSymbols(),
    profile: $("profile").value,
    market: $("market").value,
    autoIntervalSeconds: AUTO_INTERVAL_SECONDS,
  };
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || "요청 처리 중 오류가 발생했습니다.");
  }
  return data;
}

async function post(url, body = {}) {
  setStatus("saving", "busy");
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || "요청 처리 중 오류가 발생했습니다.");
  }
  setStatus("running");
  return data;
}

function setStatus(text, kind = "") {
  const badge = $("statusBadge");
  badge.textContent = text;
  badge.className = `badge ${kind}`.trim();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch];
  });
}

function pct(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function signedPct(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  const number = Number(value) * 100;
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function money(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  return Math.round(Number(value)).toLocaleString("ko-KR");
}

function priceMoney(item, field = "price", sourceField = "sourcePrice") {
  if (!item || item[field] === undefined || item[field] === null) return "-";
  const krw = `KRW ${money(item[field])}`;
  if (item.sourceCurrency === "USD" && item[sourceField] !== undefined && item[sourceField] !== null) {
    return `USD ${Number(item[sourceField]).toLocaleString("en-US", { maximumFractionDigits: 2 })} / ${krw}`;
  }
  return krw;
}

function signedMoney(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  const number = Math.round(Number(value));
  return `${number >= 0 ? "+" : ""}${number.toLocaleString("ko-KR")}`;
}

function symbolTitle(item) {
  const name = item?.name || item?.symbol || "-";
  const symbol = item?.symbol || "-";
  return `${name} · ${symbol}`;
}

function marketBadge(item) {
  const label = item?.marketOpen ? "장중" : "장외";
  const className = item?.marketOpen ? "market-open" : "market-closed";
  return `<span class="mini-badge ${className}">${label}</span>`;
}

function renderLive(result) {
  state.lastResult = result;
  setStatus("running");
  syncControls(result);
  renderDecision(result.lastDecision || {});

  const metricValues = [
    money(result.finalValue),
    pct(result.metrics.totalReturn),
    pct(result.metrics.policyReturn),
    money(result.metrics.realizedPnl),
    pct(result.metrics.winRate),
    String(result.metrics.step),
  ];
  [...$("metrics").querySelectorAll("strong")].forEach((node, idx) => {
    node.textContent = metricValues[idx];
  });

  $("chartCaption").textContent =
    `${result.profile} · 안전 정책 v${result.policyVersion} · Q ${result.metrics.policySize}`;
  $("runtimeStatus").innerHTML = [
    `판단 5분`,
    `비중 조정 ${Math.round(result.minTradeIntervalSeconds / 60)}분`,
    `종목 교체 ${Math.round(result.symbolRotationSeconds / 3600)}시간`,
    `손실 휴식 ${Math.round(result.riskStopCooldownSeconds / 3600)}시간`,
    `남은 휴식 ${Math.ceil((result.metrics.riskStopRemainingSeconds || 0) / 60)}분`,
    `탐색률 ${pct(result.metrics.epsilon)}`,
    `정책 낙폭 ${pct(result.metrics.policyDrawdown)}`,
  ].map((item) => `<span class="badge muted">${escapeHtml(item)}</span>`).join("");

  renderChart(result.equityCurve);
  renderPositions(result);
  renderHoldings(result);
  renderTrades(result.trades || [], result);
  renderQuotes(result.quotes || {});
  renderRotation(result.rotation || {});
  renderMessages(result.messages || []);
}

function renderDecision(decision) {
  $("decisionStatus").textContent = decision.status || "-";
  $("decisionAction").textContent = decision.action || "-";
  $("decisionTrades").textContent = `${decision.tradeCount ?? 0}건`;
  $("decisionReward").textContent =
    decision.reward === undefined || decision.reward === null ? "-" : Number(decision.reward).toFixed(6);
  $("decisionChecked").textContent = decision.checkedAt || "-";
  $("decisionQuote").textContent = decision.quoteTime || "-";
  $("decisionNext").textContent = decision.nextCheckAt || "-";
  $("decisionBadge").textContent = decision.action || "대기";

  const panel = $("decisionPanel");
  const tradeCount = Number(decision.tradeCount || 0);
  panel.classList.toggle("is-trade", tradeCount > 0);
  panel.classList.toggle("is-wait", String(decision.action || "").includes("대기") || String(decision.action || "").includes("보류"));
}

function syncControls(result) {
  if (document.activeElement !== $("symbols")) {
    $("symbols").value = (result.symbols || []).join(", ");
  }
  $("profile").value = result.profile || "balanced";
  $("market").value = result.rotation?.market || "mixed";
  $("intervalDisplay").value = "5분마다 자동 판단";
}

function renderPositions(result) {
  const positions = result.positions || [];
  const invested = positions.filter((item) => item.value > 0);
  const totalPnl = positions.reduce((sum, item) => sum + Number(item.unrealizedPnl || 0), 0);
  $("positionSummary").textContent = `${invested.length}개 보유 · 미실현 ${signedMoney(totalPnl)}`;

  $("positionCards").innerHTML = positions
    .slice(0, 8)
    .map((item) => {
      const tone = item.unrealizedPnl >= 0 ? "up" : "down";
      const subtitle = item.value > 0
        ? `${signedPct(item.unrealizedPnlRate)} (${signedMoney(item.unrealizedPnl)})`
        : "미보유";
      return `
        <article class="position-card">
          <div>
            <strong>${escapeHtml(item.name)}</strong>
            <span>${escapeHtml(item.symbol)} ${marketBadge(item)}</span>
          </div>
          <em class="${tone}">${escapeHtml(subtitle)}</em>
          ${miniSparkline(item.points || [], tone)}
        </article>
      `;
    })
    .join("");

  $("positions").innerHTML = positions
    .map((item) => {
      const tone = item.unrealizedPnl >= 0 ? "up" : "down";
      const targetBadge = item.targeted ? "" : `<span class="mini-badge">제외 후보</span>`;
      const openBadge = marketBadge(item);
      return `
        <tr>
          <td>
            <strong>${escapeHtml(item.name)}</strong>
            <small>${escapeHtml(item.symbol)} ${openBadge} ${targetBadge}</small>
          </td>
          <td>${Number(item.shares).toFixed(4)}</td>
          <td>${priceMoney(item, "avgCost", "sourceAvgCost")}</td>
          <td>${priceMoney(item)}</td>
          <td>${money(item.value)}</td>
          <td class="${tone}">${signedPct(item.unrealizedPnlRate)} (${signedMoney(item.unrealizedPnl)})</td>
        </tr>
      `;
    })
    .join("");
}

function renderHoldings(result) {
  const positions = result.positions || [];
  $("weights").innerHTML = positions
    .map((item) => {
      const width = Math.max(0, Math.min(100, item.weight * 100));
      return `
        <div class="weight-row">
          <header><span>${escapeHtml(item.name)}</span><span>${pct(item.weight)}</span></header>
          <div class="bar"><span style="width:${width}%"></span></div>
          <small>${escapeHtml(item.symbol)} · 평가 ${money(item.value)} · ${signedPct(item.unrealizedPnlRate)}</small>
        </div>
      `;
    })
    .join("");
}

function renderQuotes(quotes) {
  const entries = Object.values(quotes);
  $("quotes").innerHTML = entries
    .map((quote) => {
      const tone = quote.change >= 0 ? "up" : "down";
      return `
        <article class="quote-card">
          <div>
            <strong>${escapeHtml(quote.name || quote.symbol)}</strong>
            <small>${escapeHtml(quote.symbol)} ${marketBadge(quote)}</small>
          </div>
          <span>${priceMoney(quote)}</span>
          <em class="${tone}">${signedPct(quote.change)}</em>
          <small>시세 ${escapeHtml(quote.time || "-")}</small>
        </article>
      `;
    })
    .join("");
}

function renderRotation(rotation) {
  const candidates = rotation.candidates || [];
  $("rotationBadge").textContent = rotation.enabled ? "자동" : "수동";
  $("rotationPanel").innerHTML = `
    <div class="rotation-meta">
      <span>다음 교체</span>
      <strong>${escapeHtml(rotation.nextAt || "다음 판단")}</strong>
    </div>
    ${candidates.slice(0, 8).map((item, idx) => `
      <div class="rotation-item">
        <span>${idx + 1}</span>
        <strong>${escapeHtml(item.name || item.symbol)}</strong>
        <small>${escapeHtml(item.symbol)} · 점수 ${item.score} · 30일 ${signedPct(item.recentReturn)}</small>
      </div>
    `).join("")}
  `;
}

function renderMessages(messages) {
  $("log").innerHTML = messages
    .map((message) => `<div class="log-entry">${escapeHtml(message)}</div>`)
    .join("");
}

function renderTrades(trades, result) {
  const names = result.symbolNames || {};
  $("tradeCount").textContent = `${trades.length}건`;
  $("trades").innerHTML = trades
    .slice()
    .reverse()
    .slice(0, 300)
    .map((trade) => {
      const sideClass = trade.side === "BUY" ? "side-buy" : "side-sell";
      const name = names[trade.symbol] || trade.name || trade.symbol;
      return `
        <tr>
          <td>${escapeHtml(trade.time)}</td>
          <td><strong>${escapeHtml(name)}</strong><small>${escapeHtml(trade.symbol)}</small></td>
          <td class="${sideClass}">${trade.side}</td>
          <td>${Number(trade.shares).toFixed(4)}</td>
          <td>${priceMoney(trade)}</td>
          <td>${money(trade.value)}</td>
          <td>${signedMoney(trade.realizedPnl)}</td>
        </tr>
      `;
    })
    .join("");
}

function miniSparkline(points, tone = "up") {
  if (!points || points.length < 2) {
    return `<svg class="sparkline" viewBox="0 0 140 42" aria-hidden="true"></svg>`;
  }
  const values = points.map((point) => Number(point.price)).filter((value) => Number.isFinite(value));
  if (values.length < 2) return `<svg class="sparkline" viewBox="0 0 140 42" aria-hidden="true"></svg>`;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const path = values
    .map((value, idx) => {
      const x = (idx / (values.length - 1)) * 140;
      const y = 36 - ((value - min) / span) * 30;
      return `${idx === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  return `<svg class="sparkline ${tone}" viewBox="0 0 140 42" aria-hidden="true"><path d="${path}"></path></svg>`;
}

function renderChart(points) {
  const svg = $("equityChart");
  const width = svg.clientWidth || 900;
  const height = svg.clientHeight || 340;
  const pad = { top: 20, right: 18, bottom: 32, left: 62 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";
  if (!points || points.length < 2) return;

  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (idx) => pad.left + (idx / (points.length - 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + (1 - (value - min) / span) * (height - pad.top - pad.bottom);
  const path = points.map((point, idx) => `${idx === 0 ? "M" : "L"} ${x(idx).toFixed(2)} ${y(point.value).toFixed(2)}`).join(" ");
  const baselineY = y(points[0].value);
  svg.insertAdjacentHTML(
    "beforeend",
    `
      <line x1="${pad.left}" y1="${baselineY}" x2="${width - pad.right}" y2="${baselineY}" class="chart-baseline" />
      <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}" class="chart-axis" />
      <line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" class="chart-axis" />
      <path d="${path}" class="chart-line" />
      <text x="${pad.left}" y="${pad.top - 6}" class="chart-label">${money(max)}</text>
      <text x="${pad.left}" y="${height - 10}" class="chart-label">${money(min)}</text>
    `,
  );
}

async function refreshState() {
  try {
    const result = await getJson("/api/live/state");
    renderLive(result);
  } catch (error) {
    fail(error);
  }
}

async function onSuggest() {
  try {
    const data = await post("/api/symbols/suggest", {
      market: $("market").value,
      limit: 8,
    });
    const symbols = data.items.map((item) => item.symbol);
    $("symbols").value = symbols.join(", ");
    renderMessages([
      `후보 갱신: ${data.items.map((item) => `${item.name || item.symbol}(${item.symbol})`).join(", ")}`,
      ...(state.lastResult?.messages || []),
    ]);
  } catch (error) {
    fail(error);
  }
}

async function onSave() {
  try {
    const result = await post("/api/live/settings", settingsPayload());
    renderLive(result);
  } catch (error) {
    fail(error);
  }
}

function fail(error) {
  setStatus("error", "error");
  renderMessages([`오류: ${error.message}`, ...(state.lastResult?.messages || [])]);
}

$("suggestBtn").addEventListener("click", onSuggest);
$("saveBtn").addEventListener("click", onSave);
$("refreshBtn").addEventListener("click", refreshState);
window.addEventListener("resize", () => {
  if (state.lastResult) renderChart(state.lastResult.equityCurve);
});

refreshState();
state.poller = setInterval(refreshState, 10000);
