const elements = {
  loginPanel: document.querySelector("#login-panel"),
  appPanel: document.querySelector("#app-panel"),
  headerActions: document.querySelector("#header-actions"),
  indicator: document.querySelector("#service-indicator"),
  settingsForm: document.querySelector("#settings-form"),
  devices: document.querySelector("#devices-list"),
  channels: document.querySelector("#channel-list"),
  discovered: document.querySelector("#discovered-devices"),
  channelResults: document.querySelector("#channel-results"),
  logOutput: document.querySelector("#log-output"),
  toast: document.querySelector("#toast"),
};

let csrfToken = "";
let settings = null;
let logPolling = null;
let toastTimeout = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  if (csrfToken && options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}

function notify(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => { elements.toast.hidden = true; }, 4000);
}

function showLogin() {
  csrfToken = "";
  elements.appPanel.hidden = true;
  elements.headerActions.hidden = true;
  elements.loginPanel.hidden = false;
}

async function showApp(token) {
  csrfToken = token;
  elements.loginPanel.hidden = true;
  elements.appPanel.hidden = false;
  elements.headerActions.hidden = false;
  settings = await api("/api/config");
  renderSettings();
  await refreshService();
  await refreshLogs();
  clearInterval(logPolling);
  logPolling = setInterval(() => {
    if (!document.querySelector("#auto-refresh")?.checked) return;
    if (!document.querySelector("#logs-tab").hidden) refreshLogs();
    refreshService();
  }, 3000);
}

function renderDevices() {
  if (!settings.devices.length) {
    elements.devices.innerHTML = '<div class="empty-state">No devices paired yet. Add one below to get started.</div>';
    return;
  }
  elements.devices.innerHTML = settings.devices.map((device, index) => `
    <div class="device-row" data-index="${index}">
      <label class="id-label">Screen ID<input data-field="screen_id" value="${escapeHtml(device.screen_id)}" readonly></label>
      <label>Device name<input data-field="name" maxlength="200" value="${escapeHtml(device.name || "")}"></label>
      <label class="offset-label">Skip offset (ms)<input data-field="offset" type="number" min="-60000" max="60000" step="10" value="${Number(device.offset) || 0}"></label>
      <button class="button button-quiet remove-device" type="button">Remove</button>
    </div>`).join("");
}

function renderChannels() {
  if (!settings.channel_whitelist.length) {
    elements.channels.innerHTML = '<div class="empty-state">No channels have been whitelisted.</div>';
    return;
  }
  elements.channels.innerHTML = settings.channel_whitelist.map((channel, index) => `
    <div class="channel-row" data-index="${index}">
      <span>${escapeHtml(channel.name || "Unnamed channel")}<small>${escapeHtml(channel.id)}</small></span>
      <button class="button button-quiet remove-channel" type="button">Remove</button>
    </div>`).join("");
}

function renderSettings() {
  renderDevices();
  renderChannels();
  const form = elements.settingsForm;
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(key);
    if (input && input.type === "checkbox") input.checked = Boolean(value);
    else if (input && key !== "apikey") input.value = value;
  }
  form.querySelectorAll('input[name="category"]').forEach((input) => {
    input.checked = settings.skip_categories.includes(input.value);
  });
  form.elements.apikey.value = settings.apikey || "";
}

function collectSettings() {
  const form = elements.settingsForm;
  return {
    devices: [...elements.devices.querySelectorAll(".device-row")].map((row) => ({
      screen_id: row.querySelector('[data-field="screen_id"]').value,
      name: row.querySelector('[data-field="name"]').value.trim(),
      offset: Number(row.querySelector('[data-field="offset"]').value || 0),
    })),
    skip_categories: [...form.querySelectorAll('input[name="category"]:checked')].map((input) => input.value),
    skip_count_tracking: form.elements.skip_count_tracking.checked,
    mute_ads: form.elements.mute_ads.checked,
    skip_ads: form.elements.skip_ads.checked,
    minimum_skip_length: Number(form.elements.minimum_skip_length.value),
    auto_play: form.elements.auto_play.checked,
    join_name: form.elements.join_name.value.trim(),
    apikey: form.elements.apikey.value,
    channel_whitelist: settings.channel_whitelist,
    use_proxy: form.elements.use_proxy.checked,
    debug: form.elements.debug.checked,
    http_tracing: form.elements.http_tracing.checked,
    sponsorblock_api_url: form.elements.sponsorblock_api_url.value.trim(),
  };
}

async function saveSettings() {
  const message = document.querySelector("#save-message");
  message.textContent = "Saving...";
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(collectSettings()) });
    settings = collectSettings();
    message.textContent = "Settings saved.";
    await refreshService();
    notify("Settings saved.");
  } catch (error) {
    message.textContent = error.message;
    notify(error.message, true);
  }
}

async function refreshService() {
  try {
    const status = await api("/api/service");
    elements.indicator.textContent = status.running ? "Service running" : "Service stopped";
    elements.indicator.classList.toggle("running", status.running);
    elements.indicator.classList.toggle("stopped", !status.running);
    document.querySelector("#start-button").disabled = status.running;
    document.querySelector("#stop-button").disabled = !status.running;
  } catch {
    elements.indicator.textContent = "Service unavailable";
  }
}

async function refreshLogs() {
  try {
    const result = await api("/api/logs?limit=1000");
    const nearBottom = elements.logOutput.scrollHeight - elements.logOutput.scrollTop - elements.logOutput.clientHeight < 80;
    elements.logOutput.textContent = result.lines.join("\n") || "No service logs yet.";
    document.querySelector("#log-count").textContent = `(${result.lines.length})`;
    if (nearBottom) elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
  } catch (error) {
    elements.logOutput.textContent = error.message;
  }
}

function renderDiscovered(devices) {
  if (!devices.length) {
    elements.discovered.innerHTML = '<div class="empty-state">No new devices found. Check that this container can access your local network.</div>';
    return;
  }
  elements.discovered.innerHTML = devices.map((device, index) => `
    <div class="discovered-row" data-index="${index}">
      <span class="grow">${escapeHtml(device.name || "YouTube device")} <small>${escapeHtml(device.screen_id)}</small></span>
      <button class="button button-secondary add-discovered" type="button">Add device</button>
    </div>`).join("");
  elements.discovered._devices = devices;
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const errorElement = document.querySelector("#login-error");
  errorElement.textContent = "";
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    event.currentTarget.reset();
    await showApp(result.csrf_token);
  } catch (error) {
    errorElement.textContent = error.message;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    showLogin();
  }
});

elements.settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveSettings();
});

elements.devices.addEventListener("input", (event) => {
  const field = event.target.dataset.field;
  if (!field) return;
  const row = event.target.closest(".device-row");
  const device = settings.devices[Number(row.dataset.index)];
  device[field] = field === "offset" ? Number(event.target.value || 0) : event.target.value;
});

elements.devices.addEventListener("click", (event) => {
  if (!event.target.matches(".remove-device")) return;
  settings.devices.splice(Number(event.target.closest(".device-row").dataset.index), 1);
  renderDevices();
});

elements.channels.addEventListener("click", (event) => {
  if (!event.target.matches(".remove-channel")) return;
  settings.channel_whitelist.splice(Number(event.target.closest(".channel-row").dataset.index), 1);
  renderChannels();
});

document.querySelector("#pair-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api("/api/devices/pair", {
      method: "POST",
      body: JSON.stringify({
        pairing_code: document.querySelector("#pairing-code").value,
        name: document.querySelector("#pairing-name").value,
      }),
    });
    settings.devices.push(result.device);
    document.querySelector("#pairing-code").value = "";
    document.querySelector("#pairing-name").value = "";
    renderDevices();
    await refreshService();
    notify("Device paired.");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#discover-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Discovering...";
  elements.discovered.innerHTML = '<div class="empty-state">Searching your local network...</div>';
  try {
    const result = await api("/api/devices/discover", { method: "POST" });
    renderDiscovered(result.devices);
    if (result.timed_out) notify("Discovery window ended; showing devices found so far.");
  } catch (error) {
    elements.discovered.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "Discover devices";
  }
});

elements.discovered.addEventListener("click", (event) => {
  if (!event.target.matches(".add-discovered")) return;
  const row = event.target.closest(".discovered-row");
  const device = elements.discovered._devices[Number(row.dataset.index)];
  settings.devices.push({ screen_id: device.screen_id, name: device.name || "", offset: device.offset || 0 });
  renderDevices();
  row.remove();
  if (!elements.discovered.children.length) renderDiscovered([]);
  notify("Device added. Save settings to apply it.");
});

document.querySelector("#channel-search-button").addEventListener("click", async (event) => {
  if (!elements.settingsForm.elements.apikey.value) {
    notify("Enter and save a YouTube API key before searching.", true);
    return;
  }
  const query = document.querySelector("#channel-query").value.trim();
  if (!query) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api("/api/channels/search", {
      method: "POST",
      body: JSON.stringify({ query }),
    });
    if (!result.channels.length) {
      elements.channelResults.innerHTML = '<div class="empty-state">No channels found.</div>';
    } else {
      elements.channelResults.innerHTML = result.channels.map((channel, index) => `
        <div class="channel-result" data-index="${index}">
          <span>${escapeHtml(channel.name)} <small>Subscribers: ${escapeHtml(channel.subscribers)}</small></span>
          <button class="button button-secondary add-channel" type="button">Whitelist</button>
        </div>`).join("");
      elements.channelResults._channels = result.channels;
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

elements.channelResults.addEventListener("click", (event) => {
  if (!event.target.matches(".add-channel")) return;
  const row = event.target.closest(".channel-result");
  const channel = elements.channelResults._channels[Number(row.dataset.index)];
  if (!settings.channel_whitelist.some((item) => item.id === channel.id)) {
    settings.channel_whitelist.push({ id: channel.id, name: channel.name });
    renderChannels();
  }
  row.remove();
});

document.querySelector("#add-custom-channel").addEventListener("click", () => {
  const id = document.querySelector("#custom-channel-id").value.trim();
  const name = document.querySelector("#custom-channel-name").value.trim();
  if (!id) {
    notify("Enter a channel ID.", true);
    return;
  }
  if (!settings.channel_whitelist.some((channel) => channel.id === id)) {
    settings.channel_whitelist.push({ id, name });
    renderChannels();
  }
  document.querySelector("#custom-channel-id").value = "";
  document.querySelector("#custom-channel-name").value = "";
});

document.querySelector("#start-button").addEventListener("click", async () => {
  try {
    await api("/api/service/start", { method: "POST" });
    await refreshService();
    notify("Service started.");
  } catch (error) {
    notify(error.message, true);
  }
});

document.querySelector("#stop-button").addEventListener("click", async () => {
  try {
    await api("/api/service/stop", { method: "POST" });
    await refreshService();
    notify("Service stopped.");
  } catch (error) {
    notify(error.message, true);
  }
});

document.querySelector("#refresh-logs").addEventListener("click", refreshLogs);
document.querySelector("#clear-logs").addEventListener("click", () => {
  elements.logOutput.textContent = "Log view cleared. Service output will reappear on refresh.";
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelector("#settings-tab").hidden = tab.dataset.tab !== "settings";
    document.querySelector("#logs-tab").hidden = tab.dataset.tab !== "logs";
    if (tab.dataset.tab === "logs") await refreshLogs();
  });
});

async function initialize() {
  try {
    const state = await api("/api/auth/status");
    if (state.authenticated) await showApp(state.csrf_token);
    else showLogin();
  } catch (error) {
    showLogin();
    document.querySelector("#login-error").textContent = error.message;
  }
}

initialize();
