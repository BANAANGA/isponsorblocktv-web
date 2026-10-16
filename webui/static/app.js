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
  historyList: document.querySelector("#history-list"),
  toast: document.querySelector("#toast"),
};

let csrfToken = "";
let settings = null;
let logPolling = null;
let toastTimeout = null;
let historyOffset = 0;
let historySearchTimeout = null;
let historyRequestId = 0;
const historyPageSize = 50;

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
  await refreshStats();
  await refreshNowPlaying();
  clearInterval(logPolling);
  logPolling = setInterval(() => {
    if (document.querySelector("#auto-refresh")?.checked && !document.querySelector("#logs-tab").hidden) {
      refreshLogs();
    }
    refreshService();
    refreshNowPlaying();
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
    max_history_videos: Number(form.elements.max_history_videos.value),
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
    elements.logOutput.innerHTML = result.lines.length
      ? result.lines.map((line) => {
        const timestampMatch = line.match(/^(\d{4}-\d{2}-\d{2}[T ][0-9:.+Z-]+)\s*/);
        const timestamp = timestampMatch?.[1] || "";
        const remainder = timestampMatch ? line.slice(timestampMatch[0].length) : line;
        const levelMatch = remainder.match(/^\s*(?:\[)?(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/i);
        const level = levelMatch?.[1]?.toUpperCase() || "LOG";
        const message = levelMatch
          ? remainder.replace(/^\s*\[?(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b\]?\s*[:| -]?\s*/i, "")
          : remainder;
        return `<div class="log-entry log-${level.toLowerCase()}"><time>${escapeHtml(timestamp)}</time><span class="log-level">${level}</span><span class="log-message">${escapeHtml(message)}</span></div>`;
      }).join("")
      : '<div class="log-empty">No service logs yet.</div>';
    document.querySelector("#log-count").textContent = `(${result.lines.length})`;
    if (nearBottom) elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
  } catch (error) {
    elements.logOutput.textContent = error.message;
  }
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

async function refreshHistory(reset = true) {
  const requestId = ++historyRequestId;
  const search = document.querySelector("#history-search").value.trim();
  if (reset) {
    historyOffset = 0;
    elements.historyList.innerHTML = "";
  }
  const query = new URLSearchParams({
    limit: historyPageSize,
    offset: historyOffset,
    search,
  });
  const result = await api(`/api/history?${query}`);
  if (requestId !== historyRequestId) return;
  const items = result.items.map((video) => `
    <article class="history-row">
      <a class="history-thumbnail" href="https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}" target="_blank" rel="noopener noreferrer" aria-label="Watch ${escapeHtml(video.title || video.video_id)}">
        <img src="${escapeHtml(video.thumbnail_url || `https://i.ytimg.com/vi/${encodeURIComponent(video.video_id)}/hqdefault.jpg`)}" alt="" loading="lazy">
        <span class="play-icon" aria-hidden="true">▶</span>
      </a>
      <div class="history-video">
        <a href="https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}" target="_blank" rel="noopener noreferrer">${escapeHtml(video.title || video.video_id)}</a>
        <small>${video.channel_url
          ? `<a class="history-channel" href="${escapeHtml(video.channel_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(video.channel || "Unknown channel")}</a>`
          : escapeHtml(video.channel || "Unknown channel")} · ${Number(video.segment_count) || 0} SponsorBlock segments found</small>
      </div>
      <span class="history-device">${escapeHtml(video.device)}</span>
      <time datetime="${escapeHtml(video.started_at)}">${escapeHtml(formatDate(video.started_at))}</time>
    </article>`).join("");
  if (reset && !result.items.length) {
    elements.historyList.innerHTML = `<div class="empty-state">${search
      ? "No video history matches your search."
      : "No videos have been recorded yet. History begins when the service detects playback on a paired device."}</div>`;
  } else {
    elements.historyList.insertAdjacentHTML("beforeend", items);
  }
  historyOffset += result.items.length;
  document.querySelector("#history-summary").textContent =
    `Showing ${historyOffset} of ${result.total} video sessions${search ? ` matching “${search}”` : ""}`;
  document.querySelector("#load-more-history").hidden = historyOffset >= result.total;
}

function renderMetric(label, value, detail = "") {
  return `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</article>`;
}

async function refreshNowPlaying() {
  const content = document.querySelector("#now-playing-content");
  try {
    const result = await api("/api/now-playing");
    content.innerHTML = result.items.length
      ? result.items.map((item) => `
        <article class="now-playing-item">
          <a class="now-playing-thumbnail" href="https://www.youtube.com/watch?v=${encodeURIComponent(item.video_id)}" target="_blank" rel="noopener noreferrer" aria-label="Watch ${escapeHtml(item.title)}">
            <img src="${escapeHtml(item.thumbnail_url || `https://i.ytimg.com/vi/${encodeURIComponent(item.video_id)}/hqdefault.jpg`)}" alt="" loading="lazy">
            <span class="play-icon" aria-hidden="true">▶</span>
          </a>
          <div class="now-playing-details">
            <span class="now-playing-indicator"><span></span> PLAYING</span>
            <a class="now-playing-title" href="https://www.youtube.com/watch?v=${encodeURIComponent(item.video_id)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title || item.video_id)}</a>
            <span class="now-playing-channel">${escapeHtml(item.channel || "Unknown channel")}</span>
            <span class="now-playing-device">${escapeHtml(item.device)}</span>
          </div>
        </article>`).join("")
      : '<div class="empty-state">Nothing is currently playing.</div>';
  } catch (error) {
    content.textContent = `Could not load current playback: ${error.message}`;
  }
}

async function refreshStats() {
  const stats = await api("/api/stats");
  const total = stats.totals;
  const recent = stats.last_30_days;
  document.querySelector("#total-stats").innerHTML = [
    renderMetric("Videos in history", total.videos_watched, "within configured retention limit"),
    renderMetric("Skip events", total.skip_events, `${total.segments_skipped} submitted segments`),
    renderMetric("Time saved", formatDuration(total.seconds_saved), `${Number(total.seconds_saved).toFixed(0)} seconds total`),
    renderMetric("Saved days", `${Number(total.time_saved_days).toFixed(2)} days`),
  ].join("");
  document.querySelector("#recent-stats").innerHTML = [
    renderMetric("Videos logged", recent.videos_watched),
    renderMetric("Skip events", recent.skip_events),
    renderMetric("Segments skipped", recent.segments_skipped),
    renderMetric("Time saved", formatDuration(recent.seconds_saved)),
  ].join("");

  const watchedSeconds = Math.max(0, Number(total.watched_seconds) || 0);
  const skippedSeconds = Math.max(0, Number(total.seconds_saved) || 0);
  const comparedSeconds = watchedSeconds + skippedSeconds;
  const watchedPercent = comparedSeconds ? watchedSeconds / comparedSeconds * 100 : 0;
  const skippedPercent = comparedSeconds ? skippedSeconds / comparedSeconds * 100 : 0;
  const timeChart = document.querySelector("#time-comparison-chart");
  const timeLegend = document.querySelector("#time-comparison-legend");
  timeChart.style.background = comparedSeconds
    ? `conic-gradient(#43c6db 0deg ${watchedPercent * 3.6}deg, #ff525b ${watchedPercent * 3.6}deg 360deg)`
    : "conic-gradient(#35434f 0deg 360deg)";
  timeChart.setAttribute(
    "aria-label",
    comparedSeconds
      ? `Pie chart: ${formatDuration(watchedSeconds)} watched, ${formatDuration(skippedSeconds)} skipped`
      : "No watched or skipped playback time has been recorded yet",
  );
  timeLegend.innerHTML = comparedSeconds
    ? `<div class="category-legend-row">
        <span class="category-swatch time-swatch-watched"></span>
        <span class="category-legend-name">Watched</span>
        <strong>${escapeHtml(formatDuration(watchedSeconds))}</strong>
        <small>${watchedPercent.toFixed(1)}%</small>
      </div>
      <div class="category-legend-row">
        <span class="category-swatch time-swatch-skipped"></span>
        <span class="category-legend-name">Skipped</span>
        <strong>${escapeHtml(formatDuration(skippedSeconds))}</strong>
        <small>${skippedPercent.toFixed(1)}%</small>
      </div>`
    : '<div class="empty-state">Playback and skip-time data will appear here as the service runs.</div>';

  const categoryNames = {
    sponsor: "Sponsor",
    selfpromo: "Self promotion",
    exclusive_access: "Exclusive access",
    interaction: "Interaction reminder",
    intro: "Intro",
    outro: "Outro",
    preview: "Preview / recap",
    filler: "Filler",
    music_offtopic: "Off-topic music",
    hook: "Hook / greeting",
    unknown: "Other",
  };
  const categories = document.querySelector("#category-stats");
  const chart = document.querySelector("#category-chart");
  const legend = document.querySelector("#category-legend");
  if (stats.by_category.length) {
    const palette = [
      "#ff525b", "#ff9f43", "#ffd166", "#9be564", "#42d6a4",
      "#43c6db", "#5b9dff", "#9b79ff", "#e879cf", "#ff7892",
    ];
    const totalSegments = stats.by_category.reduce((sum, item) => sum + (Number(item.segments_skipped) || 0), 0);
    let angle = 0;
    const slices = stats.by_category.map((item, index) => {
      const count = Number(item.segments_skipped) || 0;
      const start = angle;
      angle += totalSegments ? count / totalSegments * 360 : 0;
      return {
        ...item,
        color: palette[index % palette.length],
        percent: totalSegments ? count / totalSegments * 100 : 0,
        start,
        end: angle,
      };
    });
    const gradient = totalSegments
      ? slices.map((item) => `${item.color} ${item.start.toFixed(2)}deg ${item.end.toFixed(2)}deg`).join(", ")
      : "#35434f 0deg 360deg";
    chart.style.background = `conic-gradient(${gradient})`;
    chart.setAttribute(
      "aria-label",
      `Pie chart of ${totalSegments} skipped segments across ${slices.length} categories`,
    );
    legend.innerHTML = slices.map((item, index) => {
      const name = categoryNames[item.category] || item.category;
      return `<div class="category-legend-row">
        <span class="category-swatch category-swatch-${index % palette.length}"></span>
        <span class="category-legend-name">${escapeHtml(name)}</span>
        <strong>${Number(item.segments_skipped) || 0}</strong>
        <small>${item.percent.toFixed(1)}%</small>
      </div>`;
    }).join("");
  } else {
    chart.style.background = "conic-gradient(#35434f 0deg 360deg)";
    chart.setAttribute("aria-label", "No skipped segments have been recorded yet");
    legend.innerHTML = '<div class="empty-state">Category charts will appear after the first skip.</div>';
  }
  categories.innerHTML = stats.by_category.length
    ? stats.by_category.map((item) => `
      <tr><td>${escapeHtml(categoryNames[item.category] || item.category)}</td>
      <td>${Number(item.segments_skipped) || 0}</td>
      <td>${Number(item.skip_events) || 0}</td>
      <td>${escapeHtml(formatDuration(item.seconds_saved))}</td></tr>`).join("")
    : '<tr><td colspan="4" class="empty-state">No skips recorded yet.</td></tr>';

  const devices = document.querySelector("#device-stats");
  devices.innerHTML = stats.by_device.length
    ? stats.by_device.map((device) => `
      <article class="stats-list-row"><strong>${escapeHtml(device.device)}</strong>
      <span>${Number(device.skip_events) || 0} skips · ${Number(device.segments_skipped) || 0} segments</span>
      <small>${escapeHtml(formatDuration(device.seconds_saved))} saved</small></article>`).join("")
    : '<div class="empty-state">Device skip statistics will appear here.</div>';

  const daily = document.querySelector("#daily-stats");
  if (!stats.daily.length) {
    daily.innerHTML = '<div class="empty-state">Daily activity will appear after the first skip.</div>';
  } else {
    const maxSeconds = Math.max(1, ...stats.daily.flatMap((item) => [
      Number(item.watched_seconds) || 0,
      Number(item.seconds_saved) || 0,
    ]));
    daily.innerHTML = stats.daily.map((item) => {
      const watched = Number(item.watched_seconds) || 0;
      const skipped = Number(item.seconds_saved) || 0;
      const watchedWidth = watched ? Math.max(2, watched / maxSeconds * 100) : 0;
      const skippedWidth = skipped ? Math.max(2, skipped / maxSeconds * 100) : 0;
      return `<div class="daily-row daily-comparison-row" title="${escapeHtml(item.day)}: ${escapeHtml(formatDuration(watched))} watched, ${escapeHtml(formatDuration(skipped))} skipped">
        <time>${escapeHtml(item.day.slice(5))}</time>
        <span class="daily-comparison-bars">
          <span class="daily-bar-track"><progress class="daily-bar daily-watch-bar" value="${watchedWidth.toFixed(1)}" max="100" aria-label="${escapeHtml(formatDuration(watched))} watched"></progress></span>
          <span class="daily-bar-track"><progress class="daily-bar daily-skip-bar" value="${skippedWidth.toFixed(1)}" max="100" aria-label="${escapeHtml(formatDuration(skipped))} skipped"></progress></span>
        </span>
        <strong>${escapeHtml(formatDuration(watched))} / ${escapeHtml(formatDuration(skipped))}</strong></div>`;
    }).join("");
  }

  const topVideos = document.querySelector("#top-video-stats");
  if (!stats.top_videos.length) {
    topVideos.innerHTML = '<div class="empty-state">Watched-time rankings will appear as playback is recorded.</div>';
  } else {
    const maxWatched = Math.max(1, ...stats.top_videos.map((video) => Number(video.watched_seconds) || 0));
    topVideos.innerHTML = stats.top_videos.map((video) => {
      const seconds = Number(video.watched_seconds) || 0;
      const width = Math.max(2, seconds / maxWatched * 100);
      return `<div class="top-video-row">
        <a href="https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}" target="_blank" rel="noopener noreferrer">${escapeHtml(video.title || video.video_id)}</a>
        <small>${escapeHtml(video.channel || video.device)}</small>
        <span class="daily-bar-track"><progress class="daily-bar daily-watch-bar" value="${width.toFixed(1)}" max="100" aria-label="${escapeHtml(formatDuration(seconds))} watched"></progress></span>
        <strong>${escapeHtml(formatDuration(seconds))}</strong>
      </div>`;
    }).join("");
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
  const loginForm = event.currentTarget;
  const form = new FormData(loginForm);
  const errorElement = document.querySelector("#login-error");
  errorElement.textContent = "";
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    loginForm.reset();
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
document.querySelector("#refresh-stats").addEventListener("click", () => {
  refreshStats().catch((error) => notify(error.message, true));
});
document.querySelector("#refresh-history").addEventListener("click", () => {
  refreshHistory().catch((error) => notify(error.message, true));
});
document.querySelector("#history-search").addEventListener("input", () => {
  clearTimeout(historySearchTimeout);
  historySearchTimeout = setTimeout(() => {
    refreshHistory().catch((error) => notify(error.message, true));
  }, 250);
});
document.querySelector("#load-more-history").addEventListener("click", () => {
  refreshHistory(false).catch((error) => notify(error.message, true));
});
document.querySelector("#clear-logs").addEventListener("click", () => {
  elements.logOutput.textContent = "Log view cleared. Service output will reappear on refresh.";
});
document.querySelector("#delete-logs").addEventListener("click", async () => {
  if (!window.confirm("Permanently delete all saved service logs, including the rotated log file?")) return;
  try {
    await api("/api/logs", { method: "DELETE" });
    await refreshLogs();
    notify("Saved service logs deleted.");
  } catch (error) {
    notify(error.message, true);
  }
});
document.querySelector("#delete-history").addEventListener("click", async () => {
  if (!window.confirm("Permanently delete all video history and activity statistics? This cannot be undone.")) return;
  try {
    await api("/api/history", { method: "DELETE" });
    historyOffset = 0;
    elements.historyList.innerHTML = "";
    await Promise.all([refreshStats(), refreshNowPlaying()]);
    if (!document.querySelector("#history-tab").hidden) await refreshHistory();
    notify("Video history and activity statistics deleted.");
  } catch (error) {
    notify(error.message, true);
  }
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelectorAll(".tab-content").forEach((section) => {
      section.hidden = section.id !== `${tab.dataset.tab}-tab`;
    });
    try {
      if (tab.dataset.tab === "logs") await refreshLogs();
      if (tab.dataset.tab === "history") await refreshHistory();
      if (tab.dataset.tab === "dashboard") {
        await Promise.all([refreshStats(), refreshNowPlaying()]);
      }
    } catch (error) {
      notify(error.message, true);
    }
  });
});

async function initialize() {
  try {
    const state = await api("/api/auth/status");
    if (state.authenticated) {
      await showApp(state.csrf_token);
      document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === "dashboard"));
      document.querySelectorAll(".tab-content").forEach((section) => {
        section.hidden = section.id !== "dashboard-tab";
      });
    }
    else showLogin();
  } catch (error) {
    showLogin();
    document.querySelector("#login-error").textContent = error.message;
  }
}

initialize();
