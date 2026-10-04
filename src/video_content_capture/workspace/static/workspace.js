"use strict";

const element = (id) => document.getElementById(id);
const controlled = (id) => encodeURIComponent(id);
const activeStatuses = new Set(["queued", "running"]);
const statusLabels = {
  queued: "排隊中", running: "處理中", completed: "已完成", failed: "失敗",
  cancelled: "已取消", interrupted: "已中斷",
};
// Status always pairs a text label with a glyph; color is never the only signal.
const statusIcons = {
  queued: "⏳", running: "⟳", completed: "✓", failed: "⚠",
  cancelled: "✕", interrupted: "⏸", info: "ⓘ",
};
const errorLabels = {
  preview_failed: "相容預覽製作失敗；字幕、問答與成品下載仍可使用，已保留來源，可手動重試。",
  preview_not_needed: "此影音可直接播放，不需要相容預覽。",
  cancelled: "工作已取消；已保留完成階段，可手動重試。",
  insufficient_space: "磁碟空間不足，請手動清理空間後重試；已完成資產會保留。",
  format_missing: "所選來源格式已消失，請重新查詢來源並選擇畫質與音軌。",
  format_unavailable: "所選來源格式無法取得，請重新查詢來源並選擇畫質與音軌。",
  media_failed: "影音下載、合併或驗證失敗，請檢查來源與本機媒體工具後重試。",
  source_unavailable: "來源不支援／無法取得，請確認影片非直播且無須登入，再重試。",
  container_confirmation_required: "MP4 封裝失敗；來源與字幕已保留。請在匯出區改選 MKV，重新確認摘要並建立匯出工作。",
  source_language_unknown: "原語系未知；請從平台字幕選單選擇人工或自動字幕，再取得所選字幕。",
  subtitle_unavailable: "字幕已消失或無法取得，請重新查詢並選擇平台字幕；不會回退辨識。",
  subtitle_acquisition_failed: "字幕清單或取得失敗，請手動重試；未將失敗當作無字幕。",
  translation_key_invalid: "Gemini 金鑰無效，請檢查 .env 的 GEMINI_API_KEY 並重啟服務。",
  translation_permission_denied: "Gemini 權限不足（403），請確認帳戶與模型權限。",
  translation_model_missing: "翻譯模型不存在（404），請檢查 VCC_GEMINI_TRANSLATION_MODEL。",
  translation_rate_limited: "Gemini 超過配額（429），已完成塊保留；請依等待時間手動重試。",
  translation_provider_unavailable: "Gemini 服務暫時不可用（5xx），已完成塊保留；請稍後手動重試。",
  translation_provider_request_failed: "Gemini 請求無效，請檢查模型設定。",
  unsupported_language: "字幕語系無法封裝，請確認有效的語系代碼。",
  translation_ids: "翻譯 cue ID 不完整或重複，未發佈；請手動重試。",
  translation_json: "翻譯回應格式不完整，未發佈；請手動重試。",
  translation_finish: "翻譯遭截斷或安全阻擋，未發佈；請檢查來源與模型後手動重試。",
  translation_empty: "翻譯含空文字，未發佈；請手動重試。",
  translation_failed: "翻譯失敗，已完成塊保留；請檢查設定並手動重試。",
  download_failed: "下載失敗，請確認網路及公開來源可用後手動重試。",
};
const stageLabels = {
  translation: "翻譯字幕", exporting: "封裝成品", subtitles: "取得字幕",
  queued: "等待開始", checking: "檢查來源與可用空間",
  downloading_video: "下載影像", downloading_audio: "下載音軌",
  merging: "合併影音", verifying: "驗證媒體",
  completed: "處理完成", failed: "處理失敗",
  download: "下載影音", merge: "合併影音", verify: "驗證媒體", publish: "發佈媒體",
  preview: "製作相容預覽", previewing: "製作相容預覽", qa: "影片問答", export: "匯出影片",
};
let currentVideo = null;
let jobs = [];
let opening = 0;
let savingPosition = false;
let lastPosition = -1;
let activeAsset = null;
let playbackVideoId = null;
let geminiConfigured = false;
let exportSnapshot = null;
let exportGeneration = 0;
let currentConversation = null;
let qaGeneration = 0;
let qaSubmitting = false;
let qaMessageSignature = "";
let libraryVideos = [];
let queryController = null;

async function request(path, method = "GET", body, signal) {
  const options = { method, credentials: "same-origin", signal };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    // Only locally authored labels are displayed, never raw exception details.
    const message = path === "/api/query"
      ? "來源不支援／無法取得。請確認網址是公開的單支隨選影片，且無須登入。"
      : `操作未完成（${response.status}），請確認本機服務與來源，或稍後重試。`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function statusNode(status, text) {
  const node = document.createElement("span");
  const icon = document.createElement("span");
  icon.className = "icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = statusIcons[status] || "•";
  node.append(icon, ` ${text}`);
  return node;
}

function snapshotOf(job) {
  try { return JSON.parse(job.snapshot || "{}") || {}; } catch { return {}; }
}

async function loadStatus() {
  try {
    const status = await request("/api/status");
    element("service-status").textContent = `服務 ${status.version} · Gemini ${
      status.gemini.configured ? "已設定" : "未設定"
    } · 翻譯：${status.gemini.translation_model} · 問答：${status.gemini.qa_model}`;
    geminiConfigured = status.gemini.configured;
    element("gemini-help").hidden = geminiConfigured;
    updateTranslationButtons();
    updateQaControls();
  } catch {
    element("service-status").textContent = "無法讀取服務狀態，請確認本機服務仍在執行。";
  }
}

async function loadVideos() {
  libraryVideos = await request("/api/videos");
  renderLibrary();
  await loadPendingDeletions();
}

function renderLibrary() {
  const query = element("library-search").value.trim().toLocaleLowerCase();
  const visible = libraryVideos.filter((video) => !query || video.title.toLocaleLowerCase().includes(query));
  element("library-empty").hidden = libraryVideos.length > 0;
  element("library-status").textContent = !libraryVideos.length
    ? "尚無影片。"
    : query ? `符合「${element("library-search").value.trim()}」的影片 ${visible.length} 支（共 ${libraryVideos.length} 支）`
      : `已保存 ${libraryVideos.length} 支影片`;
  element("video-list").replaceChildren();
  for (const video of visible) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    const title = document.createElement("span");
    title.textContent = video.title;
    const meta = document.createElement("span");
    meta.className = "hint";
    meta.textContent = `${video.duration == null ? "時長未知" : `${Math.round(video.duration)} 秒`} · 位置 ${Math.round(video.position || 0)} 秒`;
    button.append(title, meta);
    button.setAttribute("aria-current", String(currentVideo?.id === video.id));
    button.addEventListener("click", () => openVideo(video.id).catch(showError));
    item.append(button);
    element("video-list").append(item);
  }
}

async function loadPendingDeletions() {
  const pending = await request("/api/deletions");
  element("pending-section").hidden = !pending.length;
  element("pending-deletions").replaceChildren();
  for (const video of pending) {
    const item = document.createElement("li");
    item.append(statusNode("failed", `待清理：${video.title} `));
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "重試刪除";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await request(`/api/deletions/${controlled(video.id)}/retry`, "POST");
        element("library-status").textContent = "已完成待清理項目的刪除。";
        await loadVideos();
      } catch (error) {
        element("library-status").textContent = deletionErrorLabel(error);
        button.disabled = false;
      }
    });
    item.append(button);
    element("pending-deletions").append(item);
  }
}

function deletionErrorLabel(error) {
  if (error.status === 409) return "仍有工作正在停止或刪除範圍已變更；影片若已標記待清理，請稍後在「待清理」按重試刪除。";
  if (error.status === 500) return "刪除未完成；影片已標記待清理，請稍後按重試刪除。";
  return error.message;
}

function jobErrorLabel(code) {
  const [base, wait] = code.split(";");
  const label = errorLabels[base] || "處理失敗，已保存完成階段；請檢查來源與設定後手動重試。";
  return label + (wait?.startsWith("retry_after=") ? ` 建議等待：${wait.slice(12)}` : "");
}

function showError(error) {
  element("import-status").textContent = error.message;
}

function options(select, entries, selected, label) {
  select.replaceChildren();
  for (const entry of entries) {
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = label(entry);
    option.selected = entry.id === selected;
    select.append(option);
  }
}

function renderVideo(video, preserveSelection = false) {
  const sameVideo = currentVideo?.id === video.id;
  const selectedFormat = preserveSelection && sameVideo ? element("format").value : null;
  const selectedAudio = preserveSelection && sameVideo ? element("audio").value : null;
  currentVideo = video;
  if (!sameVideo) { element("include-original").checked = false; element("export-container").value = ""; element("subtitle-language").value = "zh-TW"; }
  element("video-details").hidden = false;
  element("video-title").textContent = video.title;
  element("video-source").textContent = `時長：${video.duration == null ? "未知" : `${Math.round(video.duration)} 秒`} · YouTube`;
  const metadata = video.metadata || {};
  options(element("format"), metadata.formats || [], selectedFormat || metadata.default_format_id,
    (format) => `${format.height}p · ${format.fps || "未知"} FPS · ${format.codec} · ${
      format.size == null ? "大小未知" : `約 ${(format.size / 1048576).toFixed(1)} MB`
    }（格式 ${format.id}）`);
  options(element("audio"), metadata.audio_tracks || [], selectedAudio || metadata.default_audio_id,
    (audio) => `${audio.language || "未知"} · ${audio.codec} · ${audio.original ? "原音" : "原音未知／其他音軌"}${audio.default ? " · 預設軌" : ""}`);
  element("quality-help").textContent = metadata.above_1080p
    ? "來源全部高於 1080p，預選最低畫質。大小為估算，實際磁碟需求可能不同。"
    : "預選不高於 1080p 的最高畫質。大小為估算，實際磁碟需求可能不同。";
  element("audio-help").textContent = "原音無法判定時標示未知；開始後固定所選格式與音軌，來源消失須重新選擇。";
  const assets = video.assets || [];
  const preferredAsset = assets.find((asset) => asset.format_id === element("format").value &&
    asset.audio_id === element("audio").value) || assets.find((asset) => asset.id === activeAsset) || assets[0];
  options(element("media-asset"), assets, preferredAsset?.id,
    (asset) => `格式 ${asset.format_id} · 音軌 ${asset.audio_id} · ${asset.browser_playable ? "可播放" : "需要相容預覽"}`);
  element("media-asset").disabled = !assets.length;
  renderPlayback(video);
  renderLibrary();
  renderSubtitles(video, preserveSelection && sameVideo);
  updateStartButton();
  loadConversations(video.id).catch(showQaError);
}

function stopPlayer(message) {
  const player = element("player");
  playbackVideoId = null;
  player.hidden = true;
  player.pause();
  player.removeAttribute("src");
  player.load();
  activeAsset = null;
  element("player-placeholder").hidden = false;
  element("playback-help").textContent = message;
}

function playbackSource(video) {
  // Prefer the directly playable source; otherwise use its separate compatible preview.
  const asset = (video.assets || []).find((entry) => entry.id === element("media-asset").value);
  if (!asset) return { asset: null, key: null, url: null };
  if (asset.browser_playable) return { asset, key: `asset:${asset.id}`, url: `/api/assets/${controlled(asset.id)}/media` };
  const preview = (video.previews || []).find((entry) => entry.asset_id === asset.id);
  if (preview) return { asset, preview, key: `preview:${preview.id}`, url: `/api/previews/${controlled(preview.id)}/media` };
  return { asset, key: null, url: null };
}

function renderPlayback(video) {
  const source = playbackSource(video);
  const player = element("player");
  renderPreviewPanel(video);
  if (!source.url) {
    stopPlayer(source.asset
      ? "此影音無法由瀏覽器直接播放；可製作相容預覽。字幕、問答與成品下載仍可使用。"
      : "影音尚未就緒，請選擇畫質與音軌並開始處理。");
    return;
  }
  player.hidden = false;
  element("player-placeholder").hidden = true;
  if (activeAsset !== source.key) {
    playbackVideoId = null;
    activeAsset = source.key;
    lastPosition = -1;
    player.src = source.url;
    player.onloadedmetadata = () => {
      playbackVideoId = video.id;
      player.currentTime = Math.min(video.position || 0, Number.isFinite(player.duration) ? player.duration : Infinity);
    };
    player.onerror = () => {
      if (currentVideo?.id !== video.id) return;
      stopPlayer("瀏覽器無法播放此影音。字幕、問答與成品下載仍可使用；引用會顯示原文與時間，但目前不能跳播。");
    };
  }
}

function previewJobs(video, assetId) {
  return jobs.filter((job) => job.video_id === video.id && job.kind === "preview" && snapshotOf(job).asset_id === assetId);
}

function renderPreviewPanel(video) {
  const source = playbackSource(video);
  const panel = element("preview-panel");
  const button = element("make-preview");
  const anyPreviewWork = (video.previews || []).length > 0 ||
    jobs.some((job) => job.video_id === video.id && job.kind === "preview" && activeStatuses.has(job.status));
  element("clear-preview").disabled = !anyPreviewWork;
  if (!source.asset || source.asset.browser_playable) { panel.hidden = true; return; }
  panel.hidden = false;
  const related = previewJobs(video, source.asset.id);
  const latest = related[related.length - 1];
  let message;
  button.hidden = false;
  button.disabled = false;
  button.textContent = related.some((job) => job.status === "completed") ? "重新製作預覽" : "製作相容預覽";
  if (source.preview) {
    message = `正在播放相容預覽（${source.preview.height}p H.264／AAC）。下載成品仍保留所選來源畫質。`;
    button.hidden = true;
  } else if (latest && activeStatuses.has(latest.status)) {
    message = `${statusLabels[latest.status]}：正在製作相容預覽（最高 720p），進度總量未知；可在處理工作中取消。`;
    button.disabled = true;
  } else if (latest && ["failed", "interrupted", "cancelled"].includes(latest.status)) {
    message = `相容預覽製作失敗或未完成（${statusLabels[latest.status]}）。字幕、問答與成品下載仍可使用；引用會顯示原文與時間，但目前不能跳播。`;
    if (latest.error_code) message += ` ${jobErrorLabel(latest.error_code)}`;
  } else {
    message = "此影音無法由瀏覽器直接播放。可製作最高 720p H.264／AAC 相容預覽（需額外本機處理與儲存，不覆寫來源或成品）。";
  }
  const status = latest?.status && !source.preview ? latest.status : source.preview ? "completed" : "info";
  const node = statusNode(status, message);
  if (element("preview-status").textContent !== node.textContent) element("preview-status").replaceChildren(node);
}

async function openVideo(id) {
  const generation = ++opening;
  await savePosition(true);
  const video = await request(`/api/videos/${controlled(id)}`);
  if (generation !== opening) return;
  renderVideo(video);
  element("import-status").textContent = "已開啟影片；開啟不會自動建立下載工作。";
  renderJobs();
}

function updateStartButton() {
  element("start-job").disabled = !currentVideo || !element("format").value ||
    !element("audio").value || jobs.some((job) => job.video_id === currentVideo.id && activeStatuses.has(job.status));
}

function renderJobs() {
  const visibleJobs = currentVideo ? jobs.filter((job) => job.video_id === currentVideo.id) : jobs;
  element("job-list").replaceChildren();
  const active = visibleJobs.find((job) => activeStatuses.has(job.status));
  const progress = element("job-progress");
  progress.hidden = !active;
  if (active?.progress == null) progress.removeAttribute("value");
  else progress.value = active.progress;
  const announcement = active
    ? `${statusLabels[active.status]} · ${stageLabels[active.kind] || active.kind} · ${stageLabels[active.stage] || active.stage || "準備中"}${active.progress == null ? " · 進度總量未知" : ` · ${Math.round(active.progress * 100)}%`}`
    : visibleJobs.length ? "目前沒有執行中的工作" : "尚無處理工作";
  // Only replace the live region when the text changes, so polling does not re-announce.
  const statusText = `${active ? statusIcons[active.status] : statusIcons.info} ${announcement}`;
  if (element("job-status").textContent !== statusText) {
    element("job-status").replaceChildren(statusNode(active ? active.status : "info", announcement));
  }
  for (const job of visibleJobs) {
    const item = document.createElement("li");
    const summary = statusNode(job.status, `${statusLabels[job.status] || job.status} · ${stageLabels[job.kind] || job.kind} · ${stageLabels[job.stage] || job.stage || "準備中"}${job.error_code ? ` · ${jobErrorLabel(job.error_code)}` : ""} `);
    item.append(summary);
    const action = activeStatuses.has(job.status) ? "/cancel" :
      ["failed", "cancelled", "interrupted"].includes(job.status) && job.error_code !== "container_confirmation_required" ? "/retry" : null;
    if (action) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action === "/cancel" ? "取消" : "重試";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await request(`/api/jobs/${controlled(job.id)}${action}`, "POST");
          await pollJobs();
        } catch (error) { showError(error); button.disabled = false; }
      });
      item.append(button);
    }
    element("job-list").append(item);
  }
  updateStartButton();
  if (currentVideo) renderPreviewPanel(currentVideo);
}

async function pollJobs() {
  const previous = jobs;
  jobs = await request("/api/jobs");
  renderJobs();
  if (currentVideo && jobs.some((job) => job.video_id === currentVideo.id && (["completed", "failed", "cancelled", "interrupted"].includes(job.status) || job.kind === "translation") &&
    !previous.some((old) => old.id === job.id && old.status === job.status))) {
    const id = currentVideo.id;
    const video = await request(`/api/videos/${controlled(id)}`);
    if (currentVideo?.id === id) renderVideo(video, true);
  }
}

async function savePosition(force = false) {
  const player = element("player");
  if (savingPosition || !playbackVideoId || player.hidden || !Number.isFinite(player.currentTime)) return;
  const position = player.currentTime;
  if (!force && Math.abs(position - lastPosition) < 5) return;
  const id = playbackVideoId;
  savingPosition = true;
  try {
    await request(`/api/videos/${controlled(id)}/position`, "PATCH", { position });
    if (playbackVideoId === id) lastPosition = position;
  } catch {
    element("playback-help").textContent = "播放位置未保存，請確認本機服務仍在執行。";
  } finally { savingPosition = false; }
}

element("media-asset").addEventListener("change", async () => {
  if (!currentVideo) return;
  const video = currentVideo;
  if (!element("player").hidden) {
    video.position = element("player").currentTime;
    await savePosition(true);
  }
  if (currentVideo?.id === video.id) { renderPlayback(video); invalidateExport(); }
});
const positionEvents = ["timeupdate", "pause", "seeked"];
for (const event of positionEvents) {
  element("player").addEventListener(event, () => savePosition(event !== "timeupdate"));
}
document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(true); });
element("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = element("load-video");
  if (button.disabled) return;
  button.disabled = true;
  element("import-status").textContent = "正在查詢影片資料；尚未開始下載…";
  const generation = ++opening;
  queryController = new AbortController();
  element("cancel-query").hidden = false;
  try {
    const video = await request("/api/query", "POST", { url: element("youtube-url").value.trim() }, queryController.signal);
    if (generation !== opening) return;
    await savePosition(true);
    renderVideo(video);
    element("import-status").textContent = "影片已載入，請確認畫質與音軌後開始處理。";
    await loadVideos();
    await pollJobs();
  } catch (error) {
    if (error.name === "AbortError") {
      element("import-status").textContent = "已取消查詢；不會建立下載工作。若服務已完成查詢，影片會出現在影片庫。";
    } else showError(error);
  } finally {
    button.disabled = false;
    element("cancel-query").hidden = true;
    queryController = null;
  }
});
element("cancel-query").addEventListener("click", () => queryController?.abort());
element("library-search").addEventListener("input", renderLibrary);
element("make-preview").addEventListener("click", async () => {
  const video = currentVideo;
  const assetId = element("media-asset").value;
  if (!video || !assetId || element("make-preview").disabled) return;
  element("make-preview").disabled = true;
  try {
    await request(`/api/videos/${controlled(video.id)}/previews`, "POST", { asset_id: assetId });
    await pollJobs();
  } catch (error) { showError(error); }
  finally { if (currentVideo?.id === video.id) renderPreviewPanel(currentVideo); }
});
element("clear-preview").addEventListener("click", async () => {
  const video = currentVideo;
  if (!video || element("clear-preview").disabled) return;
  element("clear-preview").disabled = true;
  try {
    const result = await request(`/api/videos/${controlled(video.id)}/previews`, "DELETE");
    await pollJobs();
    const fresh = await request(`/api/videos/${controlled(video.id)}`);
    if (currentVideo?.id !== video.id) return;
    renderVideo(fresh, true);
    element("import-status").textContent = `已清除 ${result.removed} 個相容預覽；字幕、對話、來源與成品均保留。需要時可按「重新製作預覽」。`;
  } catch (error) { showError(error); renderPreviewPanel(video); }
});
element("delete-video").addEventListener("click", () => openDeleteDialog().catch(showError));
element("delete-acknowledge").addEventListener("change", () => {
  element("delete-confirm").disabled = !element("delete-acknowledge").checked || !deletionScope;
});
element("delete-cancel").addEventListener("click", () => WorkspaceDialog.close(element("delete-dialog")));
element("delete-confirm").addEventListener("click", () => confirmDeletion().catch(showError));
element("refresh-source").addEventListener("click", async () => {
  if (!currentVideo || element("refresh-source").disabled) return;
  const id = currentVideo.id;
  const url = currentVideo.source_url || currentVideo.metadata?.source_url;
  if (!url) { showError(new Error("此項目沒有來源網址，請從上方重新載入。")); return; }
  const button = element("refresh-source");
  button.disabled = true;
  element("import-status").textContent = "正在重新查詢來源格式；尚未開始下載…";
  try {
    const video = await request("/api/query", "POST", { url, refresh: true });
    if (currentVideo?.id !== id) return;
    renderVideo(video);
    element("import-status").textContent = "已更新來源，請重新選擇畫質與音軌後開始處理。";
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
});
element("processing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (element("start-job").disabled || !currentVideo) return;
  element("start-job").disabled = true;
  try {
    await request(`/api/videos/${controlled(currentVideo.id)}/jobs`, "POST", {
      format_id: element("format").value, audio_id: element("audio").value,
    });
    await pollJobs();
  } catch (error) { showError(error); updateStartButton(); }
});

async function refresh() {
  try { await pollJobs(); await pollConversation(); }
  catch { element("job-status").textContent = "工作狀態暫時無法更新，請確認本機服務仍在執行。"; }
  setTimeout(refresh, 1500);
}
loadStatus();
loadVideos().catch(showError);
refresh();

function invalidateExport() {
  exportSnapshot = null;
  exportGeneration += 1;
  element("export-start").disabled = true;
  element("export-summary").textContent = "選擇已變更，請重新確認格式與字幕軌。";
}

function updateTranslationButtons() {
  for (const id of ["translate", "retranslate"]) {
    element(id).disabled = !geminiConfigured || !currentVideo?.translation_source_version_id;
  }
}

function subtitleLabel(version) {
  const sources = { platform_manual: "平台人工", platform_auto: "平台自動", asr: "本機辨識", import: "匯入", translation: "翻譯" };
  return `${version.language} · ${sources[version.source_type] || version.source_type} · ${version.name} · ${version.complete ? "完整" : "未完成"} · ${version.id.slice(0, 8)}`;
}

function applyPlaybackSubtitle(video) {
  const player = element("player");
  for (const button of element("subtitle-list").querySelectorAll('button[data-selection="playback"]')) {
    button.setAttribute("aria-pressed", String((button.dataset.versionId || null) === video.playback_version_id));
  }
  for (const track of player.querySelectorAll("track")) track.remove();
  const version = (video.subtitles || []).find((item) => item.id === video.playback_version_id && item.complete);
  if (!version) return;
  const track = document.createElement("track");
  track.kind = "subtitles";
  track.srclang = version.language;
  track.label = subtitleLabel(version);
  track.src = `/api/subtitles/${controlled(version.id)}/track.vtt`;
  track.default = true;
  track.addEventListener("load", () => { track.track.mode = "showing"; });
  player.append(track);
}

async function chooseSubtitle(selection, versionId) {
  if (!currentVideo) return;
  const id = currentVideo.id;
  await request(`/api/videos/${controlled(id)}/subtitles/${selection}`, "POST", { version_id: versionId || null });
  if (currentVideo?.id !== id) return;
  const field = { playback: "playback_version_id", "translation-source": "translation_source_version_id", "export-selection": "export_version_id" }[selection];
  currentVideo[field] = versionId || null;
  if (selection === "playback") applyPlaybackSubtitle(currentVideo);
  else { invalidateExport(); updateTranslationButtons(); }
}

function renderSubtitles(video, preserveSelection = false) {
  const originalSelection = preserveSelection ? element("export-original").value : video.translation_source_version_id;
  invalidateExport();
  const versions = video.subtitles || [];
  const complete = versions.filter((version) => version.complete);
  const optional = [{ id: "", name: "未選擇" }, ...complete];
  for (const [id, selected] of [
    ["translation-source", video.translation_source_version_id],
    ["export-target", video.export_version_id],
    ["export-original", originalSelection],
    ["import-parent", ""],
  ]) options(element(id), optional, selected || "", (v) => v.id ? subtitleLabel(v) : v.name);
  element("subtitle-list").replaceChildren();
  const off = document.createElement("button");
  off.type = "button";
  off.textContent = "關閉播放字幕";
  off.addEventListener("click", () => chooseSubtitle("playback", null).catch(showError));
  off.dataset.selection = "playback";
  off.setAttribute("aria-pressed", String(!video.playback_version_id));
  const offItem = document.createElement("li");
  offItem.append(off);
  element("subtitle-list").append(offItem);
  for (const version of versions) {
    const item = document.createElement("li");
    const label = document.createElement("p");
    label.textContent = subtitleLabel(version);
    item.append(label);
    if (version.complete) {
      for (const [selection, text] of [["playback", "用於播放"], ["qa", "設為問答依據"], ["translation-source", "以此版本翻譯"], ["export-selection", "匯出選擇"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        button.dataset.selection = selection;
        button.dataset.versionId = version.id;
        button.addEventListener("click", async () => {
          try {
            if (selection === "qa") { await createConversation(version.id); return; }
            await chooseSubtitle(selection, version.id);
            const control = selection === "translation-source" ? "translation-source" : selection === "export-selection" ? "export-target" : null;
            if (control) element(control).value = version.id;
          } catch (error) { showError(error); }
        });
        item.append(button);
      }
      for (const format of ["srt", "vtt"]) {
        const link = document.createElement("a");
        link.href = `/api/subtitles/${controlled(version.id)}/download.${format}`;
        link.textContent = ` 下載 ${format.toUpperCase()} `;
        item.append(link);
      }
    }
    element("subtitle-list").append(item);
  }
  const languages = [...new Set(["zh-TW", "zh-CN", "en", "ja", "ko", "es", "fr", "de", ...versions.map((v) => v.language), ...(video.metadata?.subtitles || []).map((v) => v.language), ...[video.metadata?.original_language].filter(Boolean)])];
  const selected = element("subtitle-language").value || "zh-TW";
  options(element("subtitle-language"), languages.map((id) => ({ id })), selected, (v) => v.id);
  element("subtitle-language").disabled = false;
  element("include-original").disabled = false;
  const platform = (video.metadata?.subtitles || []).map((v) => ({ ...v, source_type: v.automatic ? "platform_auto" : "platform_manual", id: `${v.automatic ? "platform_auto" : "platform_manual"}:${v.language}` }));
  options(element("platform-subtitle"), platform, platform[0]?.id, (v) => `${v.language} · ${v.source_type}`);
  element("use-platform-subtitle").disabled = !platform.length || !video.assets?.length;
  element("acquire-subtitles").disabled = !video.assets?.length;
  applyPlaybackSubtitle(video);
  updateTranslationButtons();
  element("export-list").replaceChildren();
  for (const artifact of video.exports || []) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = `/api/exports/${controlled(artifact.id)}/download`;
    let tracks = "";
    try { tracks = JSON.parse(artifact.summary).tracks.map((track) => `${track.language} · ${track.name}`).join("；"); } catch { /* Older summaries remain downloadable. */ }
    link.textContent = `可下載 · ${artifact.container.toUpperCase()} · ${tracks} · ${artifact.id.slice(0, 8)}`;
    item.append(link);
    element("export-list").append(item);
  }
}

for (const [id, selection] of [["translation-source", "translation-source"], ["export-target", "export-selection"]]) {
  element(id).addEventListener("change", () => chooseSubtitle(selection, element(id).value).catch(showError));
}
for (const id of ["include-original", "export-original", "subtitle-language", "export-container"]) {
  element(id).addEventListener("change", invalidateExport);
}

element("subtitle-import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentVideo) return;
  const file = element("subtitle-file").files[0];
  if (!file || file.size > 10 * 1024 * 1024) { showError(new Error("請選擇最多 10 MiB 的字幕檔案。")); return; }
  const id = currentVideo.id;
  const params = new URLSearchParams({ language: element("import-language").value, name: element("version-name").value, format: file.name.toLowerCase().endsWith(".vtt") ? "vtt" : "srt" });
  if (element("import-parent").value) params.set("parent_id", element("import-parent").value);
  const button = element("import-subtitles");
  button.disabled = true;
  try {
    const response = await fetch(`/api/videos/${controlled(id)}/subtitles/import?${params}`, { method: "POST", credentials: "same-origin", body: file });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "字幕匯入失敗，請檢查格式與時間。");
    if (currentVideo?.id !== id) return;
    const versions = await request(`/api/videos/${controlled(id)}/subtitles`);
    if (currentVideo?.id !== id) return;
    currentVideo.subtitles = versions;
    renderSubtitles(currentVideo);
    await loadConversations(id);
    element("import-status").textContent = result.warnings?.length ? result.warnings.join("；") : "字幕已匯入；請明確選擇播放、翻譯來源或匯出用途。";
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
});

async function acquireSubtitles(platform = false) {
  if (!currentVideo || !element("media-asset").value) return;
  const body = { asset_id: element("media-asset").value };
  if (platform) {
    const [source_type, language] = element("platform-subtitle").value.split(":");
    Object.assign(body, { source_type, language });
  }
  await request(`/api/videos/${controlled(currentVideo.id)}/subtitles/acquire`, "POST", body);
  await pollJobs();
}
element("acquire-subtitles").addEventListener("click", () => acquireSubtitles().catch(showError));
element("use-platform-subtitle").addEventListener("click", () => acquireSubtitles(true).catch(showError));
for (const [id, regenerate] of [["translate", false], ["retranslate", true]]) {
  element(id).addEventListener("click", async () => {
    if (!currentVideo || element(id).disabled) return;
    element(id).disabled = true;
    try {
      await request(`/api/videos/${controlled(currentVideo.id)}/translations`, "POST", { language: element("subtitle-language").value, regenerate });
      await pollJobs();
    } catch (error) { showError(error); }
    finally { updateTranslationButtons(); }
  });
}
element("export-preview").addEventListener("click", async () => {
  if (!currentVideo) return;
  const id = currentVideo.id;
  const button = element("export-preview");
  const generation = ++exportGeneration;
  exportSnapshot = null;
  element("export-start").disabled = true;
  button.disabled = true;
  try {
    const snapshot = await request(`/api/videos/${controlled(id)}/exports/preview`, "POST", {
      asset_id: element("media-asset").value,
      target_version_ids: element("export-target").value ? [element("export-target").value] : [],
      include_original: element("include-original").checked,
      original_version_id: element("export-original").value || null,
      container: element("export-container").value || null,
    });
    if (currentVideo?.id !== id || generation !== exportGeneration) return;
    exportSnapshot = snapshot;
    element("export-summary").textContent = `${snapshot.container.toUpperCase()} · 字幕軌：${snapshot.tracks.map((track) => `${track.language} · ${track.name || track.version_id}`).join("；") || "無字幕"}。建立工作後固定此快照。`;
    element("export-start").disabled = false;
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
});
element("export-start").addEventListener("click", async () => {
  if (!currentVideo || !exportSnapshot) return;
  element("export-start").disabled = true;
  try {
    await request(`/api/videos/${controlled(currentVideo.id)}/exports`, "POST", { snapshot: exportSnapshot });
    exportSnapshot = null;
    await pollJobs();
  } catch (error) { showError(error); }
});

function showQaError(error) {
  element("qa-status").textContent = error.message;
}

function updateQaControls() {
  const source = !!currentConversation?.source_version_id;
  const pending = currentConversation?.messages?.some((message) => message.status === "pending");
  element("question").disabled = !geminiConfigured || !source;
  element("qa-submit").disabled = !geminiConfigured || !source || pending || qaSubmitting || !element("question").value.trim();
  element("qa-delete-conversation").disabled = !currentConversation;
  element("qa-no-source").hidden = source;
}

async function loadConversations(videoId) {
  const generation = ++qaGeneration;
  // Clear the previous owner before fetching a different video's conversation.
  if (currentConversation?.video_id !== videoId) {
    currentConversation = null;
    qaMessageSignature = "";
    element("qa-messages").replaceChildren();
    updateQaControls();
  }
  const listing = await request(`/api/videos/${controlled(videoId)}/conversations`);
  if (currentVideo?.id !== videoId || generation !== qaGeneration) return;
  const complete = (currentVideo.subtitles || []).filter((version) => version.complete);
  options(element("qa-source"), complete, currentVideo.qa_version_id,
    subtitleLabel);
  element("qa-source").disabled = !complete.length;
  element("qa-new-conversation").disabled = !complete.length;
  options(element("conversation-select"), listing.conversations, listing.current_conversation_id,
    (conversation) => {
      const version = complete.find((item) => item.id === conversation.source_version_id);
      return `${version ? subtitleLabel(version) : conversation.source_version_id} · 對話 ${conversation.id.slice(0, 8)}`;
    });
  element("conversation-select").disabled = !listing.conversations.length;
  const conversationId = listing.current_conversation_id;
  if (!conversationId) {
    currentConversation = null;
    element("qa-messages").replaceChildren();
    element("qa-source-summary").textContent = "尚無問答依據。";
    element("qa-status").textContent = "請取得／匯入字幕並設為問答依據。";
    updateQaControls();
    return;
  }
  const conversation = await request(`/api/conversations/${controlled(conversationId)}`);
  if (currentVideo?.id !== videoId || generation !== qaGeneration) return;
  renderConversation(conversation);
}

function renderConversation(conversation) {
  if (conversation.video_id !== currentVideo?.id) return;
  currentConversation = conversation;
  const version = (currentVideo.subtitles || []).find((item) => item.id === conversation.source_version_id);
  element("qa-source-summary").textContent = `本串問答依據：${version ? subtitleLabel(version) : conversation.source_version_id}`;
  element("qa-source").value = conversation.source_version_id;
  const pending = conversation.messages.some((message) => message.status === "pending");
  const announcement = pending ? "問題已送出，正在等待完整且驗證通過的回覆。" : "可提出問題；回覆將先整份驗證。";
  // Avoid repeated live announcements and DOM replacements during unchanged polling.
  if (element("qa-status").textContent !== announcement) element("qa-status").textContent = announcement;
  const signature = JSON.stringify([conversation.id, conversation.messages]);
  if (signature !== qaMessageSignature) {
    qaMessageSignature = signature;
    element("qa-messages").replaceChildren();
    for (const message of conversation.messages) renderQaMessage(message, conversation);
  }
  updateQaControls();
}

function paragraph(text) {
  const node = document.createElement("p");
  node.textContent = text;
  return node;
}

function citationTime(seconds) {
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 3600).toString().padStart(2, "0")}:${Math.floor(whole / 60 % 60).toString().padStart(2, "0")}:${(whole % 60).toString().padStart(2, "0")}`;
}

function renderCitation(citation, conversation) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `引用 ${citationTime(citation.start)}–${citationTime(citation.end)} · 依據版本 ${conversation.source_version_id.slice(0, 8)}`;
  const quote = document.createElement("blockquote");
  quote.textContent = citation.text;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = `跳至 ${citationTime(citation.start)} 並展開原文`;
  const help = paragraph("");
  button.addEventListener("click", () => {
    details.open = true;
    const player = element("player");
    if (currentVideo?.id !== conversation.video_id || player.hidden || playbackVideoId !== conversation.video_id) {
      help.textContent = "目前不能跳播；仍可閱讀當時依據版本的原文與時間。";
      return;
    }
    player.currentTime = citation.start;
    help.textContent = "已跳至引用起點；目前播放字幕保持原選擇。";
  });
  details.append(summary, quote, button, help);
  if (element("player").hidden) help.textContent = "目前不能跳播；仍可閱讀當時依據版本的原文與時間。";
  return details;
}

function renderQaMessage(message, conversation) {
  const item = document.createElement("li");
  item.append(paragraph(`問題：${message.question}`));
  if (message.status === "completed" && message.response) {
    item.append(paragraph(`本次參考最近 ${(message.memory_ids || []).length} 組問答`));
    if (!message.in_memory) item.append(paragraph("較早問答：不在記憶內（仍保存可閱讀）。"));
    for (const [field, title] of [["video_content", "影片內容"], ["supplemental_knowledge", "補充知識（未經網路查證）"], ["insufficient_evidence", "缺乏依據"]]) {
      const section = document.createElement("section");
      const heading = document.createElement("h4");
      heading.textContent = title;
      section.append(heading);
      const entries = message.response[field] || [];
      if (!entries.length) section.append(paragraph("本次無此類回覆。"));
      for (const entry of entries) {
        section.append(paragraph(field === "video_content" ? entry.claim : entry));
        if (field === "video_content") {
          for (const citation of entry.citations) section.append(renderCitation(citation, conversation));
        }
      }
      item.append(section);
    }
  } else {
    item.append(paragraph(message.status === "pending" ? "等待回覆（尚未顯示未驗證內容）" : `${statusLabels[message.status] || message.status}：${qaErrorLabel(message.error_code)}`));
    if (message.job_id) {
      const button = document.createElement("button");
      button.type = "button";
      const action = message.status === "pending" ? "cancel" : "retry";
      button.textContent = action === "cancel" ? "取消此問題" : "重試此問題";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await request(`/api/jobs/${controlled(message.job_id)}/${action}`, "POST");
          await pollConversation();
        } catch (error) { showQaError(error); button.disabled = false; }
      });
      item.append(button);
    }
  }
  element("qa-messages").append(item);
}

function qaErrorLabel(code) {
  const labels = {
    qa_token_limit: "來源與問題超過輸入上限；請改用較短來源另開對話。",
    qa_json: "回覆 JSON 格式無效，請重試。",
    qa_citations: "影片主張的引用驗證失敗，未顯示回覆；請重試。",
    qa_finish: "回覆遭截斷或安全阻擋，請檢查問題後重試。",
    qa_empty: "回覆為空，請重試。",
    qa_blocked: "回覆遭安全阻擋，未顯示內容；請調整問題後重試。",
    qa_token_count_failed: "無法計算輸入 token，請稍後重試。",
    qa_owner_changed: "對話或問答依據已變更，此回覆未寫入。",
    missing_gemini_key: "請在專案 .env 設定 GEMINI_API_KEY 並重新啟動服務。",
    qa_key_invalid: "Gemini 金鑰無效，請檢查 .env 的 GEMINI_API_KEY 並重啟服務。",
    interrupted: "服務中斷；不會自動重送，請手動重試。",
  };
  return labels[code] || (code ? jobErrorLabel(code) : "未完成，可手動重試。");
}

async function pollConversation() {
  const videoId = currentVideo?.id;
  const conversationId = currentConversation?.id;
  const generation = qaGeneration;
  if (!videoId || !conversationId) return;
  const conversation = await request(`/api/conversations/${controlled(conversationId)}`);
  if (currentVideo?.id !== videoId || currentConversation?.id !== conversationId || qaGeneration !== generation) return;
  renderConversation(conversation);
}

async function createConversation(versionId) {
  const videoId = currentVideo?.id;
  if (!videoId) return;
  await request(`/api/videos/${controlled(videoId)}/conversations`, "POST", { version_id: versionId });
  if (currentVideo?.id !== videoId) return;
  await loadConversations(videoId);
  if (currentVideo?.id === videoId) element("qa-status").textContent = "已建立新對話";
}

element("qa-new-conversation").addEventListener("click", () => createConversation(element("qa-source").value).catch(showQaError));
element("conversation-select").addEventListener("change", async () => {
  const videoId = currentVideo?.id;
  const conversationId = element("conversation-select").value;
  if (!videoId || !conversationId) return;
  ++qaGeneration;
  currentConversation = null;
  updateQaControls();
  try {
    await request(`/api/videos/${controlled(videoId)}/conversations/${controlled(conversationId)}/select`, "POST");
    if (currentVideo?.id === videoId) await loadConversations(videoId);
  } catch (error) { showQaError(error); }
});
element("qa-delete-conversation").addEventListener("click", async () => {
  const videoId = currentVideo?.id;
  const conversationId = currentConversation?.id;
  if (!videoId || !conversationId) return;
  ++qaGeneration;
  currentConversation = null;
  updateQaControls();
  try {
    await request(`/api/conversations/${controlled(conversationId)}`, "DELETE");
    if (currentVideo?.id === videoId) await loadConversations(videoId);
  } catch (error) { showQaError(error); }
});
element("question").addEventListener("input", updateQaControls);
element("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!element("qa-submit").disabled) element("qa-form").requestSubmit();
  }
});
element("qa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const videoId = currentVideo?.id;
  const conversationId = currentConversation?.id;
  const question = element("question").value.trim();
  if (!videoId || !conversationId || !question || element("qa-submit").disabled) return;
  qaSubmitting = true;
  updateQaControls();
  try {
    await request(`/api/conversations/${controlled(conversationId)}/messages`, "POST", { question });
    if (currentVideo?.id !== videoId || currentConversation?.id !== conversationId) return;
    element("question").value = "";
    await pollConversation();
  } catch (error) { showQaError(error); }
  finally { qaSubmitting = false; updateQaControls(); }
});

let deletionScope = null;

function megabytes(bytes) {
  return `${(bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0)} MB`;
}

async function openDeleteDialog() {
  const video = currentVideo;
  if (!video) return;
  const dialog = element("delete-dialog");
  deletionScope = null;
  element("delete-acknowledge").checked = false;
  element("delete-confirm").disabled = true;
  element("delete-error").textContent = "";
  element("delete-scope").replaceChildren(paragraph("正在計算刪除範圍…"));
  WorkspaceDialog.open(dialog, element("delete-video"), element("delete-cancel"));
  const scope = await request(`/api/videos/${controlled(video.id)}/deletion`);
  if (!dialog.open || currentVideo?.id !== video.id) return;
  deletionScope = scope;
  const list = document.createElement("ul");
  for (const text of [
    `影音資產 ${scope.assets} 個（另有相容預覽 ${scope.previews} 個）`,
    `字幕版本 ${scope.subtitle_versions} 份`,
    `對話 ${scope.conversations} 串`,
    `匯出成品 ${scope.exports} 個`,
    `估計大小 ${megabytes(scope.size_bytes)}`,
  ]) {
    const item = document.createElement("li");
    item.textContent = text;
    list.append(item);
  }
  element("delete-scope").replaceChildren(
    paragraph(`將從影片庫永久刪除「${scope.title}」及以下資料：`),
    list,
    paragraph(scope.active_jobs ? `有 ${scope.active_jobs} 個進行中的工作會先取消並停止。` : "目前沒有進行中的工作。"),
    paragraph("影片庫外另存的檔案不受影響。刪除後可用相同網址重新匯入。"),
  );
  element("delete-confirm").disabled = !element("delete-acknowledge").checked;
}

async function confirmDeletion() {
  const video = currentVideo;
  const dialog = element("delete-dialog");
  if (!video || !deletionScope || !element("delete-acknowledge").checked) return;
  dialog.dataset.busy = "true";
  element("delete-confirm").disabled = true;
  element("delete-cancel").disabled = true;
  element("delete-error").textContent = "";
  try {
    await request(`/api/videos/${controlled(video.id)}/delete`, "POST", { confirmation: deletionScope.confirmation });
    dialog.dataset.busy = "false";
    element("delete-cancel").disabled = false;
    forgetVideo(video.id);
    WorkspaceDialog.close(dialog);
    element("import-status").textContent = "影片及其影片庫資料已刪除。";
    await loadVideos();
    await pollJobs();
  } catch (error) {
    element("delete-error").textContent = deletionErrorLabel(error);
    if (error.status === 409 || error.status === 500) {
      // The video may already be marked for cleanup; it is hidden and listed as pending.
      await loadVideos().catch(() => {});
      if (!libraryVideos.some((item) => item.id === video.id)) forgetVideo(video.id);
    }
  } finally {
    dialog.dataset.busy = "false";
    element("delete-cancel").disabled = false;
    element("delete-confirm").disabled = !element("delete-acknowledge").checked || !deletionScope;
  }
}

function forgetVideo(videoId) {
  if (currentVideo?.id !== videoId) return;
  ++opening;
  ++qaGeneration;
  currentVideo = null;
  deletionScope = null;
  stopPlayer("");
  element("video-details").hidden = true;
  currentConversation = null;
  qaMessageSignature = "";
  element("qa-messages").replaceChildren();
  for (const id of ["qa-source", "conversation-select"]) {
    element(id).replaceChildren();
    element(id).disabled = true;
  }
  element("qa-new-conversation").disabled = true;
  element("qa-source-summary").textContent = "尚無問答依據。";
  element("qa-status").textContent = "尚未選取影片";
  updateQaControls();
  renderJobs();
}
