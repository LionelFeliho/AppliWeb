(function () {
  state.xccySensitivities = null;
  state.lastXccySensitivityRequest = null;
  state.activeProductTab = "overview";

  function activateProductTab(tabName) {
    state.activeProductTab = tabName;
    document.querySelectorAll("[data-product-tab]").forEach((button) => {
      const active = button.dataset.productTab === tabName;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-product-panel]").forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.productPanel !== tabName);
    });
  }

  function currentXccyRequest() {
    return {
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
      sensitivities: {
        rate_bump_bps: numberInput("rateSensitivityBump"),
        fx_bump_relative: numberInput("fxSensitivityBumpPct") / 100,
        basis_bump_bps: numberInput("basisSensitivityBump"),
        spread_bump_bps: numberInput("xvaSensitivityBump"),
      },
    };
  }

  function mergedSensitivityRows(sensitivities) {
    const rows = new Map();
    const add = (point, role) => {
      const key = `${Number(point.time).toFixed(10)}|${point.tenor}`;
      const current = rows.get(key) || {
        tenor: point.tenor,
        time: Number(point.time),
        base: null,
        quote: null,
      };
      current[role] = point;
      rows.set(key, current);
    };
    sensitivities.base_curve.forEach((point) => add(point, "base"));
    sensitivities.quote_curve.forEach((point) => add(point, "quote"));
    return [...rows.values()].sort((left, right) => left.time - right.time);
  }

  function renderCurveLineage(result) {
    const render = (nodes) => nodes.map((node) => `<div class="lineage-row">
      <div><strong>${escapeHtml(node.tenor)}</strong><small>${escapeHtml(node.quote_id)}</small></div>
      <div class="lineage-values"><strong>${escapeHtml(formatValue(node.zero_rate))}</strong><small>DF ${escapeHtml(formatValue(node.discount_factor))}</small></div>
    </div>`).join("");
    $("baseCurveLineageTitle").textContent = `${result.base_currency} base curve`;
    $("quoteCurveLineageTitle").textContent = `${result.quote_currency} quote curve`;
    $("baseCurveLineage").innerHTML = render(result.base_curve.nodes);
    $("quoteCurveLineage").innerHTML = render(result.quote_curve.nodes);
  }

  function renderSensitivityResults(result) {
    const sensitivities = result.sensitivities;
    const summary = sensitivities.summary;
    const xva = sensitivities.xva;
    const currency = result.reporting_currency;
    const rows = mergedSensitivityRows(sensitivities);

    $("basePv01Label").textContent = `${result.base_currency} curve PV01`;
    $("quotePv01Label").textContent = `${result.quote_currency} curve PV01`;
    $("basePv01Metric").textContent = formatMoney(summary.base_curve_parallel_pv01, currency);
    $("quotePv01Metric").textContent = formatMoney(summary.quote_curve_parallel_pv01, currency);
    $("fxDeltaMetric").textContent = formatMoney(summary.fx_delta_1pct, currency);
    $("basisPv01Metric").textContent = formatMoney(summary.cross_currency_basis_pv01, currency);
    $("counterpartyCva01Metric").textContent = formatMoney(xva.counterparty_cva01, currency);
    $("ownDva01Metric").textContent = formatMoney(xva.own_dva01, currency);
    $("fundingFva01Metric").textContent = formatMoney(xva.funding_fva01, currency);
    $("collateralColva01Metric").textContent = formatMoney(xva.collateral_colva01, currency);
    $("sensitivityCurrency").textContent = `${currency} / bp`;
    $("baseSensitivityHeader").textContent = `${result.base_currency} PV01`;
    $("quoteSensitivityHeader").textContent = `${result.quote_currency} PV01`;

    svgChart("sensitivityChart", rows.map((row) => row.tenor), [
      { name: `${result.base_currency} curve`, values: rows.map((row) => row.base?.pv01 || 0) },
      { name: `${result.quote_currency} curve`, values: rows.map((row) => row.quote?.pv01 || 0) },
    ]);

    $("sensitivityMeta").textContent = `Central bump-and-reprice · rates ±${sensitivities.rate_bump_bps} bp · FX ±${formatValue(sensitivities.fx_bump_relative * 100)}% · basis ±${sensitivities.basis_bump_bps} bp. XVA spread 01s keep the simulated exposure profile fixed.`;
    $("sensitivityTableBody").innerHTML = rows.map((row) => {
      const base = row.base;
      const quote = row.quote;
      const basePv01 = Number(base?.pv01 || 0);
      const quotePv01 = Number(quote?.pv01 || 0);
      const lineage = [base?.quote_id, quote?.quote_id].filter(Boolean);
      return `<tr>
        <td><strong>${escapeHtml(row.tenor)}</strong><small>T ${escapeHtml(formatValue(row.time))}</small></td>
        <td class="number">${escapeHtml(formatMoney(basePv01, currency))}</td>
        <td class="number">${escapeHtml(formatMoney(quotePv01, currency))}</td>
        <td class="number"><strong>${escapeHtml(formatMoney(basePv01 + quotePv01, currency))}</strong></td>
        <td class="number">${escapeHtml(formatMoney(base?.gamma_per_bp2 || 0, currency))}</td>
        <td class="number">${escapeHtml(formatMoney(quote?.gamma_per_bp2 || 0, currency))}</td>
        <td>${lineage.map((quoteId) => `<code>${escapeHtml(quoteId)}</code>`).join("<br>")}</td>
      </tr>`;
    }).join("");
  }

  function resetSensitivityResults() {
    state.xccySensitivities = null;
    [
      "basePv01Metric", "quotePv01Metric", "fxDeltaMetric", "basisPv01Metric",
      "counterpartyCva01Metric", "ownDva01Metric", "fundingFva01Metric", "collateralColva01Metric",
    ].forEach((id) => { $(id).textContent = "—"; });
    $("sensitivityChart").innerHTML = '<text x="450" y="160" text-anchor="middle" class="chart-empty">Open the Sensitivities tab to calculate risk</text>';
    $("sensitivityTableBody").innerHTML = '<tr><td colspan="7" class="empty">Sensitivities have not been calculated for this trade.</td></tr>';
    $("sensitivityMeta").textContent = "";
    $("sensitivityLoadStatus").textContent = "Open this tab to calculate clean-PV and frozen-exposure XVA sensitivities.";
  }

  async function loadXccySensitivities(force = false) {
    if (!state.lastXccySensitivityRequest) {
      $("sensitivityLoadStatus").textContent = "Price an XCCY product first.";
      return;
    }
    if (state.xccySensitivities && !force) {
      renderSensitivityResults(state.xccySensitivities);
      return;
    }
    const button = $("refreshSensitivitiesButton");
    button.disabled = true;
    button.textContent = "Calculating…";
    $("sensitivityLoadStatus").textContent = "Bumping curve nodes and reusing the deterministic exposure simulation…";
    try {
      const result = await requestJson("/api/v1/pricing/xccy-swap/sensitivities", {
        method: "POST",
        headers: headers("application/json"),
        body: JSON.stringify(state.lastXccySensitivityRequest),
      });
      state.xccySensitivities = result;
      renderSensitivityResults(result);
      $("sensitivityLoadStatus").textContent = `Calculated on market ${result.market_data_date} · checksum ${result.snapshot_checksum.slice(0, 12)}…`;
    } catch (error) {
      $("sensitivityLoadStatus").textContent = error.message;
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Recalculate sensitivities";
    }
  }

  const baseRenderXccyResults = renderXccyResults;
  renderXccyResults = function (result) {
    state.lastXccySensitivityRequest = currentXccyRequest();
    baseRenderXccyResults(result);
    resetSensitivityResults();
    renderCurveLineage(result);
    activateProductTab(state.activeProductTab || "overview");
    if (state.activeProductTab === "sensitivities") loadXccySensitivities();
  };

  document.querySelectorAll("[data-product-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activateProductTab(button.dataset.productTab);
      if (button.dataset.productTab === "sensitivities") loadXccySensitivities();
    });
  });
  $("refreshSensitivitiesButton").addEventListener("click", () => {
    state.lastXccySensitivityRequest = currentXccyRequest();
    loadXccySensitivities(true);
  });
  activateProductTab("overview");
  resetSensitivityResults();
})();
