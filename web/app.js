"use strict";

const state = {
  scenario: null,
  options: null,
  result: null,
  previousBudget: 96,
  fireDrill: {
    phase: "waiting",
    frozenProof: null,
    frozenReceiptId: null,
    currentReceiptId: null,
    decision: null,
  },
};

const byId = (id) => document.getElementById(id);

function titleCase(value) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function setBusy(isBusy) {
  const button = byId("compile-button");
  button.disabled = isBusy;
  button.textContent = isBusy ? "Compiling…" : "Compile context";
  if (isBusy) {
    setHeaderStatus("loading", "COMPILING");
  }
}

function setHeaderStatus(kind, label) {
  const status = byId("header-status");
  status.classList.remove("is-loading", "is-failed");
  if (kind === "loading") status.classList.add("is-loading");
  if (kind === "failed") status.classList.add("is-failed");
  byId("header-status-label").textContent = label;
}

function optionCheckbox({ id, value, label, checked, group, type }) {
  const wrapper = document.createElement("label");
  wrapper.className = "choice";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.name = group;
  input.value = value;
  input.checked = checked;
  if (type) input.dataset.stateType = type;
  const text = document.createElement("span");
  text.textContent = label;
  wrapper.append(input, text);
  return wrapper;
}

function switchControl({ id, label, checked }) {
  const wrapper = document.createElement("label");
  wrapper.className = "switch-row";
  const text = document.createElement("span");
  text.textContent = label;
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = `control-${id}`;
  input.name = "adversarial_control";
  input.value = id;
  input.checked = checked;
  const track = document.createElement("span");
  track.className = "switch-track";
  track.setAttribute("aria-hidden", "true");
  wrapper.append(text, input, track);
  return wrapper;
}

function populateContract(payload) {
  state.scenario = payload.scenario;
  state.options = payload.options;
  const contract = payload.scenario.contract;
  const controls = payload.scenario.controls;

  byId("task").value = contract.task;
  byId("as-of").value = contract.as_of;
  byId("project").value = contract.project;
  byId("scope").value = contract.scope;
  byId("token-budget").value = contract.token_budget;
  state.previousBudget = contract.token_budget;

  const sourceRoot = byId("source-options");
  sourceRoot.replaceChildren(
    ...payload.options.sources.map((source) =>
      optionCheckbox({
        id: `source-${source}`,
        value: source,
        label: titleCase(source),
        checked: contract.allowed_sources.includes(source),
        group: "allowed_source",
      }),
    ),
  );

  const authorityRoot = byId("authority-options");
  authorityRoot.replaceChildren(
    ...payload.options.authorities.map((authority) =>
      optionCheckbox({
        id: `authority-${authority}`,
        value: authority,
        label: titleCase(authority),
        checked: contract.allowed_authorities.includes(authority),
        group: "allowed_authority",
      }),
    ),
  );

  const requiredRoot = byId("required-options");
  requiredRoot.replaceChildren(
    ...payload.options.required_records.map((record) =>
      optionCheckbox({
        id: `required-${record.id}`,
        value: record.id,
        label: `${record.id} · ${record.title}`,
        checked: contract.required_record_ids.includes(record.id),
        group: "required_record",
      }),
    ),
  );

  const forbiddenRoot = byId("forbidden-options");
  const lifecycle = payload.options.lifecycle_states.map((value) =>
    optionCheckbox({
      id: `lifecycle-${value}`,
      value,
      label: titleCase(value),
      checked: contract.forbidden_lifecycle_states.includes(value),
      group: "forbidden_state",
      type: "lifecycle",
    }),
  );
  const sensitivity = payload.options.sensitivity_states.map((value) =>
    optionCheckbox({
      id: `sensitivity-${value}`,
      value,
      label: titleCase(value),
      checked: contract.forbidden_sensitivity_states.includes(value),
      group: "forbidden_state",
      type: "sensitivity",
    }),
  );
  forbiddenRoot.replaceChildren(...lifecycle, ...sensitivity);

  const controlsRoot = byId("control-options");
  controlsRoot.replaceChildren(
    ...payload.options.controls.map((control) =>
      switchControl({
        id: control.id,
        label: control.label,
        checked: controls[control.id],
      }),
    ),
  );

  byId("control-tight_token_budget").addEventListener("change", (event) => {
    const budget = byId("token-budget");
    if (event.target.checked) {
      state.previousBudget = Number(budget.value) || contract.token_budget;
      budget.value = "12";
    } else if (Number(budget.value) === 12) {
      budget.value = String(state.previousBudget);
    }
  });
}

function checkedValues(name, predicate = () => true) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)]
    .filter(predicate)
    .map((input) => input.value);
}

function readContract() {
  return {
    task: byId("task").value.trim(),
    as_of: byId("as-of").value,
    project: byId("project").value.trim(),
    scope: byId("scope").value.trim(),
    allowed_sources: checkedValues("allowed_source"),
    allowed_authorities: checkedValues("allowed_authority"),
    required_record_ids: checkedValues("required_record"),
    forbidden_lifecycle_states: checkedValues(
      "forbidden_state",
      (input) => input.dataset.stateType === "lifecycle",
    ),
    forbidden_sensitivity_states: checkedValues(
      "forbidden_state",
      (input) => input.dataset.stateType === "sensitivity",
    ),
    token_budget: Number(byId("token-budget").value),
  };
}

function readControls() {
  const controls = {};
  document.querySelectorAll('input[name="adversarial_control"]').forEach((input) => {
    controls[input.value] = input.checked;
  });
  return controls;
}

function receiptProof(result) {
  return {
    receipt_id: result.receipt_id,
    contract_fingerprint: result.contract_fingerprint,
    packet_fingerprint: result.packet_fingerprint,
  };
}

function writeContract(contract) {
  byId("task").value = contract.task;
  byId("as-of").value = contract.as_of;
  byId("project").value = contract.project;
  byId("scope").value = contract.scope;
  byId("token-budget").value = contract.token_budget;
  state.previousBudget = contract.token_budget;

  const groups = {
    allowed_source: contract.allowed_sources,
    allowed_authority: contract.allowed_authorities,
    required_record: contract.required_record_ids,
  };
  Object.entries(groups).forEach(([name, values]) => {
    const selected = new Set(values);
    document.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
      input.checked = selected.has(input.value);
    });
  });

  const forbiddenLifecycle = new Set(contract.forbidden_lifecycle_states);
  const forbiddenSensitivity = new Set(contract.forbidden_sensitivity_states);
  document.querySelectorAll('input[name="forbidden_state"]').forEach((input) => {
    const selected = input.dataset.stateType === "lifecycle"
      ? forbiddenLifecycle
      : forbiddenSensitivity;
    input.checked = selected.has(input.value);
  });
}

function renderFireDrill() {
  const drill = state.fireDrill;
  const section = byId("fire-drill");
  const button = byId("fire-drill-button");
  section.classList.remove("is-stale", "is-blocked", "is-current");

  const activeIndex = {
    ready: 0,
    stale: 1,
    blocked: 1,
    current: 2,
  }[drill.phase];
  section.querySelectorAll("[data-drill-step]").forEach((step, index) => {
    step.classList.toggle("is-done", Number.isInteger(activeIndex) && index < activeIndex);
    step.classList.toggle("is-active", index === activeIndex);
  });

  byId("fire-drill-frozen").textContent = drill.frozenReceiptId || "—";
  byId("fire-drill-current").textContent = drill.currentReceiptId || "—";
  button.disabled = false;

  if (drill.phase === "ready") {
    byId("fire-drill-title").textContent = "RECEIPT FROZEN — CURRENT";
    byId("fire-drill-fact").textContent =
      "Attempt continuation now, or declare the synthetic late user correction.";
    byId("fire-drill-reason").textContent = "RECEIPT_CURRENT";
    button.textContent = "Inject late correction";
    return;
  }

  if (drill.phase === "stale") {
    section.classList.add("is-stale");
    byId("fire-drill-title").textContent = "STALE — RECOMPILE REQUIRED";
    byId("fire-drill-fact").textContent = drill.decision.boundary_fact;
    byId("fire-drill-reason").textContent = drill.decision.reason_code;
    byId("fire-drill-current").textContent = "recompile pending";
    button.textContent = "Recompile current context";
    return;
  }

  if (drill.phase === "current") {
    section.classList.add("is-current");
    byId("fire-drill-title").textContent = "CURRENT — NEW RECEIPT";
    byId("fire-drill-fact").textContent =
      "The late correction is now required context and the new receipt may continue.";
    byId("fire-drill-reason").textContent = "RECEIPT_CURRENT";
    button.textContent = "Recompile complete";
    button.disabled = true;
    return;
  }

  if (drill.phase === "blocked") {
    section.classList.add("is-blocked");
    byId("fire-drill-title").textContent = "BLOCKED — NO CURRENT PACKET";
    byId("fire-drill-fact").textContent = drill.decision.boundary_fact;
    byId("fire-drill-reason").textContent = drill.decision.reason_code;
    button.textContent = "Resolve compile failure first";
    button.disabled = true;
    return;
  }

  byId("fire-drill-title").textContent = "Receipt unavailable";
  byId("fire-drill-fact").textContent = "Compile a valid packet to freeze its proof.";
  byId("fire-drill-reason").textContent = "WAITING_FOR_RECEIPT";
  button.textContent = "Inject late correction";
  button.disabled = true;
}

function freezeFireDrill(result) {
  if (result.status !== "compiled") {
    state.fireDrill = {
      phase: "unavailable",
      frozenProof: null,
      frozenReceiptId: null,
      currentReceiptId: null,
      decision: null,
    };
  } else {
    state.fireDrill = {
      phase: "ready",
      frozenProof: receiptProof(result),
      frozenReceiptId: result.receipt_id,
      currentReceiptId: result.receipt_id,
      decision: null,
    };
  }
  renderFireDrill();
}

function decisionSymbol(selected) {
  const symbol = document.createElement("span");
  symbol.className = "decision-symbol";
  symbol.setAttribute("aria-hidden", "true");
  if (selected) symbol.dataset.selected = "true";
  return symbol;
}

function recordCard(record, mode) {
  const warning = record.warning_code;
  const card = document.createElement("article");
  card.className = "record-card";
  if (warning) card.classList.add("is-illegal");
  if (mode === "gate") card.classList.add("is-selected");
  if (record.required) card.classList.add("is-required");

  const top = document.createElement("div");
  top.className = "record-topline";
  top.append(decisionSymbol(mode === "gate"));

  const title = document.createElement("div");
  title.className = "record-title";
  const id = document.createElement("span");
  id.className = "record-id";
  id.textContent = record.id;
  const name = document.createElement("span");
  name.className = "record-name";
  name.textContent = record.title;
  title.append(id, name);

  const tokens = document.createElement("span");
  tokens.className = "record-tokens";
  tokens.textContent = `${record.token_count} tkn`;
  top.append(title, tokens);

  const meta = document.createElement("p");
  meta.className = "record-meta";
  const facts = mode === "naive"
    ? [
        `Source: ${titleCase(record.source_class)}`,
        `Lifecycle: ${titleCase(record.lifecycle)}`,
        `Valid: ${record.valid_from}`,
      ]
    : [
        `Source: ${titleCase(record.source_class)}`,
        `By: ${titleCase(record.authority)}`,
        `Provenance: ${record.provenance.length}`,
      ];
  facts.forEach((fact) => {
    const span = document.createElement("span");
    span.textContent = fact;
    meta.append(span);
  });

  card.append(top, meta);
  if (warning) {
    const reason = document.createElement("code");
    reason.className = "reason-label";
    reason.textContent = warning;
    reason.title = record.warning_fact || warning;
    card.append(reason);
  } else if (record.required) {
    const required = document.createElement("span");
    required.className = "required-label";
    required.textContent = "REQUIRED";
    card.append(required);
  }
  return card;
}

function summaryBlock(primary, secondary) {
  const primaryNode = document.createElement("span");
  primaryNode.className = "summary-primary";
  primaryNode.textContent = primary;
  const secondaryNode = document.createElement("span");
  secondaryNode.className = "summary-secondary";
  secondaryNode.textContent = secondary;
  return [primaryNode, secondaryNode];
}

function failureCard(failure, trace) {
  const card = document.createElement("article");
  card.className = "failure-card";
  const heading = document.createElement("h3");
  heading.textContent = failure.title;
  const message = document.createElement("p");
  message.className = "failure-message";
  message.textContent = failure.message;
  const code = document.createElement("code");
  code.className = "failure-code";
  code.textContent = failure.reason_code;
  const fact = document.createElement("p");
  fact.className = "failure-fact";
  const matching = trace.find((row) => row.reason_code === failure.reason_code);
  fact.textContent = matching
    ? `Boundary fact · ${matching.boundary_fact}`
    : "Required records must satisfy every declared boundary.";
  card.append(heading, message, code, fact);
  return card;
}

function traceRow(row) {
  const wrapper = document.createElement("article");
  wrapper.className = `trace-row ${row.selected ? "is-selected" : "is-excluded"}`;

  const record = document.createElement("div");
  record.className = "trace-record";
  record.append(decisionSymbol(row.selected));
  const recordText = document.createElement("div");
  recordText.className = "trace-record-text";
  const id = document.createElement("strong");
  id.textContent = row.record_id;
  const title = document.createElement("span");
  title.textContent = row.title;
  recordText.append(id, title);
  record.append(recordText);

  const reason = document.createElement("div");
  reason.className = "trace-reason";
  const code = document.createElement("code");
  code.textContent = row.reason_code;
  const fact = document.createElement("span");
  fact.textContent = row.boundary_fact;
  reason.append(code, fact);

  const tokens = document.createElement("span");
  tokens.className = "trace-token";
  tokens.textContent = String(row.token_count);
  wrapper.append(record, reason, tokens);
  return wrapper;
}

function renderResult(result) {
  state.result = result;
  const compiled = result.status === "compiled";
  setHeaderStatus(compiled ? "compiled" : "failed", compiled ? "COMPILED" : "FAIL CLOSED");
  byId("receipt-id").textContent = result.receipt_id;

  byId("naive-count").textContent = `${result.naive.selected_count} selected`;
  byId("naive-records").replaceChildren(
    ...result.naive.selected_records.map((record) => recordCard(record, "naive")),
  );
  byId("naive-summary").replaceChildren(
    ...summaryBlock(
      `${result.naive.token_count} / ${result.contract.token_budget} tokens`,
      `${result.naive.illegal_selected_count} illegal · ${result.naive.missing_required_ids.length} required missed`,
    ),
  );

  const gateRecords = byId("gate-records");
  const gateSummary = byId("gate-summary");
  const gateCaption = byId("gate-caption");
  const copyButton = byId("copy-button");
  byId("gate-count").textContent = compiled ? `${result.summary.selected_count} selected` : "0 selected";
  gateSummary.classList.toggle("is-success", compiled);
  gateSummary.classList.toggle("is-failed", !compiled);
  copyButton.disabled = !compiled;
  copyButton.querySelector("span").textContent = compiled ? "Copy packet" : "No packet to copy";
  byId("copy-status").textContent = "";

  if (compiled) {
    gateCaption.textContent = "Legal and complete";
    gateCaption.className = "success-text";
    gateRecords.replaceChildren(...result.packet.map((record) => recordCard(record, "gate")));
    gateSummary.replaceChildren(
      ...summaryBlock(
        `${result.summary.token_count} / ${result.contract.token_budget} tokens`,
        `${result.summary.remaining_tokens} tokens remaining`,
      ),
    );
  } else {
    gateCaption.textContent = "Required boundary failed";
    gateCaption.className = "danger-text";
    gateRecords.replaceChildren(failureCard(result.failure, result.trace));
    gateSummary.replaceChildren(
      ...summaryBlock(`0 / ${result.contract.token_budget} tokens`, "Compile blocked · no packet emitted"),
    );
  }

  byId("trace-count").textContent = `${result.trace.length} decisions`;
  byId("trace-list").replaceChildren(...result.trace.map(traceRow));
  byId("trace-caption").textContent = compiled
    ? "Every supplied candidate gets one decision."
    : "The required failure is explicit; legal optional records are withheld.";

  const previewRows = result.trace.slice(0, 3).map(traceRow);
  byId("mobile-trace-rows").replaceChildren(...previewRows);
  byId("view-all-decisions").textContent = `View all ${result.trace.length} decisions`;

  byId("audit-tokens").textContent = `${result.summary.token_count} / ${result.contract.token_budget} tokens`;
  byId("audit-selected").textContent = `${result.summary.selected_count} selected`;
  byId("audit-selected-note").textContent = compiled ? "legal and included" : "compile blocked";
  byId("audit-excluded").textContent = `${result.summary.excluded_count} excluded`;
  byId("audit-principle-text").textContent = compiled
    ? "Relevance is optimized only after legality."
    : "Required records must be legal, supported, and budget-fit.";
  byId("principle").classList.toggle("is-failed", !compiled);
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function requestCompile(contract, controls) {
  return postJson("/api/compile", { contract, controls });
}

async function requestStaleness(priorReceipt, change, currentContract, controls) {
  return postJson("/api/fire-drill", {
    prior_receipt: priorReceipt,
    change,
    current_contract: currentContract,
    controls,
  });
}

async function compileContext(event) {
  if (event) event.preventDefault();
  const formError = byId("form-error");
  formError.hidden = true;
  setBusy(true);
  try {
    const payload = await requestCompile(readContract(), readControls());
    renderResult(payload);
    freezeFireDrill(payload);
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
    setHeaderStatus("failed", "INPUT ERROR");
  } finally {
    setBusy(false);
  }
}

async function runFireDrill() {
  const formError = byId("form-error");
  const button = byId("fire-drill-button");
  formError.hidden = true;
  button.disabled = true;
  try {
    const fixture = state.scenario.fire_drill;
    const controls = readControls();
    if (state.fireDrill.phase === "ready") {
      button.textContent = "Checking old receipt…";
      const decision = await requestStaleness(
        state.fireDrill.frozenProof,
        fixture.change,
        fixture.current_contract,
        controls,
      );
      state.fireDrill.decision = decision;
      state.fireDrill.currentReceiptId = null;
      state.fireDrill.phase = decision.outcome === "recompile_required"
        ? "stale"
        : decision.outcome === "continue"
          ? "ready"
          : "blocked";
      renderFireDrill();
      setHeaderStatus(
        decision.outcome === "continue" ? "compiled" : "failed",
        decision.outcome === "continue"
          ? "CURRENT"
          : decision.outcome === "block"
            ? "BLOCKED"
            : "STALE",
      );
      return;
    }

    if (state.fireDrill.phase === "stale") {
      button.textContent = "Recompiling…";
      writeContract(fixture.current_contract);
      const result = await requestCompile(fixture.current_contract, controls);
      renderResult(result);
      if (result.status !== "compiled") {
        state.fireDrill.phase = "blocked";
        state.fireDrill.decision = {
          reason_code: result.failure.reason_code,
          boundary_fact: result.failure.message,
        };
        renderFireDrill();
        return;
      }
      const confirmation = await requestStaleness(
        receiptProof(result),
        { kind: "none", summary: "", required_record_ids: [] },
        fixture.current_contract,
        controls,
      );
      if (confirmation.outcome !== "continue") {
        throw new Error(`New receipt was not current: ${confirmation.reason_code}`);
      }
      state.result = result;
      state.fireDrill.phase = "current";
      state.fireDrill.currentReceiptId = result.receipt_id;
      state.fireDrill.decision = confirmation;
      renderFireDrill();
      setHeaderStatus("compiled", "CURRENT");
    }
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
    setHeaderStatus("failed", "DRILL ERROR");
    button.disabled = false;
  }
}

async function copyPacket() {
  if (!state.result || state.result.status !== "compiled") return;
  const copyStatus = byId("copy-status");
  try {
    await navigator.clipboard.writeText(state.result.packet_text);
    copyStatus.textContent = "Packet copied with provenance and token accounting.";
  } catch (_error) {
    const textarea = document.createElement("textarea");
    textarea.value = state.result.packet_text;
    textarea.setAttribute("readonly", "");
    textarea.className = "clipboard-proxy";
    document.body.append(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
    copyStatus.textContent = "Packet copied with provenance and token accounting.";
  }
}

function activateTab(tabName) {
  document.querySelectorAll(".mobile-tabs button").forEach((button) => {
    const active = button.dataset.tab === tabName;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  document.querySelectorAll(".mobile-pane").forEach((pane) => {
    pane.classList.toggle("is-mobile-active", pane.dataset.pane === tabName);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function start() {
  byId("contract-form").addEventListener("submit", compileContext);
  byId("copy-button").addEventListener("click", copyPacket);
  byId("fire-drill-button").addEventListener("click", runFireDrill);
  document.querySelectorAll(".mobile-tabs button").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });
  byId("view-all-decisions").addEventListener("click", () => activateTab("trace"));

  try {
    const response = await fetch("/api/scenario");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load scenario");
    populateContract(payload);
    await compileContext();
  } catch (error) {
    byId("form-error").textContent = error.message;
    byId("form-error").hidden = false;
    setHeaderStatus("failed", "LOAD ERROR");
    setBusy(false);
  }
}

document.addEventListener("DOMContentLoaded", start);
