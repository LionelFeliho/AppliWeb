const state = { quotes: [], snapshots: [] };
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
    const detail = Array.isArray(payload.detail) ? payload.detail.join(" · ") : (payload.detail || response.statusText);
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

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
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
$("valuationDate").addEventListener("change", () => { $("maturityDate").value = addYears($("valuationDate").value, 1); });
$("loadButton").addEventListener("click", loadSnapshot);
$("uploadButton").addEventListener("click", uploadSettlement);
$("ecbButton").addEventListener("click", ingestEcb);
$("priceButton").addEventListener("click", priceZeroCoupon);
$("filterInput").addEventListener("input", () => renderQuotes(state.quotes));
$("apiKey").addEventListener("change", () => sessionStorage.setItem("xvaApiKey", $("apiKey").value));
$("apiKey").value = sessionStorage.getItem("xvaApiKey") || "";
checkHealth();
loadSnapshotInventory();
