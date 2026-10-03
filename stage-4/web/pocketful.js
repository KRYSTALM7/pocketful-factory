(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const main = $("#main-content");
  const tokenKey = "pocketful.token";
  const state = { token: localStorage.getItem(tokenKey), me: null, currency: "EUR", minorUnits: 2 };
  let homeReadSequence = 0;
  let requestReadSequence = 0;
  let authorizationReadSequence = 0;
  const requestPayOperations = new Map();

  class NetworkFailure extends Error {}

  function freshKey() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    const bytes = new Uint8Array(24);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  }

  function operationTracker() {
    let key = null;
    let bodyText = null;
    const tracker = {
      changed() { key = null; bodyText = null; },
      forBody(body) {
        const nextBody = JSON.stringify(body);
        if (key === null || nextBody !== bodyText) {
          key = freshKey();
          bodyText = nextBody;
        }
        return key;
      }
    };
    return tracker;
  }

  async function api(path, { method = "GET", body, key } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store"
      });
    } catch (error) {
      throw new NetworkFailure(error && error.message ? error.message : "Connection interrupted");
    }
    if (response.status === 204 && method !== "GET") {
      // Financial and authentication writes return receipts. A successful status
      // without one leaves the operation outcome unknown, so retain its retry key.
      throw new NetworkFailure("The response could not be confirmed");
    }
    let data = null;
    if (response.status !== 204) {
      try { data = await response.json(); }
      catch (_) {
        // A successful write without its receipt is an unknown outcome. Preserve
        // the operation key/body so the caller can safely retry the same request.
        if (response.ok) throw new NetworkFailure("The response could not be confirmed");
        data = null;
      }
    }
    return { response, data };
  }

  function messageOf(result, fallback) {
    return result && result.data && result.data.error && result.data.error.message
      ? result.data.error.message : fallback;
  }

  function requireOk(result, fallback) {
    if (!result.response.ok) {
      const error = new Error(messageOf(result, fallback));
      error.status = result.response.status;
      error.payload = result.data;
      throw error;
    }
    return result.data;
  }

  function isUncertain(error) {
    return error instanceof NetworkFailure || (error && error.status >= 500);
  }

  function parseMoney(text) {
    const raw = String(text).trim();
    if (!raw || !/^\d+(?:\.\d*)?$/.test(raw)) return null;
    const pieces = raw.split(".");
    const whole = pieces[0];
    const fraction = pieces[1] || "";
    if (fraction.length > state.minorUnits) return null;
    const scale = 10n ** BigInt(state.minorUnits);
    try {
      const amount = BigInt(whole) * scale + BigInt(fraction.padEnd(state.minorUnits, "0") || "0");
      if (amount < 1n || amount > 1000000000n) return null;
      return Number(amount);
    } catch (_) {
      return null;
    }
  }

  function formatMoney(value, currency = state.currency, units = state.minorUnits) {
    let minor;
    try { minor = BigInt(String(value)); }
    catch (_) { minor = 0n; }
    const negative = minor < 0n;
    if (negative) minor = -minor;
    const scale = 10n ** BigInt(units);
    const whole = (minor / scale).toString();
    if (units === 0) return `${negative ? "−" : ""}${whole} ${currency}`;
    const fraction = (minor % scale).toString().padStart(units, "0");
    return `${negative ? "−" : ""}${whole}.${fraction} ${currency}`;
  }

  function decimalInput(value) {
    let minor;
    try { minor = BigInt(String(value)); }
    catch (_) { return ""; }
    const scale = 10n ** BigInt(state.minorUnits);
    const whole = (minor / scale).toString();
    if (!state.minorUnits) return whole;
    return `${whole}.${(minor % scale).toString().padStart(state.minorUnits, "0")}`;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function testNode(tag, testId, className, text) {
    const element = node(tag, className, text);
    element.dataset.testid = testId;
    return element;
  }

  function clearMessage(container, testId) {
    const current = $(`[data-testid="${testId}"]`, container);
    if (current) current.remove();
  }

  function showMessage(container, testId, text, type = "error-message") {
    clearMessage(container, testId);
    const message = testNode("div", testId, type, text);
    message.setAttribute("role", type === "error-message" ? "alert" : "status");
    container.prepend(message);
    return message;
  }

  function field(labelText, id, type = "text", placeholder = "", testId = id) {
    const wrap = node("div", "field");
    const label = node("label", "", labelText);
    label.htmlFor = id;
    const input = document.createElement(type === "select" ? "select" : "input");
    input.id = id;
    input.dataset.testid = testId;
    if (type !== "select") {
      input.type = type;
      input.placeholder = placeholder;
      if (type === "text") input.autocomplete = "off";
    }
    wrap.append(label, input);
    return { wrap, input };
  }

  function addCurrentIdentity() {
    const host = $("#account-nav");
    host.replaceChildren();
    if (!state.me || !state.token) return;
    const identity = testNode("span", "current-user", "account-name", state.me.display_name);
    const handle = testNode("span", "current-handle", "visually-hidden", state.me.handle);
    const logout = testNode("button", "logout-button", "logout-button", "Log out");
    logout.type = "button";
    logout.addEventListener("click", () => {
      localStorage.removeItem(tokenKey);
      state.token = null;
      state.me = null;
      location.assign("/login");
    });
    host.append(identity, handle, logout);
  }

  function setCurrentNavigation() {
    const path = location.pathname;
    for (const anchor of document.querySelectorAll(".main-nav a")) {
      if (anchor.getAttribute("href") === path || (path === "/" && anchor.getAttribute("href") === "/")) {
        anchor.setAttribute("aria-current", "page");
      } else {
        anchor.removeAttribute("aria-current");
      }
    }
  }

  function pageHeading(kicker, title, description) {
    const header = node("header", "page-heading");
    const copy = node("div");
    copy.append(node("p", "eyebrow", kicker), node("h1", "", title), node("p", "lede", description));
    header.append(copy);
    return header;
  }

  function emptyState(title, detail) {
    const wrap = node("div", "empty-state");
    wrap.append(node("span", "empty-icon", "✦"), node("strong", "", title), node("p", "", detail));
    return wrap;
  }

  function panelHeading(title, detail) {
    const wrap = node("div", "panel-heading");
    const copy = node("div");
    copy.append(node("h2", "", title));
    if (detail) copy.append(node("p", "", detail));
    wrap.append(copy);
    return wrap;
  }

  function makeNavForm(panelTitle, detail, formId, submitId, submitText) {
    const panel = node("section", "panel");
    panel.append(panelHeading(panelTitle, detail));
    const form = document.createElement("form");
    form.id = formId;
    form.className = "form-grid";
    form.noValidate = true;
    const feedback = node("div", "notice-list");
    form.append(feedback);
    const button = testNode("button", submitId, "primary-button", submitText);
    button.type = "submit";
    form.append(button);
    panel.append(form);
    return { panel, form, feedback, button };
  }

  function addVisibilitySelect(wrap, id, labelText = "Who can see this?") {
    const holder = node("div", "field");
    const label = node("label", "", labelText);
    label.htmlFor = id;
    const select = testNode("select", id, "");
    select.id = id;
    for (const [value, title] of [["public", "Friends · public"], ["private", "Only the people involved"]]) {
      const option = node("option", "", title);
      option.value = value;
      select.append(option);
    }
    holder.append(label, select);
    wrap.insertBefore(holder, wrap.querySelector("button[type='submit']"));
    return select;
  }

  function buildHome() {
    if (!state.me) {
      main.replaceChildren(pageHeading("Your money, together", "A little more connected.", "Sign in to see your wallet, pay someone, or request what you’re owed."));
      const cta = node("section", "panel auth-card");
      const links = node("div", "form-actions");
      const login = node("a", "primary-button", "Log in"); login.href = "/login";
      const signup = node("a", "secondary-button", "Create account"); signup.href = "/signup";
      links.append(login, signup); cta.append(links); main.append(cta); return;
    }
    main.replaceChildren(pageHeading("Your wallet", `Good to see you, ${state.me.display_name}.`, "A clear view of what’s yours to spend, plus the people and payments that make it move."));
    const grid = node("div", "layout-grid");
    const left = node("div", "stack");
    const wallet = testNode("section", "wallet-card", "panel wallet-hero");
    wallet.setAttribute("aria-label", "Wallet balance");
    const walletTop = node("div", "activity-top");
    const walletCopy = node("div");
    walletCopy.append(node("p", "eyebrow", "Ready when you are"));
    const available = testNode("strong", "wallet-available", "", "—");
    available.setAttribute("data-amount", "0");
    available.setAttribute("aria-label", "Available balance");
    walletCopy.append(available, node("p", "currency-caption", "Available to spend"));
    const refresh = testNode("button", "wallet-refresh", "wallet-refresh", "↻ Refresh");
    refresh.type = "button";
    walletTop.append(walletCopy, refresh);
    const breakdown = node("div", "wallet-breakdown");
    const totalStat = node("div", "wallet-stat");
    totalStat.append(node("span", "", "Total balance"));
    const total = testNode("strong", "wallet-balance", "", "—");
    total.dataset.amount = "0";
    totalStat.append(total);
    breakdown.append(totalStat);
    if (Number(state.me.held || 0) > 0) breakdown.append(createHeldStat(state.me.held));
    wallet.append(walletTop, breakdown);
    refresh.addEventListener("click", () => { void refreshHome(); });
    const homeError = node("div", "notice-list"); homeError.id = "home-notices";
    const activityPanel = node("section", "panel");
    activityPanel.append(panelHeading("Recent activity", "The latest payments visible to you."));
    const activityRegion = node("div"); activityRegion.id = "activity-region";
    activityPanel.append(activityRegion);
    left.append(wallet, homeError, activityPanel);

    const right = node("div", "stack");
    const pay = makeNavForm("Send a payment", "A little note makes it feel personal.", "pay-form", "pay-submit", "Send payment");
    const payTo = field("Pay who?", "pay-handle", "text", "Their handle");
    payTo.input.autocapitalize = "none"; payTo.input.autocomplete = "off";
    const payAmount = field("Amount", "pay-amount", "text", "0.00"); payAmount.input.inputMode = "decimal";
    const payNote = field("What’s it for?", "pay-note", "text", "Add a note (optional)"); payNote.input.maxLength = 200;
    pay.form.insertBefore(payTo.wrap, pay.button);
    pay.form.insertBefore(payAmount.wrap, pay.button);
    pay.form.insertBefore(payNote.wrap, pay.button);
    addVisibilitySelect(pay.form, "pay-visibility");
    const payTracker = operationTracker();
    for (const input of [payTo.input, payAmount.input, payNote.input, $("[data-testid='pay-visibility']", pay.form)]) {
      input.addEventListener("input", () => { payTracker.changed(); clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain"); clearMessage(pay.feedback, "pay-form-success"); });
      input.addEventListener("change", () => { payTracker.changed(); clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain"); clearMessage(pay.feedback, "pay-form-success"); });
    }
    pay.form.addEventListener("submit", async event => {
      event.preventDefault();
      clearMessage(pay.feedback, "pay-error"); clearMessage(pay.feedback, "pay-uncertain");
      const amount = parseMoney(payAmount.input.value);
      if (amount === null) { showMessage(pay.feedback, "pay-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { to_handle: payTo.input.value.trim(), amount, note: payNote.input.value, visibility: $("[data-testid='pay-visibility']", pay.form).value };
      const key = payTracker.forBody(body);
      await runMoneyAction(pay, async () => requireOk(await api("/payments", { method: "POST", body, key }), "Payment was not accepted."), "pay-error", "pay-uncertain", "Payment sent.");
    });

    const request = makeNavForm("Request money", "Ask someone to send you a specific amount.", "request-form", "request-submit", "Send request");
    const requestTo = field("Request from", "request-handle", "text", "Their handle");
    requestTo.input.autocapitalize = "none";
    const requestAmount = field("Amount", "request-amount", "text", "0.00"); requestAmount.input.inputMode = "decimal";
    const requestNote = field("Reason", "request-note", "text", "Add a note (optional)"); requestNote.input.maxLength = 200;
    request.form.insertBefore(requestTo.wrap, request.button); request.form.insertBefore(requestAmount.wrap, request.button); request.form.insertBefore(requestNote.wrap, request.button);
    const requestTracker = operationTracker();
    for (const input of [requestTo.input, requestAmount.input, requestNote.input]) input.addEventListener("input", () => { requestTracker.changed(); clearMessage(request.feedback, "request-error"); });
    request.form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(request.feedback, "request-error");
      const amount = parseMoney(requestAmount.input.value);
      if (amount === null) { showMessage(request.feedback, "request-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { payer_handle: requestTo.input.value.trim(), amount, note: requestNote.input.value };
      const key = requestTracker.forBody(body);
      const result = await runMoneyAction(request, async () => requireOk(await api("/requests", { method: "POST", body, key }), "Request was not sent."), "request-error", null, "Request sent.");
      if (result) { requestTo.input.value = ""; requestAmount.input.value = ""; requestNote.input.value = ""; requestTracker.changed(); }
    });

    const authorize = makeNavForm("Reserve for later", "Set money aside for someone to collect in one or more captures.", "authorize-form", "authorize-submit", "Create hold");
    const authTo = field("For who?", "authorize-handle", "text", "Their handle"); authTo.input.autocapitalize = "none";
    const authAmount = field("Amount to reserve", "authorize-amount", "text", "0.00"); authAmount.input.inputMode = "decimal";
    const authNote = field("What’s it for?", "authorize-note", "text", "Add a note (optional)"); authNote.input.maxLength = 200;
    authorize.form.insertBefore(authTo.wrap, authorize.button); authorize.form.insertBefore(authAmount.wrap, authorize.button); authorize.form.insertBefore(authNote.wrap, authorize.button);
    addVisibilitySelect(authorize.form, "authorize-visibility");
    const authorizeTracker = operationTracker();
    const authVisibility = $("[data-testid='authorize-visibility']", authorize.form);
    for (const input of [authTo.input, authAmount.input, authNote.input, authVisibility]) input.addEventListener("input", () => { authorizeTracker.changed(); clearMessage(authorize.feedback, "authorize-error"); });
    authorize.form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(authorize.feedback, "authorize-error");
      const amount = parseMoney(authAmount.input.value);
      if (amount === null) { showMessage(authorize.feedback, "authorize-error", `Enter an amount with no more than ${state.minorUnits} decimal places.`); return; }
      const body = { to_handle: authTo.input.value.trim(), amount, note: authNote.input.value, visibility: authVisibility.value };
      const key = authorizeTracker.forBody(body);
      await runMoneyAction(authorize, async () => requireOk(await api("/authorizations", { method: "POST", body, key }), "Hold was not created."), "authorize-error", null, "Money reserved.");
    });
    right.append(pay.panel, request.panel, authorize.panel);
    grid.append(left, right); main.append(grid);
    void refreshHome();
  }

  function createHeldStat(value) {
    const heldStat = node("div", "wallet-stat");
    heldStat.dataset.testid = "wallet-held-wrap";
    heldStat.append(node("span", "", "On hold"));
    const held = testNode("strong", "wallet-held", "", formatMoney(value));
    held.dataset.amount = String(value);
    heldStat.append(held);
    return heldStat;
  }

  async function runMoneyAction(formView, action, errorId, uncertainId, successText) {
    formView.button.disabled = true;
    try {
      await action();
      if (errorId) clearMessage(formView.feedback, errorId);
      if (uncertainId) clearMessage(formView.feedback, uncertainId);
      showMessage(formView.feedback, `${formView.form.id}-success`, successText, "success-message");
      await refreshHome();
      return true;
    } catch (error) {
      clearMessage(formView.feedback, `${formView.form.id}-success`);
      if (isUncertain(error) && uncertainId) {
        showMessage(formView.feedback, uncertainId, "We couldn’t confirm this payment yet. Your details are saved here—retry the same payment to check safely.", "uncertain-message");
      } else if (errorId) {
        showMessage(formView.feedback, errorId, error.message || "This action could not be completed.");
      }
      if (formView.form.id === "pay-form" || formView.form.id === "authorize-form") await refreshHome();
      return false;
    } finally {
      formView.button.disabled = false;
    }
  }

  async function refreshHome() {
    const region = $("#activity-region");
    if (!region || !state.token) return;
    const sequence = ++homeReadSequence;
    const notices = $("#home-notices");
    clearMessage(notices, "home-error");
    try {
      const [meResult, activityResult] = await Promise.all([api("/me"), api("/activity")]);
      const me = requireOk(meResult, "Could not refresh your wallet.");
      const activity = requireOk(activityResult, "Could not refresh activity.");
      if (sequence !== homeReadSequence) return;
      state.me = me; state.currency = me.currency; state.minorUnits = me.minor_units;
      const available = $("[data-testid='wallet-available']");
      const total = $("[data-testid='wallet-balance']");
      let held = $("[data-testid='wallet-held']");
      if (!available || !total) return;
      available.textContent = formatMoney(me.available === undefined ? me.balance : me.available);
      available.dataset.amount = String(me.available === undefined ? me.balance : me.available);
      total.textContent = formatMoney(me.total === undefined ? me.balance : me.total);
      total.dataset.amount = String(me.total === undefined ? me.balance : me.total);
      const heldWrap = $("[data-testid='wallet-held-wrap']");
      if (Number(me.held || 0) > 0) {
        if (!heldWrap) $(".wallet-breakdown").append(createHeldStat(me.held));
        else {
          held = $("[data-testid='wallet-held']", heldWrap);
          held.textContent = formatMoney(me.held);
          held.dataset.amount = String(me.held);
        }
      } else if (heldWrap) {
        heldWrap.remove();
      }
      renderActivity(region, activity.payments || []);
      addCurrentIdentity();
    } catch (error) {
      if (sequence !== homeReadSequence) return;
      showMessage(notices, "home-error", error.message || "Could not refresh your wallet.");
    }
  }

  function renderActivity(region, payments) {
    region.replaceChildren();
    if (!payments.length) {
      const empty = emptyState("Nothing here yet", "When you pay or receive money, the story will show up here.");
      empty.dataset.testid = "empty-activity";
      region.append(empty);
      return;
    }
    const list = testNode("div", "activity-list", "activity-list");
    for (const payment of payments) {
      const item = testNode("article", `activity-item-${payment.payment_id}`, "activity-item");
      item.dataset.visibility = payment.visibility;
      const top = node("div", "activity-top");
      const parties = testNode("div", `activity-parties-${payment.payment_id}`, "activity-parties", `${payment.from_handle} paid ${payment.to_handle}`);
      const amount = testNode("strong", `activity-amount-${payment.payment_id}`, "activity-amount", formatMoney(payment.amount, payment.currency || state.currency));
      top.append(parties, amount);
      const note = testNode("p", `activity-note-${payment.payment_id}`, "activity-note", payment.note || "");
      const meta = node("div", "row-meta");
      const privacy = node("span", `privacy-pill ${payment.visibility === "private" ? "private" : ""}`, payment.visibility === "private" ? "Private" : "Friends");
      meta.append(privacy);
      item.append(top, note, meta); list.append(item);
    }
    region.append(list);
  }

  function buildRequests() {
    main.replaceChildren(pageHeading("Between people", "Requests", "Keep track of what’s coming in and what you’ve asked for."));
    const panel = node("section", "panel");
    panel.append(panelHeading("Your requests", "Pay, decline, or cancel while a request is still pending."));
    const feedback = node("div", "notice-list"); feedback.id = "request-notices"; panel.append(feedback);
    const empty = emptyState("All caught up", "Requests you send or receive will appear here."); empty.dataset.testid = "empty-requests";
    const columns = node("div", "two-fields"); columns.classList.add("request-columns");
    const incomingSection = node("section", ""); incomingSection.append(node("h3", "", "Incoming"));
    const incoming = testNode("div", "incoming-list", "request-list");
    const outgoingSection = node("section", ""); outgoingSection.append(node("h3", "", "Outgoing"));
    const outgoing = testNode("div", "outgoing-list", "request-list");
    incomingSection.append(incoming); outgoingSection.append(outgoing); columns.append(incomingSection, outgoingSection);
    panel.append(empty, columns); main.append(panel);
    void refreshRequests();
  }

  async function refreshRequests() {
    const incoming = $("[data-testid='incoming-list']");
    const outgoing = $("[data-testid='outgoing-list']");
    if (!incoming || !outgoing || !state.token) return;
    const sequence = ++requestReadSequence;
    let result;
    try { result = await api("/requests?limit=200"); }
    catch (error) {
      if (sequence === requestReadSequence) showMessage($("#request-notices"), "request-error", error.message || "Could not load requests.");
      return;
    }
    if (sequence !== requestReadSequence) return;
    if (!result.response.ok) { showMessage($("#request-notices"), "request-error", messageOf(result, "Could not load requests.")); return; }
    incoming.replaceChildren(); outgoing.replaceChildren();
    const requests = result.data.requests || [];
    for (const request of requests) {
      const isIncoming = request.payer_id === state.me.user_id;
      const target = isIncoming ? incoming : outgoing;
      const row = testNode("article", `request-item-${request.request_id}`, "request-item");
      row.dataset.status = request.status;
      const top = node("div", "row-top");
      const copy = node("div");
      const other = isIncoming ? request.requester_handle : request.payer_handle;
      copy.append(node("div", "row-title", isIncoming ? `${other} asked you` : `You asked ${other}`));
      const status = node("span", `status-pill ${request.status}`, request.status);
      const amount = testNode("strong", `request-amount-${request.request_id}`, "row-amount", formatMoney(request.amount, request.currency || state.currency));
      top.append(copy, amount); row.append(top);
      const meta = node("div", "row-meta"); meta.append(status); row.append(meta);
      const note = node("p", "activity-note", request.note || ""); row.append(note);
      if (request.status === "pending") {
        const actions = node("div", "row-actions");
        if (isIncoming) {
          const pay = testNode("button", `request-pay-${request.request_id}`, "primary-button", "Pay request"); pay.type = "button";
          const decline = testNode("button", `request-decline-${request.request_id}`, "quiet-button", "Decline"); decline.type = "button";
          pay.addEventListener("click", () => handleRequestAction(request, "pay", pay));
          decline.addEventListener("click", () => handleRequestAction(request, "decline", decline));
          actions.append(pay, decline);
        } else {
          const cancel = testNode("button", `request-cancel-${request.request_id}`, "quiet-button", "Cancel request"); cancel.type = "button";
          cancel.addEventListener("click", () => handleRequestAction(request, "cancel", cancel));
          actions.append(cancel);
        }
        row.append(actions);
      }
      target.append(row);
    }
    const noRequests = requests.length === 0;
    $("[data-testid='empty-requests']").hidden = !noRequests;
    if (noRequests) { incoming.hidden = true; outgoing.hidden = true; }
    else { incoming.hidden = false; outgoing.hidden = false; }
  }

  async function handleRequestAction(request, action, button) {
    button.disabled = true;
    clearMessage($("#request-notices"), "request-error");
    try {
      let path = `/requests/${encodeURIComponent(request.request_id)}/${action}`;
      let result;
      if (action === "pay") {
        let operation = requestPayOperations.get(request.request_id);
        if (!operation) { operation = { key: freshKey(), bodyText: "{}" }; requestPayOperations.set(request.request_id, operation); }
        result = await api(path, { method: "POST", body: {}, key: operation.key });
      } else {
        result = await api(path, { method: "POST", body: {} });
      }
      requireOk(result, "This request could not be updated.");
      await refreshRequests();
      await refreshHomeIfMounted();
    } catch (error) {
      showMessage($("#request-notices"), "request-error", error.message || "This request could not be updated.");
      await refreshRequests();
      await refreshHomeIfMounted();
    } finally {
      button.disabled = false;
    }
  }

  async function refreshHomeIfMounted() {
    if ($("#activity-region")) await refreshHome();
  }

  function buildSplit() {
    main.replaceChildren(pageHeading("Make it even", "Split a shared cost", "Divide the amount in minor units. The first handles in your list receive any remainder."));
    const panel = node("section", "panel");
    panel.append(panelHeading("Create a split", "Preview the exact shares before you send requests."));
    const form = document.createElement("form"); form.className = "form-grid"; form.noValidate = true;
    const feedback = node("div", "notice-list");
    const amount = field("Total amount", "split-amount", "text", "0.00"); amount.input.inputMode = "decimal";
    const handles = field("Participants, in order", "split-handles", "text", "alex, sam, you");
    handles.input.autocapitalize = "none";
    const note = field("Note", "split-note", "text", "What did you share? (optional)"); note.input.maxLength = 200;
    const previewRegion = node("div"); previewRegion.id = "split-preview-region";
    const submit = testNode("button", "split-submit", "primary-button", "Create requests"); submit.type = "submit";
    form.append(feedback, amount.wrap, handles.wrap, note.wrap, previewRegion, submit);
    panel.append(form); main.append(panel);
    const tracker = operationTracker();
    for (const input of [amount.input, handles.input, note.input]) input.addEventListener("input", () => { tracker.changed(); clearMessage(feedback, "split-error"); refreshSplitPreview(previewRegion, amount.input.value, handles.input.value); });
    form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(feedback, "split-error");
      const total = parseMoney(amount.input.value);
      const participants = handles.input.value.split(",").map(value => value.trim());
      if (total === null || !participants.length || participants.some(value => !value) || new Set(participants).size !== participants.length) {
        showMessage(feedback, "split-error", "Check the amount and enter each participant handle once."); return;
      }
      const body = { amount: total, participant_handles: participants, note: note.input.value };
      const key = tracker.forBody(body);
      submit.disabled = true;
      try {
        requireOk(await api("/splits", { method: "POST", body, key }), "Split could not be created.");
        showMessage(feedback, "split-success", "Requests are on their way.", "success-message");
        await refreshSplitPreview(previewRegion, amount.input.value, handles.input.value);
      } catch (error) {
        showMessage(feedback, "split-error", error.message || "Split could not be created.");
      } finally { submit.disabled = false; }
    });
  }

  function refreshSplitPreview(region, rawAmount, rawHandles) {
    region.replaceChildren();
    const amount = parseMoney(rawAmount);
    const handles = rawHandles.split(",").map(value => value.trim());
    if (amount === null || !handles.length || handles.some(value => !value) || new Set(handles).size !== handles.length) return;
    const preview = testNode("section", "split-preview", "split-preview");
    preview.append(node("div", "split-preview-title", "Your share preview"));
    const count = BigInt(handles.length);
    const total = BigInt(amount);
    const base = total / count;
    const remainder = total % count;
    handles.forEach((handle, index) => {
      const row = node("div", "split-share-row");
      row.append(node("span", "", handle));
      const share = testNode("strong", `split-share-${handle}`, "", formatMoney(base + (BigInt(index) < remainder ? 1n : 0n)));
      row.append(share); preview.append(row);
    });
    region.append(preview);
  }

  function buildAuthorizations() {
    main.replaceChildren(pageHeading("Money set aside", "Authorizations", "See what’s reserved, capture money sent to you, or release a hold you created."));
    const panel = node("section", "panel");
    panel.append(panelHeading("Your holds", "Open holds reduce what’s available to spend. Captures move money."));
    const feedback = node("div", "notice-list"); feedback.id = "authorization-notices"; panel.append(feedback);
    const empty = emptyState("No holds yet", "Money you reserve or authorize for you to collect will show up here."); empty.dataset.testid = "empty-authorizations";
    const list = testNode("div", "authorization-list", "authorization-list");
    panel.append(empty, list); main.append(panel);
    void refreshAuthorizations();
  }

  async function refreshAuthorizations() {
    const list = $("[data-testid='authorization-list']");
    if (!list || !state.token) return;
    const sequence = ++authorizationReadSequence;
    let result;
    try { result = await api("/authorizations?limit=200"); }
    catch (error) {
      if (sequence === authorizationReadSequence) showMessage($("#authorization-notices"), "authorization-error", error.message || "Could not load authorizations.");
      return;
    }
    if (sequence !== authorizationReadSequence) return;
    if (!result.response.ok) { showMessage($("#authorization-notices"), "authorization-error", messageOf(result, "Could not load authorizations.")); return; }
    const authorizations = result.data.authorizations || [];
    list.replaceChildren();
    for (const authorization of authorizations) list.append(renderAuthorization(authorization));
    $("[data-testid='empty-authorizations']").hidden = authorizations.length > 0;
  }

  function renderAuthorization(authorization) {
    const id = authorization.authorization_id;
    const incoming = authorization.to_user_id === state.me.user_id;
    const row = testNode("article", `authorization-item-${id}`, "authorization-item");
    row.dataset.status = authorization.status;
    const top = node("div", "row-top");
    const copy = node("div");
    const person = incoming ? authorization.from_handle : authorization.to_handle;
    copy.append(node("div", "row-title", incoming ? `Reserved by ${person}` : `Reserved for ${person}`));
    const amount = testNode("strong", `authorization-amount-${id}`, "row-amount", formatMoney(authorization.amount, authorization.currency || state.currency));
    top.append(copy, amount); row.append(top);
    const status = node("span", `status-pill ${authorization.status}`, authorization.status);
    const meta = node("div", "row-meta"); meta.append(status); row.append(meta);
    const note = node("p", "activity-note", authorization.note || ""); row.append(note);
    const info = node("div", "authorization-info");
    const expiry = testNode("time", `authorization-expires-${id}`, "", authorization.expires_at);
    expiry.dateTime = authorization.expires_at;
    // The time element's text must stay the exact RFC 3339 value, so the label sits beside it.
    const expiryLine = node("span");
    expiryLine.append("Expires ", expiry);
    info.append(expiryLine);
    if (authorization.status === "captured") {
      info.append(testNode("span", `authorization-captured-${id}`, "", formatMoney(authorization.captured_amount, authorization.currency || state.currency)));
    } else if (authorization.status === "open" && Number(authorization.captured_amount || 0) > 0) {
      info.append(node("span", "", `${formatMoney(authorization.captured_amount, authorization.currency || state.currency)} captured so far`));
    }
    row.append(info);
    if (authorization.status === "open" && incoming) {
      const actions = node("div", "row-actions");
      const captureInput = field("Capture amount", `capture-input-${id}`, "text", "");
      captureInput.input.inputMode = "decimal";
      captureInput.input.dataset.testid = `authorization-capture-amount-${id}`;
      captureInput.input.value = decimalInput(authorization.remaining_amount === undefined ? authorization.amount - (authorization.captured_amount || 0) : authorization.remaining_amount);
      captureInput.wrap.classList.add("authorization-capture-field");
      const finalOption = node("label", "capture-final-option");
      const finalInput = document.createElement("input");
      finalInput.type = "checkbox";
      finalInput.checked = true;
      finalInput.dataset.testid = `authorization-capture-final-${id}`;
      const finalText = node("span", "", "Release the remaining hold after capture");
      finalOption.append(finalInput, finalText);
      const capture = testNode("button", `authorization-capture-${id}`, "primary-button", "Capture funds"); capture.type = "button";
      const tracker = operationTracker();
      captureInput.input.addEventListener("input", () => tracker.changed());
      finalInput.addEventListener("change", () => tracker.changed());
      capture.addEventListener("click", async () => {
        const amountMinor = parseMoney(captureInput.input.value);
        if (amountMinor === null) { showMessage($("#authorization-notices"), "authorization-error", `Enter a valid amount with up to ${state.minorUnits} decimal places.`); return; }
        clearMessage($("#authorization-notices"), "authorization-error");
        const body = { amount: amountMinor, final: finalInput.checked };
        const key = tracker.forBody(body);
        capture.disabled = true;
        try { requireOk(await api(`/authorizations/${encodeURIComponent(id)}/capture`, { method: "POST", body, key }), "Capture was refused."); await refreshAuthorizations(); }
        catch (error) { showMessage($("#authorization-notices"), "authorization-error", error.message || "Capture was refused."); await refreshAuthorizations(); }
        finally { capture.disabled = false; }
      });
      actions.append(captureInput.wrap, finalOption, capture); row.append(actions);
    } else if (authorization.status === "open" && !incoming) {
      const actions = node("div", "row-actions");
      const cancel = testNode("button", `authorization-void-${id}`, "quiet-button", "Release hold"); cancel.type = "button";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true; clearMessage($("#authorization-notices"), "authorization-error");
        try { requireOk(await api(`/authorizations/${encodeURIComponent(id)}/void`, { method: "POST", body: {} }), "Hold could not be released."); await refreshAuthorizations(); }
        catch (error) { showMessage($("#authorization-notices"), "authorization-error", error.message || "Hold could not be released."); await refreshAuthorizations(); }
        finally { cancel.disabled = false; }
      });
      actions.append(cancel); row.append(actions);
    }
    return row;
  }

  function buildAuthPage(mode) {
    const signup = mode === "signup";
    const panel = node("section", "panel auth-card");
    const logo = node("div", "auth-brand");
    logo.innerHTML = '<a class="brand" href="/" aria-label="Pocketful home"><span class="brand-mark" aria-hidden="true">p</span><span>Pocketful</span></a>';
    panel.append(logo, node("p", "eyebrow", signup ? "A fresh start" : "Welcome back"), node("h1", "", signup ? "Create your account" : "Good to have you back"), node("p", "lede", signup ? "Make room for money that moves with you." : "Pick up right where you left off."));
    const form = document.createElement("form"); form.className = "form-grid"; form.noValidate = true;
    const feedback = node("div", "notice-list");
    const email = field("Email address", `${mode}-email`, "email", "you@example.com"); email.input.autocomplete = "email";
    const password = field("Password", `${mode}-password`, "password", signup ? "At least 8 characters" : "Your password"); password.input.autocomplete = signup ? "new-password" : "current-password";
    form.append(feedback, email.wrap);
    if (signup) {
      const display = field("Name people will see", "signup-display-name", "text", "Your name"); display.input.autocomplete = "name"; form.append(display.wrap);
    }
    form.append(password.wrap);
    const submit = testNode("button", `${mode}-submit`, "primary-button", signup ? "Create account" : "Log in"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); clearMessage(feedback, "auth-error"); submit.disabled = true;
      const body = { email: email.input.value, password: password.input.value };
      if (signup) body.display_name = $("[data-testid='signup-display-name']", form).value;
      try {
        const result = await api(`/auth/${mode}`, { method: "POST", body });
        if (!result.response.ok) { showMessage(feedback, "auth-error", messageOf(result, signup ? "Could not create your account." : "Email or password is incorrect.")); return; }
        state.token = result.data.token; localStorage.setItem(tokenKey, state.token);
        location.assign("/");
      } catch (error) {
        showMessage(feedback, "auth-error", "We couldn’t reach Pocketful. Check your connection and try again.");
      } finally { submit.disabled = false; }
    });
    panel.append(form);
    const switcher = node("p", "auth-switch");
    if (signup) { switcher.append(document.createTextNode("Already with Pocketful? ")); const link = node("a", "", "Log in"); link.href = "/login"; switcher.append(link); }
    else { switcher.append(document.createTextNode("New to Pocketful? ")); const link = node("a", "", "Create an account"); link.href = "/signup"; switcher.append(link); }
    panel.append(switcher);
    main.replaceChildren(node("div", "auth-shell")); main.firstChild.append(panel);
  }

  async function init() {
    setCurrentNavigation();
    if (state.token) {
      try {
        const me = requireOk(await api("/me"), "Your session has expired.");
        state.me = me; state.currency = me.currency; state.minorUnits = me.minor_units;
      } catch (_) {
        localStorage.removeItem(tokenKey); state.token = null; state.me = null;
      }
    }
    addCurrentIdentity();
    const path = location.pathname;
    if (path === "/signup" || path === "/login") buildAuthPage(path.slice(1));
    else if (path === "/requests") state.me ? buildRequests() : buildSignedOut("requests");
    else if (path === "/split") state.me ? buildSplit() : buildSignedOut("split");
    else if (path === "/authorizations") state.me ? buildAuthorizations() : buildSignedOut("authorizations");
    else buildHome();
  }

  function buildSignedOut(destination) {
    main.replaceChildren(pageHeading("Pocketful", "Your wallet is waiting.", "Log in to see your requests and payment activity."));
    const panel = node("section", "panel");
    const link = node("a", "primary-button", "Log in"); link.href = `/login?next=${encodeURIComponent(destination)}`;
    panel.append(link); main.append(panel);
  }

  void init();
})();
