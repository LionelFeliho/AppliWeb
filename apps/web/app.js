const state = { quotes: [], snapshots: [], g10: [], xccy: null };
const $ = (id) => document.getElementById(id);

function previousBusinessDay() {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

function addYears(isoDate, years) {
  const d = new Date(`${isoDate}T12:00:00`);
  d.setFullYear(d.getFullYear() + years);
  return d.toISOString().slice(0, 10);
}

function headers(contentType) {
  const result = {};
  if (contentType) result["Content-Type"] = contentType;
  const key = $("apiKey").value.trim();
  if (key) result["X-API-Key"] = key;
  return result;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = {};
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }
  if (!response.ok) {
    let detail = payload.detail || response.statusText;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || JSON.stringify(item)).join(" · ");
    throw new Error(detail);
  }
  return payload;
}

function toast(message, kind = "ok") {
  const element = $("toast");
  element.textContent = message;
  element.className = `show ${kind}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { element.className = ""; }, 4500);
}

function formatValue(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return value ?? "—";
  const abs = Math.abs(number);
  const digits = abs !== 0 && abs < 0.01 ? 8 : abs < 10 ? 6 : 4;
  return number.toLocaleString("fr-FR", { maximumFractionDigits: digits });
}

function formatMoney(value, currency) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number.toLocaleString("fr-FR", { maximumFractionDigits: 0 })} ${currency}`;
}

function formatCompact(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("fr-FR", { notation: "compact", maximumFractionDigits: 2 }).format(number);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function numberInput(id) {
  const value = Number($(id).value);
  if (!Number.isFinite(value)) throw new Error(`Valeur numérique invalide: ${id}`);
  return value;
}

function renderQuotes(quotes) {
  state.quotes = quotes;
  const filter = $("filterInput").value.trim().toLowerCase();
  const filtered = quotes.filter((quote) => JSON.stringify(quote).toLowerCase().includes(filter));
  const body = $("quotesBody");
  if (!filtered.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">Aucune quote pour ce filtre.</td></tr>';
    return;
  }
  body.innerHTML = filtered.map((quote) => {
    const observed = quote.metadata?.source_observation_date || quote.valuation_date;
    const stale = Number(quote.metadata?.staleness_days || 0);
    return `<tr>
      <td><strong>${escapeHtml(quote.canonical_id)}</strong><small>${escapeHtml(quote.unit)}</small></td>
      <td><span class="asset">${escapeHtml(quote.asset_class)}</span></td>
      <td>${escapeHtml(quote.quote_type)}</td>
      <td class="number">${formatValue(quote.normalized_value)}</td>
      <td class="number muted-cell">${formatValue(quote.raw_value)}</td>
      <td><code>${escapeHtml(quote.conversion)}</code></td>
      <td>${escapeHtml(quote.source)}<small>${escapeHtml(quote.source_symbol)}</small></td>
      <td>${escapeHtml(observed)}${stale ? `<small class="warning">${stale}d stale</small>` : ""}</td>
    </tr>`;
  }).join("");
}

function renderResolution(payload) {
  const snapshot = payload.snapshot;
  $("snapshotStatus").textContent = payload.exact_snapshot ? snapshot.status : `${snapshot.status} · FALLBACK`;
  $("requestedDate").textContent = payload.as_of_date;
  $("marketDate").textContent = payload.market_data_date;
  $("quoteCount").textContent = snapshot.quote_count;
  $("checksum").textContent = snapshot.checksum ? `${snapshot.checksum.slice(0, 10)}…` : "—";
  $("checksum").title = snapshot.checksum || "";
}

function resetSnapshotView() {
  $("snapshotStatus").textContent = "ABSENT";
  $("requestedDate").textContent = $("valuationDate").value || "—";
  $("marketDate").textContent = "—";
  $("quoteCount").textContent = "0";
  $("checksum").textContent = "—";
  renderQuotes([]);
}

async function loadSnapshotInventory() {
  try {
    state.snapshots = await requestJson("/api/v1/market-data/snapshots?limit=3650");
    $("snapshotDates").innerHTML = state.snapshots
      .map((snapshot) => `<option value="${escapeHtml(snapshot.valuation_date)}"></option>`)
      .join("");
  } catch (error) {
    console.warn("Unable to load snapshot inventory", error);
  }
}

async function loadSnapshot() {
  const asOfDate = $("valuationDate").value;
  const policy = $("snapshotPolicy").value;
  if (!asOfDate) return toast("Choisis une as-of date.", "error");
  try {
    const payload = await requestJson(`/api/v1/market-data/as-of/${asOfDate}/quotes?policy=${encodeURIComponent(policy)}`);
    renderResolution(payload);
    renderQuotes(payload.quotes);
    const suffix = payload.exact_snapshot ? "" : ` (market data ${payload.market_data_date})`;
    toast(`As-of ${asOfDate} chargée${suffix}.`);
  } catch (error) {
    resetSnapshotView();
    toast(error.message, "error");
  }
}

async function uploadSettlement() {
  const file = $("settlementFile").files[0];
  const date = $("valuationDate").value;
  if (!file) return toast("Sélectionne un fichier CSV.", "error");
  if (!date) return toast("Choisis la date EOD à alimenter.", "error");
  const replace = $("replaceQuotes").checked;
  try {
    const text = await file.text();
    const result = await requestJson(`/api/v1/market-data/ingestions/settlements?valuation_date=${date}&replace=${replace}`, {
      method: "POST", headers: headers("text/csv"), body: text,
    });
    toast(`${result.rows_inserted} ajoutées, ${result.rows_updated} remplacées, ${result.rows_unchanged} inchangées.`);
    $("snapshotPolicy").value = "exact";
    await loadSnapshotInventory();
    await loadSnapshot();
  } catch (error) { toast(error.message, "error"); }
}

async function ingestEcb() {
  const date = $("valuationDate").value;
  if (!date) return toast("Choisis la date EOD à alimenter.", "error");
  const replace = $("replaceQuotes").checked;
  try {
    const result = await requestJson(`/api/v1/market-data/ingestions/ecb?valuation_date=${date}&replace=${replace}`, {
      method: "POST", headers: headers(),
    });
    toast(`ECB: ${result.rows_inserted} ajoutées, ${result.rows_updated} remplacées.`);
    $("snapshotPolicy").value = "exact";
    await loadSnapshotInventory();
    await loadSnapshot();
  } catch (error) { toast(error.message, "error"); }
}

async function priceZeroCoupon() {
  const asOfDate = $("valuationDate").value;
  const policy = $("snapshotPolicy").value;
  try {
    const result = await requestJson("/api/v1/pricing/zero-coupon", {
      method: "POST", headers: headers("application/json"),
      body: JSON.stringify({
        as_of_date: asOfDate,
        maturity_date: $("maturityDate").value,
        notional: $("notional").value,
        currency: $("currency").value.trim().toUpperCase(),
        discount_rate_quote_id: $("rateQuoteId").value.trim(),
        compounding: $("compounding").value,
        snapshot_policy: policy,
      }),
    });
    const fallback = result.exact_snapshot ? "" : ` · marché ${result.market_data_date}`;
    $("pricingResult").innerHTML = `<strong>PV ${formatValue(result.present_value)} ${escapeHtml(result.currency)}</strong>
      <span>as-of ${escapeHtml(result.as_of_date)}${fallback} · DF ${formatValue(result.discount_factor)} · taux ${formatValue(result.discount_rate)}</span>`;
  } catch (error) {
    $("pricingResult").textContent = error.message;
    toast(error.message, "error");
  }
}

function syncReportingCurrencies() {
  const base = $("baseCurrency").value;
  const quote = $("quoteCurrency").value;
  if (base === quote && state.g10.length > 1) {
    $("quoteCurrency").value = state.g10.find((currency) => currency !== base) || "USD";
  }
  const actualQuote = $("quoteCurrency").value;
  const previous = $("reportingCurrency").value;
  $("reportingCurrency").innerHTML = [base, actualQuote]
    .filter((value, index, array) => array.indexOf(value) === index)
    .map((currency) => `<option value="${currency}">${currency}</option>`)
    .join("");
  $("reportingCurrency").value = [base, actualQuote].includes(previous) ? previous : actualQuote;
}

async function loadG10Reference() {
  try {
    const payload = await requestJson("/api/v1/reference/g10");
    state.g10 = payload.currencies.map((row) => row.currency);
  } catch (error) {
    console.warn("Unable to load G10 reference", error);
    state.g10 = ["EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK"];
  }
  const options = state.g10.map((currency) => `<option value="${currency}">${currency}</option>`).join("");
  $("baseCurrency").innerHTML = options;
  $("quoteCurrency").innerHTML = options;
  $("baseCurrency").value = "EUR";
  $("quoteCurrency").value = "USD";
  syncReportingCurrencies();
}

function svgChart(svgId, labels, series) {
  const svg = $(svgId);
  const width = 900;
  const height = 320;
  const left = 72;
  const right = 24;
  const top = 28;
  const bottom = 58;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const allValues = series.flatMap((item) => item.values.map(Number)).filter(Number.isFinite);
  if (!allValues.length) {
    svg.innerHTML = '<text x="450" y="160" text-anchor="middle" class="chart-empty">No data</text>';
    return;
  }
  let min = Math.min(0, ...allValues);
  let max = Math.max(0, ...allValues);
  if (min === max) { min -= 1; max += 1; }
  const margin = (max - min) * 0.08;
  min -= margin;
  max += margin;
  const x = (index) => left + (labels.length <= 1 ? chartWidth / 2 : (index / (labels.length - 1)) * chartWidth);
  const y = (value) => top + ((max - value) / (max - min)) * chartHeight;
  const parts = [];

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = min + ((max - min) * tick) / 4;
    const yy = y(value);
    parts.push(`<line x1="${left}" y1="${yy}" x2="${width - right}" y2="${yy}" class="chart-grid"/>`);
    parts.push(`<text x="${left - 10}" y="${yy + 4}" text-anchor="end" class="chart-axis-label">${escapeHtml(formatCompact(value))}</text>`);
  }
  if (min <= 0 && max >= 0) parts.push(`<line x1="${left}" y1="${y(0)}" x2="${width - right}" y2="${y(0)}" class="chart-zero"/>`);

  const labelIndexes = [...new Set([0, Math.floor((labels.length - 1) / 2), labels.length - 1])];
  labelIndexes.forEach((index) => {
    parts.push(`<text x="${x(index)}" y="${height - 22}" text-anchor="middle" class="chart-axis-label">${escapeHtml(labels[index])}</text>`);
  });

  series.forEach((item, seriesIndex) => {
    const points = item.values.map((value, index) => `${x(index)},${y(Number(value))}`).join(" ");
    parts.push(`<polyline points="${points}" class="chart-series series-${seriesIndex}" fill="none"/>`);
  });

  let legendX = left;
  series.forEach((item, index) => {
    parts.push(`<line x1="${legendX}" y1="14" x2="${legendX + 22}" y2="14" class="chart-series series-${index}"/>`);
    parts.push(`<text x="${legendX + 29}" y="18" class="chart-legend">${escapeHtml(item.name)}</text>`);
    legendX += 115;
  });
  svg.innerHTML = parts.join("");
}

function renderXccyResults(result) {
  state.xccy = result;
  $("xccyResults").classList.remove("hidden");
  const currency = result.reporting_currency;
  $("cleanPv").textContent = formatMoney(result.clean_pv, currency);
  $("adjustedPv").textContent = formatMoney(result.xva.xva_adjusted_pv, currency);
  $("cvaMetric").textContent = formatMoney(result.xva.cva, currency);
  $("dvaMetric").textContent = formatMoney(result.xva.dva, currency);
  $("fvaMetric").textContent = formatMoney(result.xva.fva, currency);
  $("colvaMetric").textContent = formatMoney(result.xva.colva, currency);
  $("baseCrossGamma").textContent = formatMoney(result.cross_gamma.fx_base_discount_interaction_pnl, currency);
  $("quoteCrossGamma").textContent = formatMoney(result.cross_gamma.fx_quote_discount_interaction_pnl, currency);
  $("mtmCurrency").textContent = currency;
  $("exposureCurrency").textContent = currency;

  const labels = result.exposure_profile.map((point) => point.date.slice(0, 7));
  svgChart("mtmChart", labels, [
    { name: "Forward MTM", values: result.exposure_profile.map((point) => point.forward_mtm) },
  ]);
  svgChart("exposureChart", labels, [
    { name: "EPE", values: result.exposure_profile.map((point) => point.epe) },
    { name: "ENE", values: result.exposure_profile.map((point) => point.ene) },
    { name: "PFE", values: result.exposure_profile.map((point) => point.pfe) },
  ]);

  const modeLabels = {
    native: "Native OIS curves",
    base_collateral: `${result.base_currency} collateral`,
    quote_collateral: `${result.quote_currency} collateral`,
  };
  $("discountComparison").innerHTML = Object.entries(result.pv_by_discounting).map(([mode, pv]) => {
    const selected = mode === result.selected_discounting_mode;
    const impact = result.discount_switch_impact[mode];
    return `<div class="comparison-row ${selected ? "selected" : ""}">
      <div><strong>${escapeHtml(modeLabels[mode] || mode)}</strong><small>${selected ? "Selected" : "Alternative"}</small></div>
      <div class="comparison-values"><strong>${escapeHtml(formatMoney(pv, currency))}</strong><small>Δ ${escapeHtml(formatMoney(impact, currency))}</small></div>
    </div>`;
  }).join("");
  $("xccyWarnings").innerHTML = result.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  $("xccyStatus").textContent = `Market ${result.market_data_date} · FX ${formatValue(result.fx_spot)} via ${result.fx_resolution} · ${result.assumptions.paths} paths · checksum ${result.snapshot_checksum.slice(0, 12)}…`;
}

async function priceXccy() {
  const button = $("priceXccyButton");
  button.disabled = true;
  button.textContent = "Calcul en cours…";
  $("xccyStatus").textContent = "Construction des courbes, pricing et simulation…";
  try {
    const payload = {
      as_of_date: $("valuationDate").value,
      maturity_date: $("xccyMaturity").value,
      snapshot_policy: $("snapshotPolicy").value,
      base_currency: $("baseCurrency").value,
      quote_currency: $("quoteCurrency").value,
      reporting_currency: $("reportingCurrency").value,
      notional_base: numberInput("xccyNotional"),
      pay_base: $("payBase").value === "true",
      base_spread_bps: numberInput("baseSpread"),
      quote_spread_bps: numberInput("quoteSpread"),
      payment_frequency_months: Number($("paymentFrequency").value),
      cross_currency_basis_bps: numberInput("xccyBasis"),
      discounting_mode: $("discountingMode").value,
      compare_discounting: true,
      simulation: {
        paths: Number($("simulationPaths").value),
        seed: 42,
        fx_volatility: numberInput("fxVolatility"),
        base_rate_volatility: numberInput("baseRateVol"),
        quote_rate_volatility: numberInput("quoteRateVol"),
        collateral_threshold: numberInput("collateralThreshold"),
        minimum_transfer_amount: numberInput("minimumTransferAmount"),
      },
      xva: {
        counterparty_spread_bps: numberInput("counterpartySpread"),
        own_spread_bps: numberInput("ownSpread"),
        funding_spread_bps: numberInput("fundingSpread"),
        collateral_spread_bps: numberInput("collateralSpread"),
      },
    };
    const result = await requestJson("/api/v1/pricing/xccy-swap", {
      method: "POST",
      headers: headers("application/json"),
      body: JSON.stringify(payload),
    });
    renderXccyResults(result);
    toast(`XCCY ${result.base_currency}/${result.quote_currency} calculé.`);
  } catch (error) {
    $("xccyStatus").textContent = error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Price XCCY & simulate exposure";
  }
}

async function checkHealth() {
  try {
    const result = await requestJson("/health");
    $("healthBadge").textContent = `${result.status.toUpperCase()} · ${result.mode}`;
    $("healthBadge").className = "badge ok";
  } catch {
    $("healthBadge").textContent = "API OFFLINE";
    $("healthBadge").className = "badge error";
  }
}

$("valuationDate").value = previousBusinessDay();
$("maturityDate").value = addYears($("valuationDate").value, 1);
$("xccyMaturity").value = addYears($("valuationDate").value, 5);
$("valuationDate").addEventListener("change", () => {
  $("maturityDate").value = addYears($("valuationDate").value, 1);
  $("xccyMaturity").value = addYears($("valuationDate").value, 5);
});
$("baseCurrency").addEventListener("change", syncReportingCurrencies);
$("quoteCurrency").addEventListener("change", syncReportingCurrencies);
$("loadButton").addEventListener("click", loadSnapshot);
$("uploadButton").addEventListener("click", uploadSettlement);
$("ecbButton").addEventListener("click", ingestEcb);
$("priceButton").addEventListener("click", priceZeroCoupon);
$("priceXccyButton").addEventListener("click", priceXccy);
$("filterInput").addEventListener("input", () => renderQuotes(state.quotes));
$("apiKey").addEventListener("change", () => sessionStorage.setItem("xvaApiKey", $("apiKey").value));
$("apiKey").value = sessionStorage.getItem("xvaApiKey") || "";
checkHealth();
loadSnapshotInventory();
loadG10Reference();
