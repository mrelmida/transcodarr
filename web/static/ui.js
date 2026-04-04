(function () {
  const API = (window.API_BASE || "/api").replace(/\/+$/, "");
  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // Elements
  const statusBadge = $("#status-badge");
  const btnStart    = $("#start-btn");
  const btnStop     = $("#stop-btn");
  const logOut      = $("#log-output");
  const logBox      = $(".log-container");

  // Boot values
  if (window.UI_BOOT) {
    $("#watch-folder").textContent  = window.UI_BOOT.watch  || "";
    $("#output-folder").textContent = window.UI_BOOT.output || "";
  }

  // ----- View & Tabs -----
  const settingsSubnav = $("#settings-subnav");
  const settingsNavItem = $(".nav-item[data-view='settings']");

  $$(".nav-item").forEach(b => b.addEventListener("click", () => {
    const view = b.dataset.view;

    // Handle settings toggle
    if (view === "settings") {
      // Toggle subnav visibility
      const isExpanded = settingsSubnav.classList.contains("expanded");
      if (isExpanded) {
        settingsSubnav.classList.remove("expanded");
        settingsNavItem.classList.remove("expanded");
      } else {
        settingsSubnav.classList.add("expanded");
        settingsNavItem.classList.add("expanded");
        // Show settings view
        $$(".nav-item").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        $$(".view").forEach(v => v.classList.toggle("visible", v.id === "view-settings"));
        // Select first section if none selected
        if (!currentSection && Object.keys(settingsSchema).length > 0) {
          showSettingsSection(Object.keys(settingsSchema)[0]);
        }
      }
      return;
    }

    // Normal nav handling for other views
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $$(".view").forEach(v => v.classList.toggle("visible", v.id === `view-${view}`));
    // Collapse settings subnav when switching to other views
    settingsSubnav.classList.remove("expanded");
    settingsNavItem.classList.remove("expanded");
  }));

  $$(".tab").forEach(t => t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    const tab = t.dataset.tab;
    $$(".tabpane").forEach(p => p.classList.toggle("visible", p.id === `tab-${tab}`));
  }));

  // ----- Status -----
  function setRunningUI(running) {
    if (running) {
      statusBadge.textContent = "Running";
      statusBadge.className = "badge badge-running";
      btnStart.disabled = true; btnStop.disabled = false;
    } else {
      statusBadge.textContent = "Stopped";
      statusBadge.className = "badge badge-stopped";
      btnStart.disabled = false; btnStop.disabled = true;
    }
  }

  async function updateStatus() {
    try {
      const r = await fetch(`${API}/status`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const d = r.ok ? await r.json() : {};
      const running = (d.status || d.running) === "running" || d.running === true;
      setRunningUI(running);
      if (d.watch_folder)  $("#watch-folder").textContent  = d.watch_folder;
      if (d.output_folder) $("#output-folder").textContent = d.output_folder;
    } catch {
      setRunningUI(false);
    }
  }

  // ----- Logs (byte-offset tail) -----
  let tailPos   = 0;
  let tailInode = null;

  function atBottom(){
    return logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 8;
  }
  function scrollToBottom(){ logBox.scrollTop = logBox.scrollHeight; }
  function appendText(txt){
    if (!txt) return;
    const stick = atBottom();
    logOut.textContent += txt.replace(/\r\n/g, "\n");
    // Hard cap
    const lines = logOut.textContent.split("\n");
    if (lines.length > 5000) logOut.textContent = lines.slice(-5000).join("\n");
    if (stick) scrollToBottom();
  }

  async function pollLogs(){
    try{
      const url = new URL(`${API}/logs/tail`, window.location.origin);
      url.searchParams.set("pos", String(tailPos));
      if (tailInode) url.searchParams.set("inode", tailInode);
      const r = await fetch(url, {headers:{Accept:"application/json"}, cache:"no-store"});
      if (!r.ok) return;
      const d = await r.json(); // {text,pos,inode,reset}
      if (d.reset || (tailInode && d.inode && d.inode !== tailInode)) {
        logOut.textContent = "";
      }
      if (typeof d.text === "string" && d.text.length) appendText(d.text);
      if (typeof d.pos === "number") tailPos = d.pos;
      if (d.inode) tailInode = d.inode;
    } catch {}
  }

  $("#clear-log").addEventListener("click", () => { logOut.textContent = ""; });

// Skeleton loaders
function moviesSkeleton(n = 6) {
  return Array(n).fill(`
    <tr class="skeleton-row">
      <td class="select-cell"></td>
      <td class="poster-cell"><div class="skeleton skeleton-poster"></div></td>
      <td><div class="skeleton skeleton-text"></div><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
    </tr>
  `).join("");
}

function tvSkeleton(n = 6) {
  return Array(n).fill(`
    <tr class="skeleton-row">
      <td class="select-cell"></td>
      <td class="poster-cell"><div class="skeleton skeleton-poster"></div></td>
      <td><div class="skeleton skeleton-text"></div><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
    </tr>
  `).join("");
}

// Worker pool status
let workerStatus = { active_manual_jobs: 0, active_auto_jobs: 0, manual_workers: 0, auto_workers: 2, can_accept: false };

async function updateWorkerStatus() {
  try {
    const r = await fetch(`${API}/workers/status`, {headers:{Accept:"application/json"}, cache:"no-store"});
    if (r.ok) {
      workerStatus = await r.json();
      const mw = workerStatus.manual_workers || 0;
      const aw = workerStatus.auto_workers || 0;
      const am = workerStatus.active_manual_jobs || 0;
      const aa = workerStatus.active_auto_jobs || 0;

      // Auto pill
      const autoCount = $("#auto-worker-count");
      const autoPill = $("#auto-workers");
      if (autoCount) autoCount.textContent = aw > 0 ? `${aa}/${aw}` : "off";
      if (autoPill) {
        autoPill.classList.toggle("busy", aw > 0 && aa >= aw);
        autoPill.classList.toggle("off", aw === 0);
      }
      const autoGroup = $("#auto-group");
      if (autoGroup) autoGroup.title = aw > 0 ? `Auto: ${aa} active / ${aw} workers` : "Auto: disabled";

      // Manual pill
      const manualCount = $("#manual-worker-count");
      const manualPill = $("#manual-workers");
      if (manualCount) manualCount.textContent = mw > 0 ? `${am}/${mw}` : "off";
      if (manualPill) {
        manualPill.classList.toggle("busy", mw > 0 && am >= mw);
        manualPill.classList.toggle("off", mw === 0);
      }

      // Manual badge
      const manualBadge = $("#manual-badge");
      if (manualBadge) {
        if (mw === 0) {
          manualBadge.textContent = "Off";
          manualBadge.className = "badge badge-off";
        } else if (am >= mw) {
          manualBadge.textContent = "Busy";
          manualBadge.className = "badge badge-busy";
        } else {
          manualBadge.textContent = "Ready";
          manualBadge.className = "badge badge-ready";
        }
      }
      const manualGroup = $("#manual-group");
      if (manualGroup) manualGroup.title = mw > 0 ? `Manual: ${am} active / ${mw} workers` : "Manual: disabled";
    }
  } catch {}
}

// Action buttons for items
function actionHtml(item, type, idx) {
  const isIgnored = item.ignored === true;
  const isProcessing = item.status === "processing" || item.status === "queued";

  // Subs button available for non-ignored, non-processing items
  const subsBtn = (isIgnored || isProcessing)
    ? ""
    : `<button class="btn btn-sm btn-subs" data-type="${type}" data-idx="${idx}" title="Manual subtitle search">&#128269;</button>`;

  // Show stop button for processing/queued items
  if (isProcessing) {
    return `<div class="action-btns"><button class="btn btn-sm btn-stop" data-type="${type}" data-idx="${idx}" title="Stop transcode">Stop</button></div>`;
  }

  // Show stop button for re-encoding items
  if (item.status === "re-encoding") {
    return `<div class="action-btns"><button class="btn btn-sm btn-stop" data-type="${type}" data-idx="${idx}" title="Stop re-encode">Stop</button></div>`;
  }

  // Ready items get a transcode button + get meta
  if (item.status === "ready") {
    const transcodeBtn = `<button class="btn btn-sm btn-transcode" data-type="${type}" data-idx="${idx}" title="Transcode with current settings">Transcode</button>`;
    const enrichBtn = `<button class="btn btn-sm btn-enrich" data-type="${type}" data-idx="${idx}" title="Fetch metadata, NFO, poster">Meta</button>`;
    return `<div class="action-btns">${enrichBtn}${transcodeBtn}</div>`;
  }

  // Only show transcode/ignore for pending items
  if (item.status !== "pending") {
    return subsBtn ? `<div class="action-btns">${subsBtn}</div>` : "";
  }

  const ignoreClass = isIgnored ? "btn-ignore active" : "btn-ignore";
  const ignoreTitle = isIgnored ? "Remove from ignore list" : "Add to ignore list (skip auto-transcode)";

  // Don't show transcode button if ignored
  const transcodeBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-transcode" data-type="${type}" data-idx="${idx}" title="Queue manual transcode">Transcode</button>`;

  // Delete subs button for pending non-ignored items
  const deleteSubsBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-delete-subs" data-type="${type}" data-idx="${idx}" title="Delete existing subtitles">&#128465;</button>`;

  // Get metadata button
  const enrichBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-enrich" data-type="${type}" data-idx="${idx}" title="Fetch metadata, NFO, poster">Meta</button>`;

  return `
    <div class="action-btns">
      ${subsBtn}
      ${deleteSubsBtn}
      ${enrichBtn}
      ${transcodeBtn}
      <button class="${ignoreClass}" data-type="${type}" data-idx="${idx}" title="${ignoreTitle}">
        ${isIgnored ? "&#10003;" : "&#8709;"}
      </button>
    </div>
  `;
}

async function handleEnrichClick(item, type) {
  const btn = event ? event.target : null;
  const origText = btn ? btn.textContent : "";
  if (btn) { btn.textContent = "..."; btn.disabled = true; }

  try {
    const r = await fetch(`${API}/media/enrich`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ path: item.path })
    });
    const result = await r.json();
    if (r.ok) {
      const parts = [];
      if (result.nfo_written) parts.push("NFO");
      if (result.poster_downloaded) parts.push("Poster");
      const msg = parts.length > 0 ? parts.join(" + ") + " saved" : "No new metadata found";
      showSubsToast({ _custom: msg });
    } else {
      showSubsToast({ error: result.error || "Enrichment failed" });
    }
  } catch (e) {
    showSubsToast({ error: e.message });
  } finally {
    if (btn) { btn.textContent = origText; btn.disabled = false; }
  }
}

async function handleTranscodeClick(item, type) {
  if ((workerStatus.manual_workers || 0) <= 0) {
    alert("Manual transcoding is disabled (MANUAL_WORKERS=0). Enable it in Settings > Advanced.");
    return;
  }
  if (!workerStatus.can_accept) {
    alert("All manual workers are busy. Please wait.");
    return;
  }

  const data = {
    file_path: item.path,
    media_type: type,
  };

  if (type === "movie") {
    data.title = item.title;
    data.year = item.year;
  } else {
    data.show = item.show;
    data.season = item.season;
    data.episode = item.episode;
    data.title = item.title;
  }

  try {
    const r = await fetch(`${API}/transcode/manual`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data)
    });
    const result = await r.json();

    if (r.ok) {
      // Refresh tables to show processing status
      if (type === "movie") loadMovies(false);
      else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to queue: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to queue: ${e.message}`);
  }
}

async function handleStopClick(item, type) {
  try {
    const r = await fetch(`${API}/transcode/stop`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ file_path: item.source_path || item.path })
    });
    const result = await r.json();
    if (r.ok) {
      if (type === "movie") loadMovies(false);
      else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to stop: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to stop: ${e.message}`);
  }
}

async function handleIgnoreClick(item, type) {
  try {
    const r = await fetch(`${API}/media/ignore`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        file_path: item.path,
        action: "toggle"
      })
    });
    const result = await r.json();

    if (r.ok) {
      // Refresh tables to show updated ignore status
      if (type === "movie") loadMovies(false);
      else loadTV(false);
    } else {
      alert(`Failed to toggle ignore: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to toggle ignore: ${e.message}`);
  }
}

// ----- Manual Subtitle Search -----
function showSubtitleSearchModal(item, type) {
  // Pre-fill values based on item type
  let defaultQuery = "";
  let defaultSeason = "";
  let defaultEpisodes = "";

  if (type === "movie") {
    defaultQuery = item.title || "";
    if (item.year) defaultQuery += ` ${item.year}`;
  } else {
    defaultQuery = item.show || "";
    defaultSeason = item.season != null ? String(item.season) : "";
    if (item.episodes && item.episodes.length > 0) {
      defaultEpisodes = item.episodes.join(", ");
    } else if (item.episode != null) {
      defaultEpisodes = String(item.episode);
    }
  }

  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-content subs-search-modal">
      <h3>Manual Subtitle Search</h3>
      <p class="modal-hint">Override search parameters for: <strong>${type === "movie" ? (item.title || "Unknown") : (item.show || "Unknown")}</strong></p>
      <div class="modal-field">
        <label for="subs-query">Search Query</label>
        <input type="text" id="subs-query" value="${escapeHtml(defaultQuery)}" placeholder="Movie or show title" autocomplete="off">
      </div>
      <div class="modal-field-row">
        <div class="modal-field">
          <label for="subs-season">Season <span class="field-optional">(optional)</span></label>
          <input type="number" id="subs-season" value="${escapeHtml(defaultSeason)}" placeholder="e.g. 4" min="1" autocomplete="off">
        </div>
        <div class="modal-field">
          <label for="subs-episodes">Episodes <span class="field-optional">(optional)</span></label>
          <input type="text" id="subs-episodes" value="${escapeHtml(defaultEpisodes)}" placeholder="e.g. 28, 29, 30" autocomplete="off">
        </div>
      </div>
      <div class="modal-field">
        <label>Max Results</label>
        <div class="toggle-group" id="subs-max-results">
          <button type="button" class="toggle-btn" data-value="1">1</button>
          <button type="button" class="toggle-btn active" data-value="3">3</button>
          <button type="button" class="toggle-btn" data-value="5">5</button>
          <button type="button" class="toggle-btn" data-value="8">8</button>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn btn-ghost modal-cancel">Cancel</button>
        <button class="btn btn-primary modal-confirm">Search</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);

  const queryInput = modal.querySelector("#subs-query");
  const seasonInput = modal.querySelector("#subs-season");
  const episodesInput = modal.querySelector("#subs-episodes");
  const confirmBtn = modal.querySelector(".modal-confirm");
  const toggleGroup = modal.querySelector("#subs-max-results");
  let maxResults = 3;

  // Toggle group handlers
  toggleGroup.querySelectorAll(".toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      toggleGroup.querySelectorAll(".toggle-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      maxResults = parseInt(btn.dataset.value, 10);
    });
  });

  modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());

  confirmBtn.addEventListener("click", () => {
    const query = queryInput.value.trim();
    if (!query) {
      alert("Search query is required");
      return;
    }

    // Close modal immediately
    modal.remove();

    // Show searching toast and run search
    showSubsToast({ searching: true });
    submitSubtitleSearch(item, query, seasonInput.value, episodesInput.value, maxResults);
  });

  // Close on overlay click
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.remove();
  });

  // Close on Escape
  const handleEscape = (e) => {
    if (e.key === "Escape") {
      modal.remove();
      document.removeEventListener("keydown", handleEscape);
    }
  };
  document.addEventListener("keydown", handleEscape);

  queryInput.focus();
  queryInput.select();
}

function submitSubtitleSearch(item, query, seasonStr, episodesStr, maxResults) {
  const data = {
    file_path: item.path,
    search_query: query,
    max_downloads: maxResults || 3
  };

  // Parse season (optional)
  const season = parseInt(seasonStr, 10);
  if (!isNaN(season) && season > 0) {
    data.season = season;
  }

  // Parse episodes (optional, comma-separated)
  if (episodesStr && episodesStr.trim()) {
    const episodes = episodesStr.split(/[,\s]+/)
      .map(s => parseInt(s.trim(), 10))
      .filter(n => !isNaN(n) && n > 0);
    if (episodes.length > 0) {
      data.episodes = episodes;
    }
  }

  fetch(`${API}/subtitles/search`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  })
    .then(r => r.json().then(result => ({ ok: r.ok, result })))
    .then(({ ok, result }) => {
      if (!ok) {
        showSubsToast({ error: result.error || "Unknown error" });
      } else {
        showSubsToast(result);
      }
    })
    .catch(e => {
      showSubsToast({ error: e.message });
    });
}

function showSubsToast(result) {
  // Remove existing toast if any
  const existing = document.querySelector(".subs-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");

  if (result.searching) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#128269;</span> Searching...`;
    document.body.appendChild(toast);
    return; // Don't auto-remove searching toast
  }

  if (result.deleting) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#128465;</span> Deleting...`;
    document.body.appendChild(toast);
    return; // Don't auto-remove deleting toast
  }

  if (result.error) {
    toast.className = "subs-toast error";
    toast.innerHTML = `<span class="toast-icon">&#10007;</span> ${result.error}`;
  } else if (result.deleted !== undefined) {
    // Delete result
    if (result.count > 0) {
      toast.className = "subs-toast success";
      toast.innerHTML = `<span class="toast-icon">&#10003;</span> Deleted ${result.count} subtitle(s)`;
    } else {
      toast.className = "subs-toast warning";
      toast.innerHTML = `<span class="toast-icon">&#8709;</span> No subtitles found`;
    }
  } else if (result.saved && result.saved.length > 0) {
    toast.className = "subs-toast success";
    toast.innerHTML = `<span class="toast-icon">&#10003;</span> Saved ${result.saved.length} subtitle(s)`;
  } else if (result.found > 0) {
    toast.className = "subs-toast warning";
    toast.innerHTML = `<span class="toast-icon">!</span> Found ${result.found} but none matched`;
  } else {
    toast.className = "subs-toast warning";
    toast.innerHTML = `<span class="toast-icon">&#10007;</span> No subtitles found`;
  }

  // Override with custom message if provided
  if (result._custom) {
    toast.className = "subs-toast success";
    toast.innerHTML = `<span class="toast-icon">&#10003;</span> ${result._custom}`;
  }

  // Custom searching message
  if (result._searching && result._custom_searching) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#9881;</span> ${result._custom_searching}`;
    document.body.appendChild(toast);
    return;
  }

  document.body.appendChild(toast);

  // Auto-remove after 4 seconds
  setTimeout(() => {
    toast.classList.add("fade-out");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

async function handleDeleteSubsClick(item, type) {
  if (!confirm("Delete all subtitle files for this media?")) return;

  showSubsToast({ deleting: true });

  try {
    const r = await fetch(`${API}/subtitles`, {
      method: "DELETE",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ file_path: item.path })
    });
    const result = await r.json();

    if (!r.ok) {
      showSubsToast({ error: result.error || "Failed to delete" });
    } else {
      showSubsToast(result);
    }
  } catch (e) {
    showSubsToast({ error: e.message });
  }
}

// Modal handling
let currentMediaItems = { movies: [], tv: [] };

// Sort, filter, selection state
let movieSort  = { col: "mtime", dir: "desc" };
let tvSort     = { col: "mtime", dir: "desc" };
let movieFilter = { text: "", status: "all" };
let tvFilter    = { text: "", status: "all" };
let displayedMovies = [];
let displayedTV     = [];
let movieSelection = new Set();
let tvSelection    = new Set();

function showMediaModal(item, type) {
  const modal = $("#media-modal");
  const posterImg = $("#modal-poster-img");

  // Set poster
  if (item.poster) {
    posterImg.src = item.poster;
    posterImg.style.display = "block";
  } else {
    posterImg.style.display = "none";
  }

  // Set title
  if (type === "movie") {
    $("#modal-title").textContent = item.title || "Unknown";
    $("#modal-subtitle").textContent = item.year ? `(${item.year})` : "";
  } else {
    let ep = "";
    if (item.season != null && item.episode != null) {
      const s = String(item.season).padStart(2, "0");
      if (item.episodes && item.episodes.length > 1) {
        const first = String(item.episodes[0]).padStart(2, "0");
        const last = String(item.episodes[item.episodes.length - 1]).padStart(2, "0");
        ep = `S${s}E${first}-E${last}`;
      } else {
        ep = `S${s}E${String(item.episode).padStart(2, "0")}`;
      }
    }
    // Strip any leading episode codes from title (e.g., "E11 - Title" or "S03E10 - Title")
    let cleanTitle = (item.title || "").replace(/^[sS]?\d{0,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*/g, "").trim();
    $("#modal-title").textContent = item.show || "Unknown";
    $("#modal-subtitle").textContent = ep ? `${ep} - ${cleanTitle}` : cleanTitle;
  }

  // Reset and hide description section initially
  const descGroup = $("#modal-description-group");
  descGroup.style.display = "none";
  $("#modal-description").textContent = "";
  $("#modal-genres").textContent = "";
  $("#modal-rating").textContent = "";

  // Fetch metadata (description, genres, rating) asynchronously
  fetchMediaMetadata(item, type);

  // File info
  $("#modal-path").textContent = item.path || "-";
  $("#modal-path").title = item.path || "";
  $("#modal-size").textContent = item.size_gb != null ? `${item.size_gb.toFixed(2)} GB` : "-";
  $("#modal-container").textContent = item.container ? item.container.toUpperCase() : "-";
  $("#modal-mtime").textContent = item.mtime_fmt || "-";

  // Video info
  $("#modal-vcodec").textContent = item.vcodec || "-";
  $("#modal-resolution").textContent = fmtRes(item.resolution);
  $("#modal-fps").textContent = item.frame_rate ? `${item.frame_rate} fps` : "-";
  $("#modal-vbitrate").textContent = item.video_bitrate_fmt || item.total_bitrate_fmt || "-";

  // Audio info
  $("#modal-acodec").textContent = item.acodec || "-";
  $("#modal-channels").textContent = item.audio_channels_fmt || (item.audio_channels ? `${item.audio_channels} channels` : "-");
  $("#modal-abitrate").textContent = item.audio_bitrate_fmt || "-";

  // Transcode info (only show if we have data)
  const transcodeGroup = $("#modal-transcode-group");
  if (item.processed_at || item.processing_duration) {
    transcodeGroup.style.display = "block";
    $("#modal-processed").textContent = item.processed_at_fmt || "-";
    $("#modal-duration").textContent = item.processing_duration_fmt || "-";
    $("#modal-source-size").textContent = item.source_size_gb != null ? `${item.source_size_gb.toFixed(2)} GB` : "-";
    $("#modal-compression").textContent = item.compression_ratio ? `${item.compression_ratio}x` : "-";
  } else {
    transcodeGroup.style.display = "none";
  }

  modal.classList.remove("hidden");
}

async function fetchMediaMetadata(item, type) {
  try {
    let url;
    if (type === "movie") {
      // Try to fetch by title and year
      const params = new URLSearchParams();
      if (item.title) params.append("title", item.title);
      if (item.year) params.append("year", item.year);
      url = `${API_BASE}/media/metadata/movie?${params}`;
    } else {
      // TV - fetch series metadata by show name
      const params = new URLSearchParams();
      if (item.show) params.append("title", item.show);
      url = `${API_BASE}/media/metadata/series?${params}`;
    }

    const resp = await fetch(url);
    if (!resp.ok) return;

    const metadata = await resp.json();
    if (metadata && metadata.description) {
      const descGroup = $("#modal-description-group");
      $("#modal-description").textContent = metadata.description;
      descGroup.style.display = "block";

      // Show genres if available
      if (metadata.genres) {
        $("#modal-genres").textContent = metadata.genres;
      }

      // Show rating if available
      if (metadata.rating) {
        $("#modal-rating").textContent = `Rating: ${metadata.rating}`;
      }
    }
  } catch (e) {
    console.debug("Failed to fetch metadata:", e);
  }
}

function hideMediaModal() {
  $("#media-modal").classList.add("hidden");
}

// Modal event listeners
document.addEventListener("DOMContentLoaded", () => {
  const modal = $("#media-modal");
  if (modal) {
    modal.querySelector(".modal-backdrop").addEventListener("click", hideMediaModal);
    modal.querySelector(".modal-close").addEventListener("click", hideMediaModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) {
        hideMediaModal();
      }
    });
  }
});

function posterHtml(url) {
  if (url) {
    return `<img class="poster-thumb" src="${url}" loading="lazy" onerror="this.outerHTML='<div class=\\'poster-placeholder\\'>🎬</div>'">`;
  }
  return `<div class="poster-placeholder">🎬</div>`;
}

function statusHtml(status, progress, ignored, reencodeProgress) {
  if (status === "processing") {
    const pct = progress != null ? Math.round(progress) : 0;
    return `<span class="status-badge status-processing">${pct}%</span>`;
  }
  if (status === "queued") {
    return `<span class="status-badge status-queued">Queued</span>`;
  }
  if (status === "re-encoding") {
    const pct = reencodeProgress != null ? Math.round(reencodeProgress) : 0;
    return `<span class="status-badge status-processing">${pct}%</span>`;
  }
  if (status === "pending") {
    if (ignored) {
      return `<span class="status-badge status-ignored">Ignored</span>`;
    }
    return `<span class="status-badge status-pending">Pending</span>`;
  }
  return `<span class="status-badge status-ready">Ready</span>`;
}

// Track scanning state for auto-refresh
let moviesScanning = false;
let tvScanning = false;
let hasProcessingItems = false;

// Track totals for stats display
let movieStats = { count: 0, sizeGb: 0 };
let tvStats = { count: 0, sizeGb: 0 };

function updateMediaStats() {
  const totalCount = movieStats.count + tvStats.count;
  const totalSizeGb = movieStats.sizeGb + tvStats.sizeGb;

  let sizeStr;
  if (totalSizeGb >= 1000) {
    sizeStr = `${(totalSizeGb / 1000).toFixed(2)} TB`;
  } else {
    sizeStr = `${totalSizeGb.toFixed(1)} GB`;
  }

  const statsEl = $("#stats-total");
  if (statsEl) {
    statsEl.innerHTML = `<span class="stat-value">${totalCount}</span> items · <span class="stat-value">${sizeStr}</span> total`;
  }
}

// ----- Filter & Sort Pipeline -----
function _statusKey(item) {
  if (item.status === "processing" || item.status === "queued" || item.status === "re-encoding") return "processing";
  if (item.status === "pending" && item.ignored) return "ignored";
  if (item.status === "pending") return "pending";
  return "ready";
}

function applyFilter(items, filter) {
  let out = items;
  if (filter.text) {
    const q = filter.text.toLowerCase();
    out = out.filter(d => {
      const blob = (d.title || "") + " " + (d.show || "") + " " + (d.year || "");
      return blob.toLowerCase().includes(q);
    });
  }
  if (filter.status !== "all") {
    out = out.filter(d => _statusKey(d) === filter.status);
  }
  return out;
}

function _resolutionRank(res) {
  if (!res) return 0;
  const m = res.match(/\d+x(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}

function _statusRank(status, ignored) {
  if (status === "processing") return 5;
  if (status === "re-encoding") return 4.5;
  if (status === "queued") return 4;
  if (status === "pending" && !ignored) return 3;
  if (status === "pending" && ignored) return 1;
  return 0; // ready
}

function applySort(items, sort) {
  const dir = sort.dir === "asc" ? 1 : -1;
  return [...items].sort((a, b) => {
    let cmp = 0;
    switch (sort.col) {
      case "title":
      case "show":
        cmp = (a[sort.col] || "").localeCompare(b[sort.col] || "");
        break;
      case "year":
      case "size_gb":
      case "mtime":
        cmp = (a[sort.col] || 0) - (b[sort.col] || 0);
        break;
      case "episode": {
        const aVal = (a.season || 0) * 10000 + (a.episode || 0);
        const bVal = (b.season || 0) * 10000 + (b.episode || 0);
        cmp = aVal - bVal;
        break;
      }
      case "resolution":
        cmp = _resolutionRank(a.resolution) - _resolutionRank(b.resolution);
        break;
      case "status":
        cmp = _statusRank(a.status, a.ignored) - _statusRank(b.status, b.ignored);
        break;
    }
    return cmp * dir;
  });
}

function getDisplayedMovies() {
  const filtered = applyFilter(currentMediaItems.movies, movieFilter);
  displayedMovies = applySort(filtered, movieSort);
  return displayedMovies;
}

function getDisplayedTV() {
  const filtered = applyFilter(currentMediaItems.tv, tvFilter);
  displayedTV = applySort(filtered, tvSort);
  return displayedTV;
}

// ----- Thead Builders -----
function _sortArrow(sort, col) {
  if (sort.col !== col) return "";
  return `<span class="sort-arrow">${sort.dir === "asc" ? "▲" : "▼"}</span>`;
}

function movieTheadHtml() {
  const s = movieSort;
  const cols = [
    { key: null, label: '<input type="checkbox" class="select-all" data-type="movie">', cls: "select-cell", sortable: false },
    { key: null, label: "", cls: "poster-cell", sortable: false },
    { key: "title", label: "Title" },
    { key: "year", label: "Year" },
    { key: "resolution", label: "Resolution" },
    { key: "size_gb", label: "Size" },
    { key: "mtime", label: "Changed" },
    { key: "status", label: "Status" },
    { key: null, label: "Action", sortable: false },
  ];
  return "<tr>" + cols.map(c => {
    if (c.sortable === false) return `<th class="${c.cls || ""}">${c.label}</th>`;
    const active = s.col === c.key ? " sort-active" : "";
    return `<th class="sortable${active}" data-sort="${c.key}">${c.label}${_sortArrow(s, c.key)}</th>`;
  }).join("") + "</tr>";
}

function tvTheadHtml() {
  const s = tvSort;
  const cols = [
    { key: null, label: '<input type="checkbox" class="select-all" data-type="tv">', cls: "select-cell", sortable: false },
    { key: null, label: "", cls: "poster-cell", sortable: false },
    { key: "show", label: "Series" },
    { key: "episode", label: "Episode" },
    { key: "resolution", label: "Resolution" },
    { key: "size_gb", label: "Size" },
    { key: "mtime", label: "Changed" },
    { key: "status", label: "Status" },
    { key: null, label: "Action", sortable: false },
  ];
  return "<tr>" + cols.map(c => {
    if (c.sortable === false) return `<th class="${c.cls || ""}">${c.label}</th>`;
    const active = s.col === c.key ? " sort-active" : "";
    return `<th class="sortable${active}" data-sort="${c.key}">${c.label}${_sortArrow(s, c.key)}</th>`;
  }).join("") + "</tr>";
}

function renderMoviesTable(items) {
  const body = $("#movies-body");
  const thead = $("#movies-thead");
  currentMediaItems.movies = items;

  // Stats on raw (unfiltered) items
  const readyItems = items.filter(m => m.status === "ready");
  movieStats.count = readyItems.length;
  movieStats.sizeGb = readyItems.reduce((sum, m) => sum + (m.size_gb || 0), 0);
  updateMediaStats();

  // Build displayed array through filter → sort pipeline
  const displayed = getDisplayedMovies();

  // Dynamic thead
  thead.innerHTML = movieTheadHtml();

  if (displayed.length === 0) {
    body.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">${items.length === 0 ? "No movies found" : "No matches"}</td></tr>`;
    updateBulkActionBar("movie");
    updateSelectAllState("movie");
    return;
  }

  hasProcessingItems = items.some(m => m.status === "processing" || m.status === "queued" || m.status === "pending" || m.status === "re-encoding") || hasProcessingItems;
  body.innerHTML = displayed.map((m, idx) => {
    const runtime = m.runtime_min ? `${m.runtime_min} min` : "";
    const codec = m.vcodec || "";
    const meta = [runtime, codec].filter(Boolean).join(" · ");
    const rowClass = m.status === "processing" ? "processing-row" : m.status === "queued" ? "queued-row" : m.status === "pending" ? "pending-row" : "";
    const ignoredClass = m.ignored ? " ignored-row" : "";
    const changed = m.status === "processing" ? (m.elapsed_fmt || "...") : (m.mtime_fmt || "-");
    const checked = movieSelection.has(m.path) ? "checked" : "";
    return `
    <tr class="${rowClass}${ignoredClass}">
      <td class="select-cell"><input type="checkbox" class="row-select" data-type="movie" data-idx="${idx}" ${checked}></td>
      <td class="poster-cell">${posterHtml(m.poster)}</td>
      <td>
        <div class="title-cell">
          <span class="title-main title-clickable" data-type="movie" data-idx="${idx}">${m.title || "-"}</span>
          ${meta ? `<span class="title-meta">${meta}</span>` : ""}
        </div>
      </td>
      <td>${m.year ?? "-"}</td>
      <td>${fmtRes(m.resolution)}</td>
      <td>${m.size_gb != null ? `${m.size_gb.toFixed(2)} GB` : "-"}</td>
      <td class="changed-cell">${changed}</td>
      <td>${statusHtml(m.status, m.progress, m.ignored, m.reencode_progress)}</td>
      <td class="action-cell">${actionHtml(m, "movie", idx)}</td>
    </tr>
  `}).join("");

  // Wire sort headers
  thead.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (movieSort.col === col) {
        movieSort.dir = movieSort.dir === "asc" ? "desc" : "asc";
      } else {
        movieSort.col = col;
        movieSort.dir = "asc";
      }
      renderMoviesTable(currentMediaItems.movies);
    });
  });

  // Wire select-all
  const selectAll = thead.querySelector(".select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      if (selectAll.checked) {
        displayed.forEach(d => movieSelection.add(d.path));
      } else {
        displayed.forEach(d => movieSelection.delete(d.path));
      }
      renderMoviesTable(currentMediaItems.movies);
    });
  }

  // Wire row checkboxes
  body.querySelectorAll(".row-select").forEach(cb => {
    cb.addEventListener("change", () => {
      const idx = parseInt(cb.dataset.idx, 10);
      const item = displayedMovies[idx];
      if (!item) return;
      if (cb.checked) movieSelection.add(item.path);
      else movieSelection.delete(item.path);
      updateBulkActionBar("movie");
      updateSelectAllState("movie");
    });
  });

  // Click handlers use displayedMovies
  body.querySelectorAll(".title-clickable").forEach(el => {
    el.addEventListener("click", () => {
      const idx = parseInt(el.dataset.idx, 10);
      const item = displayedMovies[idx];
      if (item) showMediaModal(item, "movie");
    });
  });

  body.querySelectorAll(".btn-stop").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) handleStopClick(item, "movie");
    });
  });

  body.querySelectorAll(".btn-transcode").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) handleTranscodeClick(item, "movie");
    });
  });

  body.querySelectorAll(".btn-ignore").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) handleIgnoreClick(item, "movie");
    });
  });

  body.querySelectorAll(".btn-subs").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) showSubtitleSearchModal(item, "movie");
    });
  });

  body.querySelectorAll(".btn-delete-subs").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) handleDeleteSubsClick(item, "movie");
    });
  });

  body.querySelectorAll(".btn-enrich").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedMovies[parseInt(el.dataset.idx, 10)];
      if (item) handleEnrichClick(item, "movie");
    });
  });

  updateBulkActionBar("movie");
  updateSelectAllState("movie");
}

// ----- Bulk Actions -----
function updateBulkActionBar(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const bar = $(`#${type}-bulk-actions`);
  const countEl = $(`#${type}-bulk-count`);
  if (!bar) return;

  // Only count selected items that are currently displayed
  const selectedDisplayed = displayed.filter(d => sel.has(d.path));
  const count = selectedDisplayed.length;

  if (count === 0) {
    bar.classList.add("hidden");
    return;
  }

  bar.classList.remove("hidden");
  countEl.textContent = `${count} selected`;

  // Enable/disable buttons based on selected statuses
  const hasEncodable = selectedDisplayed.some(d => (d.status === "pending" || d.status === "ready") && !d.ignored);
  const hasIgnorable = selectedDisplayed.some(d => d.status === "pending" && !d.ignored);
  const hasReady = selectedDisplayed.some(d => d.status === "ready");

  const transcodeBtn = bar.querySelector(".bulk-transcode");
  const ignoreBtn = bar.querySelector(".bulk-ignore");
  const deleteBtn = bar.querySelector(".bulk-delete");
  if (transcodeBtn) transcodeBtn.disabled = !hasEncodable;
  if (ignoreBtn) ignoreBtn.disabled = !hasIgnorable;
  if (deleteBtn) deleteBtn.disabled = !hasReady;
}

function updateSelectAllState(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const thead = type === "movie" ? $("#movies-thead") : $("#tv-thead");
  if (!thead) return;
  const cb = thead.querySelector(".select-all");
  if (!cb || displayed.length === 0) {
    if (cb) { cb.checked = false; cb.indeterminate = false; }
    return;
  }
  const selectedCount = displayed.filter(d => sel.has(d.path)).length;
  cb.checked = selectedCount === displayed.length;
  cb.indeterminate = selectedCount > 0 && selectedCount < displayed.length;
}

async function handleBulkTranscode(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && (d.status === "pending" || d.status === "ready") && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Queue ${eligible.length} item(s) for transcoding? They will be processed sequentially by a single worker.`)) return;

  const items = eligible.map(item => {
    const entry = { file_path: item.path, media_type: type };
    if (type === "movie") { entry.title = item.title; entry.year = item.year; }
    else { entry.show = item.show; entry.season = item.season; entry.episode = item.episode; entry.title = item.title; }
    return entry;
  });

  try {
    const r = await fetch(`${API}/transcode/batch`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ items })
    });
    const result = await r.json();
    if (r.ok) {
      if (type === "movie") loadMovies(false); else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to queue batch: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function handleBulkIgnore(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && d.status === "pending" && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Ignore ${eligible.length} item(s)? They will be skipped by auto-transcode.`)) return;

  try {
    await Promise.all(eligible.map(item =>
      fetch(`${API}/media/ignore`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ file_path: item.path, action: "add" })
      })
    ));
    if (type === "movie") loadMovies(false); else loadTV(false);
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function handleBulkDelete(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && d.status === "ready");

  if (eligible.length === 0) { alert("No eligible ready items selected."); return; }
  showDeleteConfirmModal(eligible, type);
}

function showDeleteConfirmModal(items, type) {
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-content delete-confirm-modal">
      <h3>Delete Output Files</h3>
      <p class="modal-hint">This will delete <strong>${items.length}</strong> output file(s) and their companion files (.nfo, .srt, etc). Source files are not affected.</p>
      <ul class="delete-list">
        ${items.map(d => `<li>${escapeHtml(d.title || d.show || "Unknown")}<span class="delete-path">${escapeHtml(d.path)}</span></li>`).join("")}
      </ul>
      <div class="modal-actions">
        <button class="btn btn-ghost modal-cancel">Cancel</button>
        <button class="btn btn-danger modal-confirm">Delete ${items.length} file(s)</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);

  modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector(".modal-confirm").addEventListener("click", async () => {
    const confirmBtn = modal.querySelector(".modal-confirm");
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Deleting...";

    try {
      const r = await fetch(API + "/media/output", {
        method: "DELETE",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ paths: items.map(d => d.path) })
      });
      const result = await r.json();
      if (r.ok) {
        // Clear selection for deleted items
        const sel = type === "movie" ? movieSelection : tvSelection;
        items.forEach(d => sel.delete(d.path));
        modal.remove();
        // Force refresh
        if (type === "movie") loadMovies(true);
        else loadTV(true);
      } else {
        alert("Delete failed: " + (result.error || "Unknown error"));
        confirmBtn.disabled = false;
        confirmBtn.textContent = "Delete " + items.length + " file(s)";
      }
    } catch (e) {
      alert("Delete failed: " + e.message);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Delete " + items.length + " file(s)";
    }
  });

  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
  const handleEscape = (e) => {
    if (e.key === "Escape") { modal.remove(); document.removeEventListener("keydown", handleEscape); }
  };
  document.addEventListener("keydown", handleEscape);
}

async function handleBulkEnrich(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Fetch metadata for ${eligible.length} item(s)?`)) return;

  let done = 0, nfos = 0, posters = 0;
  showSubsToast({ _searching: true, _custom_searching: `Enriching 0/${eligible.length}...` });

  for (const item of eligible) {
    try {
      const r = await fetch(`${API}/media/enrich`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ path: item.path })
      });
      const result = await r.json();
      if (result.nfo_written) nfos++;
      if (result.poster_downloaded) posters++;
    } catch (e) { /* continue */ }
    done++;
    const existing = document.querySelector(".subs-toast");
    if (existing) existing.innerHTML = `<span class="toast-icon spin">&#9881;</span> Enriching ${done}/${eligible.length}...`;
  }

  showSubsToast({ _custom: `Done: ${nfos} NFOs, ${posters} posters` });
  if (type === "movie") loadMovies(false); else loadTV(false);
}

async function handleEnrichAll() {
  const enrichBtn = event ? event.target : null;
  if (enrichBtn) { enrichBtn.disabled = true; enrichBtn.textContent = "Starting..."; }

  try {
    const r = await fetch(`${API}/media/enrich-all`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
    });
    const result = await r.json();
    if (!r.ok) {
      alert(result.error || "Failed to start enrichment");
      if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
      return;
    }

    // Poll progress
    const pollInterval = setInterval(async () => {
      try {
        const sr = await fetch(`${API}/media/enrich-status`);
        const status = await sr.json();

        if (enrichBtn) {
          enrichBtn.textContent = `${status.processed}/${status.total} (${status.nfo_written} NFOs)`;
        }

        if (!status.running) {
          clearInterval(pollInterval);
          if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
          showSubsToast({ _custom: `Enrichment complete: ${status.nfo_written} NFOs, ${status.posters_downloaded} posters` });
          loadMovies(false);
          loadTV(false);
        }
      } catch (e) {
        clearInterval(pollInterval);
        if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
      }
    }, 2000);
  } catch (e) {
    alert("Error: " + e.message);
    if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
  }
}

async function loadMovies(forceRefresh = false){
  const body = $("#movies-body");
  const refreshBtn = $("#refresh-movies");

  // Show skeleton only on first load (empty table)
  if (!body.innerHTML.trim() || body.querySelector(".skeleton-row")) {
    body.innerHTML = moviesSkeleton();
  }

  try{
    const url = forceRefresh ? `${API}/media/movies?refresh=1` : `${API}/media/movies`;
    const r = await fetch(url, {headers:{Accept:"application/json"}, cache:"no-store"});
    if (!r.ok) { body.innerHTML = ""; return; }
    const data = await r.json();
    const items = Array.isArray(data.items) ? data.items : [];

    renderMoviesTable(items);

    // Handle scanning state
    const wasScanning = moviesScanning;
    moviesScanning = data.scanning === true;

    if (moviesScanning) {
      refreshBtn.textContent = "Scanning...";
      refreshBtn.disabled = true;
    } else {
      refreshBtn.textContent = "Refresh";
      refreshBtn.disabled = false;
      // If scan just finished, refresh to get updated data
      if (wasScanning) {
        setTimeout(() => loadMovies(false), 500);
      }
    }
  } catch { body.innerHTML = ""; }
}

function renderTVTable(items) {
  const body = $("#tv-body");
  const thead = $("#tv-thead");
  currentMediaItems.tv = items;

  // Stats on raw (unfiltered) items
  const readyItems = items.filter(e => e.status === "ready");
  tvStats.count = readyItems.length;
  tvStats.sizeGb = readyItems.reduce((sum, e) => sum + (e.size_gb || 0), 0);
  updateMediaStats();

  // Build displayed array through filter → sort pipeline
  const displayed = getDisplayedTV();

  // Dynamic thead
  thead.innerHTML = tvTheadHtml();

  if (displayed.length === 0) {
    body.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">${items.length === 0 ? "No TV shows found" : "No matches"}</td></tr>`;
    updateBulkActionBar("tv");
    updateSelectAllState("tv");
    return;
  }

  hasProcessingItems = items.some(e => e.status === "processing" || e.status === "queued" || e.status === "pending" || e.status === "re-encoding") || hasProcessingItems;
  body.innerHTML = displayed.map((e, idx) => {
    let epLabel = "-";
    if (e.season != null && e.episode != null) {
      const s = String(e.season).padStart(2, "0");
      if (e.episodes && e.episodes.length > 1) {
        const first = String(e.episodes[0]).padStart(2, "0");
        const last = String(e.episodes[e.episodes.length - 1]).padStart(2, "0");
        epLabel = `S${s}E${first}-E${last}`;
      } else {
        epLabel = `S${s}E${String(e.episode).padStart(2, "0")}`;
      }
    }
    const runtime = e.runtime_min ? `${e.runtime_min} min` : "";
    const codec = e.vcodec || "";
    const meta = [runtime, codec].filter(Boolean).join(" · ");
    const rowClass = e.status === "processing" ? "processing-row" : e.status === "queued" ? "queued-row" : e.status === "pending" ? "pending-row" : "";
    const ignoredClass = e.ignored ? " ignored-row" : "";
    const changed = e.status === "processing" ? (e.elapsed_fmt || "...") : (e.mtime_fmt || "-");
    const checked = tvSelection.has(e.path) ? "checked" : "";
    return `
    <tr class="${rowClass}${ignoredClass}">
      <td class="select-cell"><input type="checkbox" class="row-select" data-type="tv" data-idx="${idx}" ${checked}></td>
      <td class="poster-cell">${posterHtml(e.poster)}</td>
      <td>
        <div class="title-cell">
          <span class="title-main title-clickable" data-type="tv" data-idx="${idx}">${e.show || "-"}</span>
          ${meta ? `<span class="title-meta">${meta}</span>` : ""}
        </div>
      </td>
      <td>${epLabel}</td>
      <td>${fmtRes(e.resolution)}</td>
      <td>${e.size_gb != null ? `${e.size_gb.toFixed(2)} GB` : "-"}</td>
      <td class="changed-cell">${changed}</td>
      <td>${statusHtml(e.status, e.progress, e.ignored, e.reencode_progress)}</td>
      <td class="action-cell">${actionHtml(e, "tv", idx)}</td>
    </tr>
  `}).join("");

  // Wire sort headers
  thead.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (tvSort.col === col) {
        tvSort.dir = tvSort.dir === "asc" ? "desc" : "asc";
      } else {
        tvSort.col = col;
        tvSort.dir = "asc";
      }
      renderTVTable(currentMediaItems.tv);
    });
  });

  // Wire select-all
  const selectAll = thead.querySelector(".select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      if (selectAll.checked) {
        displayed.forEach(d => tvSelection.add(d.path));
      } else {
        displayed.forEach(d => tvSelection.delete(d.path));
      }
      renderTVTable(currentMediaItems.tv);
    });
  }

  // Wire row checkboxes
  body.querySelectorAll(".row-select").forEach(cb => {
    cb.addEventListener("change", () => {
      const idx = parseInt(cb.dataset.idx, 10);
      const item = displayedTV[idx];
      if (!item) return;
      if (cb.checked) tvSelection.add(item.path);
      else tvSelection.delete(item.path);
      updateBulkActionBar("tv");
      updateSelectAllState("tv");
    });
  });

  // Click handlers use displayedTV
  body.querySelectorAll(".title-clickable").forEach(el => {
    el.addEventListener("click", () => {
      const idx = parseInt(el.dataset.idx, 10);
      const item = displayedTV[idx];
      if (item) showMediaModal(item, "tv");
    });
  });

  body.querySelectorAll(".btn-stop").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) handleStopClick(item, "tv");
    });
  });

  body.querySelectorAll(".btn-transcode").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) handleTranscodeClick(item, "tv");
    });
  });

  body.querySelectorAll(".btn-ignore").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) handleIgnoreClick(item, "tv");
    });
  });

  body.querySelectorAll(".btn-subs").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) showSubtitleSearchModal(item, "tv");
    });
  });

  body.querySelectorAll(".btn-delete-subs").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) handleDeleteSubsClick(item, "tv");
    });
  });

  body.querySelectorAll(".btn-enrich").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = displayedTV[parseInt(el.dataset.idx, 10)];
      if (item) handleEnrichClick(item, "tv");
    });
  });

  updateBulkActionBar("tv");
  updateSelectAllState("tv");
}

async function loadTV(forceRefresh = false){
  const body = $("#tv-body");
  const refreshBtn = $("#refresh-tv");

  if (!body.innerHTML.trim() || body.querySelector(".skeleton-row")) {
    body.innerHTML = tvSkeleton();
  }

  try{
    const url = forceRefresh ? `${API}/media/tv?refresh=1` : `${API}/media/tv`;
    const r = await fetch(url, {headers:{Accept:"application/json"}, cache:"no-store"});
    if (!r.ok) { body.innerHTML = ""; return; }
    const data = await r.json();
    const items = Array.isArray(data.items) ? data.items : [];

    renderTVTable(items);

    const wasScanning = tvScanning;
    tvScanning = data.scanning === true;

    if (tvScanning) {
      refreshBtn.textContent = "Scanning...";
      refreshBtn.disabled = true;
    } else {
      refreshBtn.textContent = "Refresh";
      refreshBtn.disabled = false;
      if (wasScanning) {
        setTimeout(() => loadTV(false), 500);
      }
    }
  } catch { body.innerHTML = ""; }
}
  $("#refresh-movies").addEventListener("click", () => loadMovies(true));
  $("#refresh-tv").addEventListener("click", () => loadTV(true));
  const _enrichAllMovies = $("#enrich-all-movies");
  if (_enrichAllMovies) _enrichAllMovies.addEventListener("click", () => handleEnrichAll());
  const _enrichAllTV = $("#enrich-all-tv");
  if (_enrichAllTV) _enrichAllTV.addEventListener("click", () => handleEnrichAll());

  // ----- Filter event listeners -----
  let _movieSearchTimer = null;
  $("#movie-search").addEventListener("input", (e) => {
    clearTimeout(_movieSearchTimer);
    _movieSearchTimer = setTimeout(() => {
      movieFilter.text = e.target.value.trim();
      renderMoviesTable(currentMediaItems.movies);
    }, 200);
  });
  $("#movie-status-filter").addEventListener("change", (e) => {
    movieFilter.status = e.target.value;
    renderMoviesTable(currentMediaItems.movies);
  });

  let _tvSearchTimer = null;
  $("#tv-search").addEventListener("input", (e) => {
    clearTimeout(_tvSearchTimer);
    _tvSearchTimer = setTimeout(() => {
      tvFilter.text = e.target.value.trim();
      renderTVTable(currentMediaItems.tv);
    }, 200);
  });
  $("#tv-status-filter").addEventListener("change", (e) => {
    tvFilter.status = e.target.value;
    renderTVTable(currentMediaItems.tv);
  });

  // ----- Bulk action button listeners -----
  $$(".bulk-transcode").forEach(btn => {
    btn.addEventListener("click", () => handleBulkTranscode(btn.dataset.type));
  });
  $$(".bulk-ignore").forEach(btn => {
    btn.addEventListener("click", () => handleBulkIgnore(btn.dataset.type));
  });
  $$(".bulk-delete").forEach(btn => {
    btn.addEventListener("click", () => handleBulkDelete(btn.dataset.type));
  });
  $$(".bulk-enrich").forEach(btn => {
    btn.addEventListener("click", () => handleBulkEnrich(btn.dataset.type));
  });

  // ----- Settings -----
  let settingsSchema = {};
  let settingsOriginal = {};
  let settingsModified = {};
  let currentSection = null;
  let encodingPresets = [];
  let activePresetId = null;

  async function loadSettings() {
    const container = $("#settings-container");
    const navContainer = $("#settings-subnav");
    container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--muted)">Loading...</div>`;
    navContainer.innerHTML = "";

    try {
      const r = await fetch(`${API}/settings`, {headers:{Accept:"application/json"}});
      const data = await r.json();

      if (!r.ok || data.error) {
        throw new Error(data.error || `HTTP ${r.status}`);
      }

      settingsSchema = data.schema;
      settingsOriginal = {...data.values};
      settingsModified = {...data.values};
      encodingPresets = data.encoding_presets || [];

      renderSettingsNav();
      // Show first section by default
      const firstSection = Object.keys(settingsSchema)[0];
      if (firstSection) showSettingsSection(firstSection);
    } catch (e) {
      container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--danger)">Failed to load: ${e.message}</div>`;
      console.error("Settings load error:", e);
    }
  }

  function renderSettingsNav() {
    const navContainer = $("#settings-subnav");
    navContainer.innerHTML = "";

    for (const [sectionKey, section] of Object.entries(settingsSchema)) {
      const btn = document.createElement("button");
      btn.className = "nav-subitem";
      btn.dataset.section = sectionKey;
      btn.textContent = section.label;
      btn.addEventListener("click", () => {
        // Show settings view
        $$(".nav-item").forEach(x => x.classList.remove("active"));
        settingsNavItem.classList.add("active");
        $$(".view").forEach(v => v.classList.toggle("visible", v.id === "view-settings"));
        // Show selected section
        showSettingsSection(sectionKey);
      });
      navContainer.appendChild(btn);
    }
  }

  function showSettingsSection(sectionKey) {
    currentSection = sectionKey;
    const section = settingsSchema[sectionKey];
    if (!section) return;

    // Update nav active state in sidebar subnav
    $$(".nav-subitem").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.section === sectionKey);
    });

    // Update title
    $("#settings-section-title").textContent = section.label;

    // Render fields
    const container = $("#settings-container");
    container.innerHTML = "";

    // Special handling for connections section
    if (section.type === "connections") {
      renderConnectionsSection(container);
      return;
    }

    // Special handling for subtitle providers section
    if (section.type === "subtitle_providers") {
      renderSubtitleProvidersSection(container, section);
      return;
    }

    // Special handling for encoding section — presets + two-column layout
    if (sectionKey === "encoding") {
      renderEncodingSection(container, section);
      return;
    }

    renderGenericFields(container, section);
  }

  function renderGenericFields(container, section) {
    const fieldsDiv = document.createElement("div");
    fieldsDiv.className = "settings-fields";

    for (const [fieldKey, field] of Object.entries(section.fields)) {
      renderSettingField(fieldsDiv, fieldKey, field);
    }

    container.appendChild(fieldsDiv);
  }

  function renderSettingField(parent, fieldKey, field) {
    const value = settingsModified[fieldKey] || "";
    const isModified = settingsModified[fieldKey] !== settingsOriginal[fieldKey];
    const isPassword = field.type === "password";
    const isSelect = field.type === "select";

    const fieldEl = document.createElement("div");
    fieldEl.className = `setting-field${isModified ? " modified" : ""}`;
    fieldEl.dataset.key = fieldKey;

    if (isSelect) {
      const optionsHtml = (field.options || []).map(opt =>
        `<option value="${escapeHtml(opt.value)}"${opt.value === value ? " selected" : ""}>${escapeHtml(opt.label)}</option>`
      ).join("");

      fieldEl.innerHTML = `
        <label for="set-${fieldKey}">${field.label}</label>
        <div class="input-wrap">
          <select id="set-${fieldKey}" data-key="${fieldKey}">${optionsHtml}</select>
        </div>
      `;
      parent.appendChild(fieldEl);

      const select = fieldEl.querySelector("select");
      select.addEventListener("change", (e) => {
        settingsModified[fieldKey] = e.target.value;
        updateSettingsUI();
      });
    } else {
      fieldEl.innerHTML = `
        <label for="set-${fieldKey}">${field.label}</label>
        <div class="input-wrap">
          <input type="${isPassword ? "password" : "text"}"
                 id="set-${fieldKey}"
                 data-key="${fieldKey}"
                 placeholder="${field.placeholder || ""}"
                 value="${escapeHtml(value)}"
                 autocomplete="off"
                 ${field.readonly ? "disabled" : ""}>
          ${isPassword ? `<button type="button" class="btn-reveal" data-for="set-${fieldKey}">Show</button>` : ""}
        </div>
      `;
      parent.appendChild(fieldEl);

      const input = fieldEl.querySelector("input");
      input.addEventListener("input", (e) => {
        settingsModified[fieldKey] = e.target.value;
        updateSettingsUI();
      });

      if (isPassword) {
        const revealBtn = fieldEl.querySelector(".btn-reveal");
        revealBtn.addEventListener("click", () => {
          const isHidden = input.type === "password";
          input.type = isHidden ? "text" : "password";
          revealBtn.textContent = isHidden ? "Hide" : "Show";
        });
      }
    }
  }

  // ----- Encoding Section with Presets -----

  function renderEncodingSection(container, section) {
    // 1. Preset strip
    const strip = document.createElement("div");
    strip.className = "preset-strip";
    strip.innerHTML = `
      <div class="preset-strip-header">
        <h3>Presets</h3>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn btn-ghost" id="btn-restore-presets" style="font-size:12px">Restore Defaults</button>
          <button type="button" class="btn btn-ghost" id="btn-new-preset">+ New Preset</button>
        </div>
      </div>
      <div class="preset-cards" id="preset-cards"></div>
      <div class="new-preset-form" id="new-preset-form" style="display:none">
        <input type="text" class="new-preset-input" id="new-preset-name" placeholder="Preset name..." maxlength="40">
        <button type="button" class="btn btn-primary" id="btn-save-preset">Save</button>
        <button type="button" class="btn btn-ghost" id="btn-cancel-preset">Cancel</button>
      </div>
    `;
    container.appendChild(strip);

    renderPresetCards();
    detectActivePreset();

    // Wire preset form
    $("#btn-new-preset").addEventListener("click", () => {
      $("#new-preset-form").style.display = "flex";
      $("#new-preset-name").focus();
    });
    $("#btn-cancel-preset").addEventListener("click", () => {
      $("#new-preset-form").style.display = "none";
      $("#new-preset-name").value = "";
    });
    $("#btn-save-preset").addEventListener("click", () => createPreset());
    $("#new-preset-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") createPreset();
    });
    $("#btn-restore-presets").addEventListener("click", () => restoreDefaultPresets());

    // 2. Two-column field groups
    const groups = {video: [], audio: [], advanced: []};
    for (const [fieldKey, field] of Object.entries(section.fields)) {
      const group = field.group || "advanced";
      if (groups[group]) groups[group].push([fieldKey, field]);
    }

    const columns = document.createElement("div");
    columns.className = "encoding-columns";

    // Video group
    const videoGroup = document.createElement("div");
    videoGroup.className = "encoding-group";
    videoGroup.innerHTML = `<div class="encoding-group-title">Video</div>`;
    const videoFields = document.createElement("div");
    videoFields.className = "settings-fields";
    for (const [key, field] of groups.video) renderSettingField(videoFields, key, field);
    videoGroup.appendChild(videoFields);
    columns.appendChild(videoGroup);

    // Audio group
    const audioGroup = document.createElement("div");
    audioGroup.className = "encoding-group";
    audioGroup.innerHTML = `<div class="encoding-group-title">Audio</div>`;
    const audioFields = document.createElement("div");
    audioFields.className = "settings-fields";
    for (const [key, field] of groups.audio) renderSettingField(audioFields, key, field);
    audioGroup.appendChild(audioFields);
    columns.appendChild(audioGroup);

    // Advanced group (full width)
    if (groups.advanced.length > 0) {
      const advGroup = document.createElement("div");
      advGroup.className = "encoding-group encoding-advanced";
      advGroup.innerHTML = `<div class="encoding-group-title">Advanced</div>`;
      const advFields = document.createElement("div");
      advFields.className = "settings-fields";
      for (const [key, field] of groups.advanced) renderSettingField(advFields, key, field);
      advGroup.appendChild(advFields);
      columns.appendChild(advGroup);
    }

    container.appendChild(columns);

    // 3. Compression tiers
    renderCompressionTiersPanel(container);
  }

  function renderPresetCards() {
    const cardsDiv = $("#preset-cards");
    if (!cardsDiv) return;
    cardsDiv.innerHTML = "";

    for (const preset of encodingPresets) {
      const card = document.createElement("div");
      card.className = `preset-card${preset.id === activePresetId ? " active" : ""}`;
      card.dataset.presetId = preset.id;

      let html = `
        <div class="preset-card-name">${escapeHtml(preset.name)}</div>
        <div class="preset-card-badge">${preset.is_default ? "Built-in" : "Custom"}</div>
      `;
      if (preset.id === activePresetId) {
        html += `<div class="preset-card-active">● Active</div>`;
      }
      if (!preset.is_default) {
        html += `<button type="button" class="preset-card-delete" title="Delete preset">&times;</button>`;
      }
      card.innerHTML = html;

      card.addEventListener("click", (e) => {
        if (e.target.classList.contains("preset-card-delete")) return;
        applyPreset(preset);
      });

      const delBtn = card.querySelector(".preset-card-delete");
      if (delBtn) {
        delBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          deletePreset(preset.id, preset.name);
        });
      }

      cardsDiv.appendChild(card);
    }
  }

  function detectActivePreset() {
    activePresetId = null;
    const encodingFields = settingsSchema.encoding ? Object.keys(settingsSchema.encoding.fields) : [];

    for (const preset of encodingPresets) {
      const settings = preset.settings || {};
      let matches = true;
      for (const key of encodingFields) {
        if (key in settings) {
          const current = settingsModified[key] || "";
          const presetVal = settings[key] || "";
          if (current !== presetVal) { matches = false; break; }
        }
      }
      if (matches) { activePresetId = preset.id; break; }
    }

    // Update card active states
    $$(".preset-card").forEach(card => {
      const id = parseInt(card.dataset.presetId);
      const isActive = id === activePresetId;
      card.classList.toggle("active", isActive);
      const dot = card.querySelector(".preset-card-active");
      if (isActive && !dot) {
        const d = document.createElement("div");
        d.className = "preset-card-active";
        d.textContent = "● Active";
        card.appendChild(d);
      } else if (!isActive && dot) {
        dot.remove();
      }
    });
  }

  function applyPreset(preset) {
    const settings = preset.settings || {};
    for (const [key, val] of Object.entries(settings)) {
      settingsModified[key] = val;
      const el = document.querySelector(`#set-${key}`);
      if (el) el.value = val;
    }
    updateSettingsUI();
  }

  async function createPreset() {
    const nameInput = $("#new-preset-name");
    const name = nameInput.value.trim();
    if (!name) return;

    // Collect current encoding values
    const encodingFields = settingsSchema.encoding ? Object.keys(settingsSchema.encoding.fields) : [];
    const settings = {};
    for (const key of encodingFields) {
      settings[key] = settingsModified[key] || "";
    }

    try {
      const r = await fetch(`${API}/encoding-presets`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, settings}),
      });
      const data = await r.json();
      if (!r.ok) {
        alert(data.error || "Failed to create preset");
        return;
      }
      encodingPresets.push(data.preset);
      renderPresetCards();
      detectActivePreset();
      $("#new-preset-form").style.display = "none";
      nameInput.value = "";
    } catch (e) {
      alert("Failed to create preset: " + e.message);
    }
  }

  async function deletePreset(id, name) {
    if (!confirm(`Delete preset "${name}"?`)) return;
    try {
      const r = await fetch(`${API}/encoding-presets/${id}`, {method: "DELETE"});
      if (r.ok) {
        encodingPresets = encodingPresets.filter(p => p.id !== id);
        renderPresetCards();
        detectActivePreset();
      }
    } catch (e) {
      alert("Failed to delete preset: " + e.message);
    }
  }

  async function restoreDefaultPresets() {
    try {
      const r = await fetch(`${API}/encoding-presets/restore`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        // Refresh full preset list
        const pr = await fetch(`${API}/encoding-presets`);
        const pd = await pr.json();
        encodingPresets = pd.presets || [];
        renderPresetCards();
        detectActivePreset();
      }
    } catch (e) {
      alert("Failed to restore presets: " + e.message);
    }
  }

  // ----- Compression Tiers Panel -----
  async function renderCompressionTiersPanel(container) {
    const panel = document.createElement("div");
    panel.className = "compression-tiers-panel";
    panel.id = "compression-tiers-panel";
    panel.innerHTML = `<div style="text-align:center;color:var(--muted);padding:12px">Loading tiers...</div>`;
    container.appendChild(panel);

    // Check initial visibility from the toggle
    const enabledSelect = document.getElementById("set-COMPRESSION_TIERS_ENABLED");
    const isEnabled = enabledSelect ? enabledSelect.value === "true" : false;
    panel.style.display = isEnabled ? "block" : "none";

    // Wire up toggle to show/hide
    if (enabledSelect) {
      enabledSelect.addEventListener("change", () => {
        panel.style.display = enabledSelect.value === "true" ? "block" : "none";
      });
    }

    // Fetch tiers data
    let tiersData;
    try {
      const r = await fetch(`${API}/compression-tiers`, {headers:{Accept:"application/json"}, cache:"no-store"});
      tiersData = r.ok ? await r.json() : {tiers:[], preset_options:[], crf_options:[]};
    } catch(e) {
      tiersData = {tiers:[], preset_options:[], crf_options:[]};
    }

    let tiers = tiersData.tiers || [];
    const presetOpts = tiersData.preset_options || [];
    const crfOpts = tiersData.crf_options || [];

    function renderTiersTable() {
      let html = `
        <h4>Compression Tiers</h4>
        <p class="tiers-desc">Override preset and CRF based on source file size. Larger files get slower presets for better compression; smaller files use faster presets.</p>
      `;

      if (tiers.length === 0) {
        html += `<div class="tiers-empty">No tiers configured. Click "Add Tier" to create one.</div>`;
      } else {
        html += `<table class="tiers-table"><thead><tr>
          <th>Min GB</th><th>Max GB</th><th>Preset</th><th>CRF</th><th></th>
        </tr></thead><tbody>`;
        tiers.forEach((tier, i) => {
          const presetSelect = presetOpts.map(o =>
            `<option value="${escapeHtml(o.value)}"${o.value === tier.preset ? " selected" : ""}>${escapeHtml(o.label)}</option>`
          ).join("");
          const crfSelect = crfOpts.map(o =>
            `<option value="${escapeHtml(o.value)}"${o.value === (tier.crf||"") ? " selected" : ""}>${escapeHtml(o.label)}</option>`
          ).join("");
          html += `<tr>
            <td><input type="number" class="tier-input" data-idx="${i}" data-field="min_gb" value="${tier.min_gb}" min="0" step="0.5"></td>
            <td><input type="number" class="tier-input" data-idx="${i}" data-field="max_gb" value="${tier.max_gb}" min="0" step="0.5" placeholder="0 = unlimited"></td>
            <td><select class="tier-select" data-idx="${i}" data-field="preset">${presetSelect}</select></td>
            <td><select class="tier-select" data-idx="${i}" data-field="crf">${crfSelect}</select></td>
            <td><button class="btn-delete-tier" data-idx="${i}" title="Remove tier">&times;</button></td>
          </tr>`;
        });
        html += `</tbody></table>`;
      }

      html += `<div class="tiers-actions">
        <button class="btn btn-ghost" id="btn-add-tier">+ Add Tier</button>
        <button class="btn btn-primary" id="btn-save-tiers">Save Tiers</button>
        <span class="tiers-status" id="tiers-status"></span>
      </div>`;

      panel.innerHTML = html;

      // Wire up input/select change events
      panel.querySelectorAll(".tier-input, .tier-select").forEach(el => {
        el.addEventListener("change", () => {
          const idx = parseInt(el.dataset.idx);
          const field = el.dataset.field;
          tiers[idx][field] = el.value;
        });
      });

      // Wire up delete buttons
      panel.querySelectorAll(".btn-delete-tier").forEach(btn => {
        btn.addEventListener("click", () => {
          tiers.splice(parseInt(btn.dataset.idx), 1);
          renderTiersTable();
        });
      });

      // Add tier button
      const addBtn = panel.querySelector("#btn-add-tier");
      if (addBtn) {
        addBtn.addEventListener("click", () => {
          const lastMax = tiers.length > 0 ? parseFloat(tiers[tiers.length-1].max_gb || 0) : 0;
          tiers.push({min_gb: lastMax, max_gb: 0, preset: "medium", crf: ""});
          renderTiersTable();
        });
      }

      // Save tiers button
      const saveBtn = panel.querySelector("#btn-save-tiers");
      if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
          const statusEl = panel.querySelector("#tiers-status");
          statusEl.textContent = "Saving...";
          statusEl.className = "tiers-status";
          try {
            const r = await fetch(`${API}/compression-tiers`, {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({tiers}),
            });
            const result = await r.json();
            if (r.ok) {
              tiers = result.tiers || tiers;
              statusEl.textContent = "Saved!";
              statusEl.className = "tiers-status success";
              renderTiersTable();
            } else {
              statusEl.textContent = result.error || "Save failed";
              statusEl.className = "tiers-status error";
            }
          } catch(e) {
            statusEl.textContent = "Network error";
            statusEl.className = "tiers-status error";
          }
          setTimeout(() => {
            const s = panel.querySelector("#tiers-status");
            if (s) s.textContent = "";
          }, 3000);
        });
      }
    }

    renderTiersTable();
  }

  // ----- Connections Section -----
  async function renderConnectionsSection(container) {
    container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--muted)">Loading connections...</div>`;

    try {
      const r = await fetch(`${API}/connections`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const data = r.ok ? await r.json() : {};

      container.innerHTML = `
        <div class="connections-grid">
          <div class="connection-card" id="conn-radarr">
            <div class="connection-header">
              <span class="connection-icon">🎬</span>
              <h3>Radarr</h3>
            </div>
            <div class="connection-status" id="radarr-status">
              ${renderConnectionStatus(data.radarr)}
            </div>
            <div class="connection-actions">
              ${renderConnectionActions("radarr", data.radarr)}
            </div>
          </div>

          <div class="connection-card" id="conn-sonarr">
            <div class="connection-header">
              <span class="connection-icon">📺</span>
              <h3>Sonarr</h3>
            </div>
            <div class="connection-status" id="sonarr-status">
              ${renderConnectionStatus(data.sonarr)}
            </div>
            <div class="connection-actions">
              ${renderConnectionActions("sonarr", data.sonarr)}
            </div>
          </div>
        </div>

        <div class="connections-info">
          <p>Webhooks allow Radarr/Sonarr to notify Transcodarr when new media is imported, eliminating the need for external post-processing scripts.</p>
          <p>Make sure the Radarr/Sonarr URL and API Key are configured in their respective settings sections.</p>
        </div>
      `;

      // Add event listeners
      container.querySelectorAll(".btn-connect").forEach(btn => {
        btn.addEventListener("click", () => connectService(btn.dataset.service));
      });
      container.querySelectorAll(".btn-disconnect").forEach(btn => {
        btn.addEventListener("click", () => disconnectService(btn.dataset.service));
      });
      container.querySelectorAll(".btn-test").forEach(btn => {
        btn.addEventListener("click", () => testConnection(btn.dataset.service));
      });

    } catch (e) {
      container.innerHTML = `<div style="padding:20px;color:var(--danger)">Failed to load connections: ${e.message}</div>`;
    }
  }

  function renderConnectionStatus(conn) {
    if (!conn) return `<span class="conn-badge conn-unknown">Unknown</span>`;
    if (!conn.configured) {
      return `<span class="conn-badge conn-not-configured">Not Configured</span><p class="conn-hint">Configure URL and API key in settings first</p>`;
    }
    if (conn.error) {
      return `<span class="conn-badge conn-error">Error</span><p class="conn-hint">${conn.error}</p>`;
    }
    if (conn.connected) {
      return `<span class="conn-badge conn-connected">Connected</span><p class="conn-hint">Webhook registered</p>`;
    }
    return `<span class="conn-badge conn-disconnected">Not Connected</span><p class="conn-hint">Webhook not registered</p>`;
  }

  function renderConnectionActions(service, conn) {
    if (!conn || !conn.configured) {
      return `<button class="btn" disabled>Connect</button>`;
    }
    if (conn.connected) {
      return `
        <button class="btn btn-ghost btn-test" data-service="${service}">Test</button>
        <button class="btn btn-disconnect" data-service="${service}">Disconnect</button>
      `;
    }
    return `<button class="btn btn-primary btn-connect" data-service="${service}">Connect</button>`;
  }

  async function connectService(service) {
    const btn = document.querySelector(`.btn-connect[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Connecting..."; }

    try {
      const r = await fetch(`${API}/connections/${service}`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        // Refresh the connections view
        renderConnectionsSection($("#settings-container"));
      } else {
        alert(`Failed to connect: ${data.error || "Unknown error"}`);
        if (btn) { btn.disabled = false; btn.textContent = "Connect"; }
      }
    } catch (e) {
      alert(`Failed to connect: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Connect"; }
    }
  }

  async function disconnectService(service) {
    if (!confirm(`Disconnect ${service}? The webhook will be removed.`)) return;

    const btn = document.querySelector(`.btn-disconnect[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Disconnecting..."; }

    try {
      const r = await fetch(`${API}/connections/${service}`, {method: "DELETE"});
      const data = await r.json();
      if (r.ok) {
        renderConnectionsSection($("#settings-container"));
      } else {
        alert(`Failed to disconnect: ${data.error || "Unknown error"}`);
        if (btn) { btn.disabled = false; btn.textContent = "Disconnect"; }
      }
    } catch (e) {
      alert(`Failed to disconnect: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Disconnect"; }
    }
  }

  async function testConnection(service) {
    const btn = document.querySelector(`.btn-test[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Testing..."; }

    try {
      const r = await fetch(`${API}/connections/${service}/test`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        alert(`${service} test successful!`);
      } else {
        alert(`Test failed: ${data.error || "Unknown error"}`);
      }
    } catch (e) {
      alert(`Test failed: ${e.message}`);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test"; }
    }
  }

  // ----- Subtitle Providers Section -----
  async function renderSubtitleProvidersSection(container, section) {
    container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--muted)">Loading providers...</div>`;

    try {
      const r = await fetch(`${API}/subtitle-providers`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const data = r.ok ? await r.json() : {providers: {}};

      let providersHtml = "";
      for (const [providerId, provider] of Object.entries(data.providers)) {
        // All providers get a toggle; auth providers also get account management
        const toggleHtml = renderProviderToggle(providerId, provider);
        const accountsHtml = provider.requires_auth && provider.supports_multiple_accounts
          ? renderProviderAccounts(providerId, provider)
          : "";

        providersHtml += `
          <div class="provider-card" data-provider="${providerId}">
            <div class="provider-header">
              <h3>${escapeHtml(provider.name)}</h3>
              <span class="provider-status ${provider.enabled ? 'enabled' : 'disabled'}">
                ${provider.enabled ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            <div class="provider-content">
              ${toggleHtml}
              ${accountsHtml}
            </div>
          </div>
        `;
      }

      // Also render the regular fields (like FFSUBSYNC_MAX_OFFSET)
      let fieldsHtml = "";
      if (section.fields && Object.keys(section.fields).length > 0) {
        fieldsHtml = `<div class="settings-fields subtitle-settings-fields">`;
        for (const [fieldKey, field] of Object.entries(section.fields)) {
          const value = settingsModified[fieldKey] || "";
          const isModified = settingsModified[fieldKey] !== settingsOriginal[fieldKey];
          const isPassword = field.type === "password";

          fieldsHtml += `
            <div class="setting-field${isModified ? " modified" : ""}" data-key="${fieldKey}">
              <label for="set-${fieldKey}">${field.label}</label>
              <div class="input-wrap">
                <input type="${isPassword ? "password" : "text"}"
                       id="set-${fieldKey}"
                       data-key="${fieldKey}"
                       placeholder="${field.placeholder || ""}"
                       value="${escapeHtml(value)}"
                       autocomplete="off">
                ${isPassword ? `<button type="button" class="btn-reveal" data-for="set-${fieldKey}">Show</button>` : ""}
              </div>
            </div>
          `;
        }
        fieldsHtml += `</div>`;
      }

      container.innerHTML = `
        <div class="subtitle-providers-section">
          <div class="providers-grid">
            ${providersHtml}
          </div>
          ${fieldsHtml}
          <div class="providers-info">
            <p>Add multiple accounts for OpenSubtitles.com to rotate through when download limits are reached.</p>
            <p>Enable Podnapisi as a fallback provider (no account required).</p>
          </div>
        </div>
      `;

      // Add event listeners for the regular settings fields
      container.querySelectorAll(".subtitle-settings-fields input").forEach(input => {
        input.addEventListener("input", (e) => {
          settingsModified[e.target.dataset.key] = e.target.value;
          updateSettingsUI();
        });
      });

      // Add event listeners for provider actions
      container.querySelectorAll(".btn-add-account").forEach(btn => {
        btn.addEventListener("click", () => showAddAccountModal(btn.dataset.provider));
      });
      container.querySelectorAll(".btn-remove-account").forEach(btn => {
        btn.addEventListener("click", () => removeProviderAccount(btn.dataset.provider, btn.dataset.username));
      });
      container.querySelectorAll(".provider-toggle").forEach(toggle => {
        toggle.addEventListener("change", (e) => toggleProvider(e.target.dataset.provider, e.target.checked));
      });

    } catch (e) {
      container.innerHTML = `<div style="padding:20px;color:var(--danger)">Failed to load providers: ${e.message}</div>`;
    }
  }

  function renderProviderAccounts(providerId, provider) {
    let accountsListHtml = "";
    if (provider.accounts && provider.accounts.length > 0) {
      for (const acc of provider.accounts) {
        accountsListHtml += `
          <div class="account-item">
            <span class="account-user">${escapeHtml(acc.user)}</span>
            <span class="account-status">${acc.has_pass ? '●' : '○'}</span>
            <button class="btn btn-sm btn-ghost btn-remove-account" data-provider="${providerId}" data-username="${escapeHtml(acc.user)}" title="Remove account">✕</button>
          </div>
        `;
      }
    } else {
      accountsListHtml = `<div class="no-accounts">No accounts configured</div>`;
    }

    return `
      <div class="provider-accounts">
        <div class="accounts-list">
          ${accountsListHtml}
        </div>
        <button class="btn btn-sm btn-add-account" data-provider="${providerId}">+ Add Account</button>
      </div>
    `;
  }

  function renderProviderToggle(providerId, provider) {
    return `
      <div class="provider-toggle-wrap">
        <label class="toggle-label">
          <input type="checkbox" class="provider-toggle" data-provider="${providerId}" ${provider.enabled ? 'checked' : ''}>
          <span class="toggle-text">${provider.enabled ? 'Enabled' : 'Disabled'}</span>
        </label>
      </div>
    `;
  }

  function showAddAccountModal(providerId) {
    // Create modal
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.innerHTML = `
      <div class="modal-content add-account-modal">
        <h3>Add Account</h3>
        <div class="modal-field">
          <label>Username</label>
          <input type="text" id="new-account-user" placeholder="username" autocomplete="off">
        </div>
        <div class="modal-field">
          <label>Password</label>
          <input type="password" id="new-account-pass" placeholder="password" autocomplete="off">
        </div>
        <div class="modal-actions">
          <button class="btn btn-ghost modal-cancel">Cancel</button>
          <button class="btn btn-primary modal-confirm">Add</button>
        </div>
      </div>
    `;

    document.body.appendChild(modal);

    const userInput = modal.querySelector("#new-account-user");
    const passInput = modal.querySelector("#new-account-pass");

    modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());
    modal.querySelector(".modal-confirm").addEventListener("click", async () => {
      const user = userInput.value.trim();
      const pass = passInput.value.trim();

      if (!user || !pass) {
        alert("Username and password are required");
        return;
      }

      try {
        const r = await fetch(`${API}/subtitle-providers/${providerId}/accounts`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({user, pass})
        });
        const data = await r.json();

        if (r.ok) {
          modal.remove();
          // Refresh the section
          renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
        } else {
          alert(data.error || "Failed to add account");
        }
      } catch (e) {
        alert("Failed to add account: " + e.message);
      }
    });

    // Close on overlay click
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.remove();
    });

    userInput.focus();
  }

  async function removeProviderAccount(providerId, username) {
    if (!confirm(`Remove account "${username}"?`)) return;

    try {
      const r = await fetch(`${API}/subtitle-providers/${providerId}/accounts/${encodeURIComponent(username)}`, {
        method: "DELETE"
      });
      const data = await r.json();

      if (r.ok) {
        // Refresh the section
        renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
      } else {
        alert(data.error || "Failed to remove account");
      }
    } catch (e) {
      alert("Failed to remove account: " + e.message);
    }
  }

  async function toggleProvider(providerId, enabled) {
    try {
      const r = await fetch(`${API}/subtitle-providers/${providerId}/toggle`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled})
      });
      const data = await r.json();

      if (r.ok) {
        // Refresh the section
        renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
      } else {
        alert(data.error || "Failed to toggle provider");
      }
    } catch (e) {
      alert("Failed to toggle provider: " + e.message);
    }
  }

  function fmtRes(res) {
    if (!res) return "-";
    const map = {"3840x2160":"4K","2560x1440":"1440p","1920x1080":"1080p","1280x720":"720p","720x480":"480p","640x480":"480p"};
    if (map[res]) return map[res];
    const m = res.match(/\d+x(\d+)/);
    return m ? m[1] + "p" : res;
  }

  function escapeHtml(val) {
    if (val === null || val === undefined) return "";
    const str = String(val);
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function updateSettingsUI() {
    // Update modified indicators for visible fields
    $$(".setting-field[data-key]").forEach(fieldEl => {
      const key = fieldEl.dataset.key;
      const isModified = settingsModified[key] !== settingsOriginal[key];
      fieldEl.classList.toggle("modified", isModified);
    });

    // Update status text
    const modifiedCount = Object.keys(settingsModified).filter(k => settingsModified[k] !== settingsOriginal[k]).length;
    const status = $("#settings-status");
    if (modifiedCount > 0) {
      status.textContent = `${modifiedCount} unsaved`;
      status.className = "settings-status";
    } else {
      status.textContent = "";
    }

    // Update active preset indicator
    if (currentSection === "encoding") detectActivePreset();
  }

  async function saveSettings() {
    const btn = $("#save-settings");
    const status = $("#settings-status");

    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
      const r = await fetch(`${API}/settings`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(settingsModified)
      });
      const data = await r.json();

      if (data.status === "ok" || data.status === "partial") {
        for (const key of data.updated) {
          settingsOriginal[key] = settingsModified[key];
        }
        status.textContent = "Saved!";
        status.className = "settings-status success";
        updateSettingsUI();
        updateStatus();

        setTimeout(() => {
          if (status.classList.contains("success")) {
            status.textContent = "";
            status.className = "settings-status";
          }
        }, 3000);
      } else {
        status.textContent = "Failed to save";
        status.className = "settings-status error";
      }
    } catch (e) {
      status.textContent = "Error saving";
      status.className = "settings-status error";
    } finally {
      btn.disabled = false;
      btn.textContent = "Save";
    }
  }

  $("#save-settings").addEventListener("click", saveSettings);

  // ----- Start/Stop -----
  btnStart.addEventListener("click", async () => { btnStart.disabled = true; try{ await fetch(`${API}/start`, {method:"POST"});}catch{} updateStatus();});
  btnStop .addEventListener("click", async () => { btnStop .disabled = true; try{ await fetch(`${API}/stop`,  {method:"POST"});}catch{} updateStatus();});

  // ----- Poll scanning status -----
  async function pollScanStatus() {
    // Only poll if we're actively scanning
    if (moviesScanning || tvScanning) {
      if (moviesScanning) loadMovies(false);
      if (tvScanning) loadTV(false);
    }
  }

  // ----- Poll processing items -----
  async function pollProcessing() {
    // Refresh media tables to update processing progress
    if (hasProcessingItems) {
      hasProcessingItems = false; // Reset, will be set by render if still processing
      loadMovies(false);
      loadTV(false);
    }
  }

  // ----- System Stats -----
  let statsData = null;
  let storageHistory = null;
  let statsViewActive = false;

  async function updateSystemStats() {
    if (!statsViewActive) return;
    try {
      const r = await fetch(`${API}/system/stats`, {cache:"no-store"});
      if (!r.ok) return;
      statsData = await r.json();
      renderStatGauges();
      renderLineChart("cpu-chart", statsData.history.timestamps, statsData.history.cpu, {
        color: "var(--accent)", maxY: 100, suffix: "%", label: "CPU"
      });
      renderLineChart("ram-chart", statsData.history.timestamps, statsData.history.ram, {
        color: "var(--accent-2)", maxY: 100, suffix: "%", label: "RAM"
      });
      renderDiskMiniChart();
      // Auto-update modal chart if open
      if (_chartModalOpen && (_chartModalOpen.type === "cpu" || _chartModalOpen.type === "ram")) {
        renderModalChart(_chartModalOpen.type, _chartModalOpen.range);
      }
    } catch {}
  }

  async function loadStorageHistory() {
    if (!statsViewActive) return;
    try {
      const r = await fetch(`${API}/system/stats/storage`, {cache:"no-store"});
      if (!r.ok) return;
      const d = await r.json();
      storageHistory = d.history || [];
      renderStorageChart();
    } catch {}
  }

  function renderStatGauges() {
    if (!statsData) return;
    const c = statsData.current;
    const cpuEl = $("#cpu-live");
    const ramEl = $("#ram-live");
    const diskEl = $("#disk-live");
    if (cpuEl) cpuEl.textContent = `${Math.round(c.cpu_percent)}%`;
    if (ramEl) ramEl.textContent = `${Math.round(c.ram_percent)}%`;
    if (diskEl && c.disk) {
      const used = (c.disk.used / 1e12).toFixed(2);
      const total = (c.disk.total / 1e12).toFixed(2);
      // Use GB if < 1 TB
      if (c.disk.total < 1e12) {
        const usedG = (c.disk.used / 1e9).toFixed(1);
        const totalG = (c.disk.total / 1e9).toFixed(1);
        diskEl.textContent = `${usedG} / ${totalG} GB`;
      } else {
        diskEl.textContent = `${used} / ${total} TB`;
      }
    }
  }

  function renderDiskMiniChart() {
    if (!statsData || !statsData.current.disk) return;
    const d = statsData.current.disk;
    const pct = d.percent;
    const container = $("#disk-chart");
    if (!container) return;
    // Simple usage bar for disk
    container.innerHTML = `
      <div style="display:flex;flex-direction:column;justify-content:center;height:100%;gap:8px">
        <div style="font-size:13px;color:var(--muted)">${Math.round(pct)}% used</div>
        <div style="background:var(--bg-soft);border-radius:6px;height:24px;overflow:hidden">
          <div style="height:100%;width:${pct}%;background:${pct > 90 ? 'var(--danger)' : 'var(--ok)'};border-radius:6px;transition:width 0.5s"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">
          <span>Free: ${_fmtBytes(d.free)}</span>
          <span>Total: ${_fmtBytes(d.total)}</span>
        </div>
      </div>`;
  }

  function _fmtBytes(b) {
    if (b >= 1e12) return (b / 1e12).toFixed(2) + " TB";
    if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
    return b + " B";
  }

  function renderLineChart(containerId, timestamps, values, opts) {
    const container = $(`#${containerId}`);
    if (!container || !timestamps || timestamps.length < 2) {
      if (container && (!timestamps || timestamps.length < 2)) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Collecting data...</div>`;
      }
      return;
    }

    const W = opts.W || 600, H = opts.H || 160, PAD_L = 36, PAD_R = 10, PAD_T = 10, PAD_B = 24;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const maxY = opts.maxY || Math.max(...values, 1);

    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxY) * plotH; }

    // Grid lines
    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const gy = y(pct * maxY / 100);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${pct}${opts.suffix || ""}</text>`;
    }

    // Data polyline
    const points = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    // Area polygon
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(values.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    // Time labels (show ~4-6 labels)
    let timeLabels = "";
    const labelCount = Math.min(6, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
      timeLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:100%">
        ${gridLines}
        <polygon points="${areaPoints}" fill="${opts.color}" class="chart-area"/>
        <polyline points="${points}" stroke="${opts.color}" class="chart-line"/>
        ${timeLabels}
      </svg>`;

    // Tooltip on hover
    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }

    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      // Find nearest point
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const timeStr = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}:${d.getSeconds().toString().padStart(2, "0")}`;
      tooltip.textContent = `${opts.label}: ${values[closest].toFixed(1)}${opts.suffix || ""} at ${timeStr}`;
      tooltip.style.display = "block";
      // Position tooltip near cursor
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 160)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });

    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  function renderStorageChart() {
    const container = $("#storage-chart");
    if (!container || !storageHistory || storageHistory.length < 2) {
      if (container && (!storageHistory || storageHistory.length < 2)) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">No storage history yet</div>`;
      }
      return;
    }

    // Filter by range
    const rangeEl = $("#storage-range");
    const range = rangeEl ? rangeEl.value : "30d";
    let data = storageHistory;
    if (range !== "all") {
      const days = parseInt(range) || 30;
      const cutoff = Date.now() / 1000 - days * 86400;
      data = storageHistory.filter(r => r.recorded_at >= cutoff);
    }
    if (data.length < 2) {
      container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Not enough data for this range</div>`;
      return;
    }

    const W = 900, H = 200, PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    const timestamps = data.map(r => r.recorded_at);
    const usedVals = data.map(r => r.used_bytes);
    const maxTotal = Math.max(...data.map(r => r.total_bytes), 1);
    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxTotal) * plotH; }

    // Grid lines
    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const val = pct / 100 * maxTotal;
      const gy = y(val);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${_fmtBytes(val)}</text>`;
    }

    // Data polyline
    const points = usedVals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(usedVals.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    // Date labels
    let dateLabels = "";
    const labelCount = Math.min(8, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getMonth() + 1}/${d.getDate()}`;
      dateLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:100%">
        ${gridLines}
        <polygon points="${areaPoints}" fill="var(--ok)" class="chart-area"/>
        <polyline points="${points}" stroke="var(--ok)" class="chart-line"/>
        ${dateLabels}
      </svg>`;

    // Tooltip
    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }

    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const dateStr = d.toLocaleDateString() + " " + d.toLocaleTimeString();
      tooltip.textContent = `Used: ${_fmtBytes(usedVals[closest])} / ${_fmtBytes(data[closest].total_bytes)} — ${dateStr}`;
      tooltip.style.display = "block";
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 240)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });

    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  // ----- Chart Modal -----
  let _chartModalOpen = null; // null or { type, range }

  function showChartModal(type) {
    // Remove existing modal if any
    const existing = $(".modal-overlay.chart-modal-overlay");
    if (existing) existing.remove();

    const isCpuRam = (type === "cpu" || type === "ram");
    const title = type === "cpu" ? "CPU Usage" : type === "ram" ? "Memory Usage" : "Storage Over Time";
    const ranges = isCpuRam
      ? [{ label: "1h", sec: 3600 }, { label: "2h", sec: 7200 }, { label: "6h", sec: 21600 }, { label: "12h", sec: 43200 }, { label: "24h", sec: 86400 }]
      : [{ label: "7d", sec: 604800 }, { label: "30d", sec: 2592000 }, { label: "90d", sec: 7776000 }, { label: "All", sec: 0 }];
    const defaultSec = isCpuRam ? 21600 : 2592000;

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay chart-modal-overlay";
    overlay.innerHTML = `
      <div class="chart-modal">
        <div class="chart-modal-header">
          <h3>${title}</h3>
          <button class="modal-close chart-modal-close">&times;</button>
        </div>
        <div class="chart-modal-body">
          <div class="toggle-group chart-range-group">
            ${ranges.map(r => `<button class="toggle-btn${r.sec === defaultSec ? ' active' : ''}" data-sec="${r.sec}">${r.label}</button>`).join("")}
          </div>
          <div class="chart-modal-chart" id="chart-modal-canvas"></div>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    _chartModalOpen = { type, range: defaultSec };

    // Render initial chart
    renderModalChart(type, defaultSec);

    // Range button clicks
    overlay.querySelectorAll(".chart-range-group .toggle-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        overlay.querySelectorAll(".chart-range-group .toggle-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        const sec = parseInt(btn.dataset.sec, 10);
        _chartModalOpen = { type, range: sec };
        renderModalChart(type, sec);
      });
    });

    // Close handlers
    function closeModal() {
      _chartModalOpen = null;
      overlay.remove();
    }
    overlay.querySelector(".chart-modal-close").addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    const escHandler = (e) => { if (e.key === "Escape") { closeModal(); document.removeEventListener("keydown", escHandler); } };
    document.addEventListener("keydown", escHandler);
  }

  function renderModalChart(type, rangeSec) {
    const container = $("#chart-modal-canvas");
    if (!container) return;

    if (type === "cpu" || type === "ram") {
      if (!statsData || !statsData.history) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Collecting data...</div>`;
        return;
      }
      const allTs = statsData.history.timestamps;
      const allVals = type === "cpu" ? statsData.history.cpu : statsData.history.ram;
      const color = type === "cpu" ? "var(--accent)" : "var(--accent-2)";
      const label = type === "cpu" ? "CPU" : "RAM";

      // Filter by range
      let ts, vals;
      if (rangeSec > 0) {
        const cutoff = Date.now() / 1000 - rangeSec;
        const startIdx = allTs.findIndex(t => t >= cutoff);
        if (startIdx < 0 || startIdx >= allTs.length - 1) {
          ts = allTs; vals = allVals; // show all if range exceeds data
        } else {
          ts = allTs.slice(startIdx); vals = allVals.slice(startIdx);
        }
      } else {
        ts = allTs; vals = allVals;
      }

      renderLineChart("chart-modal-canvas", ts, vals, {
        color, maxY: 100, suffix: "%", label, W: 900, H: 380
      });
    } else {
      // Storage
      if (!storageHistory || storageHistory.length < 2) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">No storage history yet</div>`;
        return;
      }
      let data = storageHistory;
      if (rangeSec > 0) {
        const cutoff = Date.now() / 1000 - rangeSec;
        data = storageHistory.filter(r => r.recorded_at >= cutoff);
      }
      if (data.length < 2) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Not enough data for this range</div>`;
        return;
      }
      renderStorageChartTo("chart-modal-canvas", data, 900, 380);
    }
  }

  function renderStorageChartTo(containerId, data, W, H) {
    const container = $(`#${containerId}`);
    if (!container) return;
    const PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const timestamps = data.map(r => r.recorded_at);
    const usedVals = data.map(r => r.used_bytes);
    const maxTotal = Math.max(...data.map(r => r.total_bytes), 1);
    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxTotal) * plotH; }

    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const val = pct / 100 * maxTotal;
      const gy = y(val);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${_fmtBytes(val)}</text>`;
    }

    const points = usedVals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(usedVals.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    let dateLabels = "";
    const labelCount = Math.min(8, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getMonth() + 1}/${d.getDate()}`;
      dateLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:100%">
        ${gridLines}
        <polygon points="${areaPoints}" fill="var(--ok)" class="chart-area"/>
        <polyline points="${points}" stroke="var(--ok)" class="chart-line"/>
        ${dateLabels}
      </svg>`;

    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const dateStr = d.toLocaleDateString() + " " + d.toLocaleTimeString();
      tooltip.textContent = `Used: ${_fmtBytes(usedVals[closest])} / ${_fmtBytes(data[closest].total_bytes)} — ${dateStr}`;
      tooltip.style.display = "block";
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 240)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });
    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  // Watch for stats view becoming visible
  const _origNavClick = null;
  // Detect view changes to start/stop stats polling
  function checkStatsView() {
    const statsView = $("#view-stats");
    const isVisible = statsView && statsView.classList.contains("visible");
    if (isVisible && !statsViewActive) {
      statsViewActive = true;
      updateSystemStats();
      loadStorageHistory();
    } else if (!isVisible) {
      statsViewActive = false;
    }
  }

  // Hook into nav clicks to detect stats view
  $$(".nav-item").forEach(b => {
    b.addEventListener("click", () => setTimeout(checkStatsView, 50));
  });

  // Wire storage range selector
  const storageRange = $("#storage-range");
  if (storageRange) {
    storageRange.addEventListener("change", () => renderStorageChart());
  }

  // Clickable stat cards → chart modal
  const statCards = $$(".stat-card");
  const cardTypes = ["cpu", "ram", "disk"];
  statCards.forEach((card, i) => {
    if (cardTypes[i] === "disk") return; // disk card has no modal
    card.addEventListener("click", () => showChartModal(cardTypes[i]));
  });

  // Clickable storage panel header → storage chart modal
  const storagePanelHead = document.querySelector("#storage-panel .panel-head");
  if (storagePanelHead) {
    storagePanelHead.addEventListener("click", (e) => {
      // Don't open modal if clicking on the range dropdown
      if (e.target.closest("select")) return;
      showChartModal("storage");
    });
  }

  // ----- Kickoff -----
  updateStatus();
  updateWorkerStatus();
  loadMovies(); loadTV();
  loadSettings();
  pollLogs();
  setInterval(updateStatus, 2000);
  setInterval(updateWorkerStatus, 2000);  // Update worker status every 2s
  setInterval(pollLogs, 1500);
  setInterval(pollScanStatus, 2000);  // Check scan status every 2s
  setInterval(pollProcessing, 3000);  // Update processing progress every 3s
  setInterval(updateSystemStats, 5000);    // System stats every 5s
  setInterval(loadStorageHistory, 300000); // Storage history every 5 min
})();