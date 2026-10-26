"use strict";

const DATA_URL = "./data/records.json";
const state = { items: [], query: "", tag: "", sort: "newest", audioFilter: "all", videoId: null, audioId: null, loading: true, error: false };
const $ = (selector) => document.querySelector(selector);
const ui = {
  grid: $("#records-grid"), search: $("#search"), sort: $("#sort"), tags: $("#tag-filters"), empty: $("#empty-state"),
  emptyTitle: $("#empty-title"), emptyCopy: $("#empty-copy"), reset: $("#reset-filters"), retry: $("#retry-load"),
  watch: $("#watch-panel"), video: $("#watch-video"), audio: $("#audio-player"),
  queue: $("#audio-queue"), audioEmpty: $("#audio-empty"), audioToggle: $("#audio-toggle"), audioPrev: $("#audio-prev"),
  audioNext: $("#audio-next"), audioStop: $("#audio-stop"), audioProgress: $("#audio-progress"), audioVolume: $("#audio-volume"),
  audioElapsed: $("#audio-elapsed"), audioTotal: $("#audio-total"), winampTime: $("#winamp-time"),
  nowPlaying: $("#now-playing"), mobileBar: $("#mobile-audio-bar"), mobileToggle: $("#mobile-audio-toggle")
};
const dateFormatter = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" });
const shortDateFormatter = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });
let watchTrigger = null;
let loadedAudioFile = "";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name, className = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function plural(count, forms) {
  const ten = count % 10;
  const hundred = count % 100;
  return forms[ten === 1 && hundred !== 11 ? 0 : ten >= 2 && ten <= 4 && (hundred < 12 || hundred > 14) ? 1 : 2];
}

function duration(seconds) {
  const value = Math.floor(Math.max(0, Number(seconds) || 0));
  if (!value) return "";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value % 3600 / 60);
  const rest = String(value % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

function playerTime(seconds) { return duration(seconds) || "0:00"; }

function safePath(value, kinds = ["video", "audio"]) {
  if (typeof value !== "string" || /[\\\u0000-\u001f?#]/.test(value)) return "";
  let path;
  try { path = decodeURIComponent(value.replace(/^\.\//, "")); } catch { return ""; }
  const parts = path.split("/");
  if (parts[0] !== "content" || !kinds.includes(parts[1]) || parts.length < 3 || parts.some((part) => !part || part === "." || part === "..") || /[\\\u0000-\u001f%?#]/.test(path)) return "";
  const url = new URL(path, document.baseURI);
  return url.origin === window.location.origin ? url.href : "";
}

function telegramUrl(value) {
  try {
    const url = new URL(String(value));
    if (url.protocol === "https:" && url.hostname === "t.me" && !url.port && !url.username && !url.password) return url.href;
  } catch { /* Invalid source URLs fall back to the channel. */ }
  return "https://t.me/records_live4dev";
}

function normalize(item) {
  if (!item || typeof item !== "object" || !["video", "audio"].includes(item.kind)) return null;
  const file = safePath(item.file, [item.kind]);
  const parsedDate = item.published_at ? new Date(item.published_at) : null;
  const publishedAt = parsedDate && !Number.isNaN(parsedDate.getTime()) ? parsedDate : null;
  const tags = Array.isArray(item.tags) ? [...new Set(item.tags.filter((tag) => typeof tag === "string").map((tag) => tag.replace(/^#+/, "").trim()).filter(Boolean))] : [];
  return {
    id: String(item.id ?? `${item.kind}-${item.message_id ?? item.telegram_url}`), kind: item.kind, file,
    title: String(item.title || "Запись без названия"), description: String(item.description || ""),
    caption: String(item.caption_original || item.description || ""), tags, publishedAt,
    duration: Math.max(0, Number(item.duration_seconds) || 0), telegramUrl: telegramUrl(item.telegram_url),
    channel: String(item.channel || "records_live4dev").replace(/^@/, ""), poster: safePath(item.poster, ["video"]),
    audioFile: safePath(item.audio_file, ["audio"]), sourceVideoId: String(item.source_video_id || ""),
    audioOrigin: item.audio_origin === "extracted" || item.source_video_id ? "extracted" : "original"
  };
}

function matches(item) {
  const query = state.query.trim().toLocaleLowerCase("ru");
  const haystack = `${item.title} ${item.description} ${item.caption} ${item.tags.join(" ")} ${item.channel}`.toLocaleLowerCase("ru");
  return (!query || haystack.includes(query)) && (!state.tag || item.tags.includes(state.tag));
}

function sorted(items) {
  return [...items].sort((a, b) => {
    if (state.sort === "available" && Boolean(a.file) !== Boolean(b.file)) return Number(Boolean(b.file)) - Number(Boolean(a.file));
    if (state.sort === "shortest") return (a.duration || Infinity) - (b.duration || Infinity);
    const first = a.publishedAt ? a.publishedAt.getTime() : 0;
    const second = b.publishedAt ? b.publishedAt.getTime() : 0;
    return state.sort === "oldest" ? first - second : second - first;
  });
}

function videos() { return sorted(state.items.filter((item) => item.kind === "video" && matches(item))); }
function audios() {
  return sorted(state.items.filter((item) => item.kind === "audio" && matches(item) && (state.audioFilter === "all" || item.audioOrigin === state.audioFilter)))
    .sort((a, b) => Number(Boolean(b.file)) - Number(Boolean(a.file)));
}
function audioQueue() {
  const visible = audios().filter((item) => item.file);
  if (!state.audioId || visible.some((item) => item.id === state.audioId)) return visible;
  return sorted(state.items.filter((item) => item.kind === "audio" && item.file));
}
function currentAudio() { return state.items.find((item) => item.kind === "audio" && item.id === state.audioId); }

function tagChip(tag, active = false) {
  const chip = element("button", `topic-chip${active ? " is-active" : ""}`, tag ? `#${tag}` : "Все темы");
  chip.type = "button";
  chip.setAttribute("aria-pressed", String(active));
  chip.addEventListener("click", () => {
    state.tag = tag;
    renderCatalog(); renderTags(); renderAudioQueue();
  });
  return chip;
}

function renderTags() {
  const tagCounts = new Map();
  state.items.forEach((item) => item.tags.forEach((tag) => tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1)));
  const tags = [...tagCounts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru")).map(([tag]) => tag);
  ui.tags.replaceChildren(tagChip("", !state.tag), ...tags.map((tag) => tagChip(tag, state.tag === tag)));
  const video = state.items.find((item) => item.id === state.videoId);
  const audio = currentAudio();
  if (video) $("#watch-tags").replaceChildren(...video.tags.map((tag) => tagChip(tag, state.tag === tag)));
  if (audio) $("#audio-detail-tags").replaceChildren(...audio.tags.map((tag) => tagChip(tag, state.tag === tag)));
}

function createCard(item) {
  const card = element("article", "record-card");
  const link = element("a", "thumbnail-link");
  link.href = `#watch=${encodeURIComponent(item.id)}`;
  link.setAttribute("aria-label", `${item.file ? "Смотреть" : "Открыть описание"}: ${item.title}`);
  const placeholder = element("div", "thumbnail-placeholder");
  placeholder.append(icon(item.file ? "play" : "video"));
  link.append(placeholder);
  if (item.poster) {
    const image = element("img");
    image.src = item.poster; image.alt = ""; image.loading = "lazy"; image.decoding = "async";
    image.addEventListener("error", () => image.remove(), { once: true });
    link.append(image);
  }
  link.append(element("span", `thumb-kind${item.file ? " is-local" : ""}`, item.file ? "СМОТРЕТЬ НА САЙТЕ" : "В TELEGRAM"));
  if (item.duration) link.append(element("span", "thumb-duration", duration(item.duration)));
  const hover = element("span", `thumb-hover${item.file ? "" : " pending-hover"}`); hover.append(icon(item.file ? "play" : "telegram")); link.append(hover);
  link.addEventListener("click", (event) => { event.preventDefault(); watchTrigger = link; openWatch(item, Boolean(item.file)); });
  const body = element("div", "record-body");
  const avatar = element("span", "record-avatar", "r."); avatar.setAttribute("aria-hidden", "true");
  const info = element("div", "record-info");
  const title = element("h3", "record-title");
  const titleLink = element("a", "", item.title); titleLink.href = link.href;
  titleLink.addEventListener("click", (event) => { event.preventDefault(); watchTrigger = titleLink; openWatch(item, Boolean(item.file)); });
  title.append(titleLink);
  const meta = element("p", "record-meta"); meta.append(element("strong", "", "live4.dev"));
  if (item.publishedAt) {
    const time = element("time", "", dateFormatter.format(item.publishedAt)); time.dateTime = item.publishedAt.toISOString();
    meta.append(element("span", "meta-dot", "·"), time);
  }
  info.append(title, meta);
  if (item.tags.length) info.append(element("p", "record-tagline", item.tags.slice(0, 3).map((tag) => `#${tag}`).join("  ")));
  body.append(avatar, info); card.append(link, body);
  return card;
}

function renderLoading() {
  ui.grid.setAttribute("aria-busy", "true");
  ui.grid.replaceChildren(...Array.from({ length: 4 }, () => {
    const card = element("div", "loading-card"); card.setAttribute("aria-hidden", "true");
    card.append(element("div", "skeleton-thumb"), element("div", "skeleton-line"), element("div", "skeleton-line"));
    return card;
  }));
  ui.empty.hidden = true;
}

function renderCatalog() {
  if (state.loading) return renderLoading();
  const items = videos();
  ui.grid.setAttribute("aria-busy", "false");
  ui.grid.replaceChildren(...items.map(createCard));
  $("#video-count").textContent = String(items.length);
  $("#results-label").textContent = `${items.length} ${plural(items.length, ["видеозапись", "видеозаписи", "видеозаписей"])}${state.query ? ` по запросу «${state.query}»` : ""}`;
  ui.empty.hidden = items.length > 0;
  const filters = Boolean(state.query.trim() || state.tag);
  ui.reset.hidden = !filters || state.error;
  ui.retry.hidden = !state.error;
  if (state.error) {
    ui.emptyTitle.textContent = "Архив не загрузился";
    ui.emptyCopy.textContent = "Попробуйте ещё раз. Пока можно открыть записи в Telegram.";
  } else if (filters) {
    ui.emptyTitle.textContent = "Ничего не найдено";
    ui.emptyCopy.textContent = "Попробуйте другую тему или более короткий поисковый запрос.";
  } else {
    ui.emptyTitle.textContent = "Видео появятся здесь";
    ui.emptyCopy.textContent = "Коллекция пока пуста. Здесь будут записи, описания и теги из канала live4.dev.";
  }
}

function originLabel(item) { return item.audioOrigin === "extracted" ? "Звук из видео" : "Аудиозапись"; }

function renderAudioQueue() {
  const focusedTrack = ui.queue.contains(document.activeElement) ? document.activeElement.closest(".queue-button")?.dataset.trackId : "";
  const items = audios();
  const available = items.filter((item) => item.file).length;
  $("#audio-count").textContent = String(state.items.filter((item) => item.kind === "audio").length);
  $("#queue-count").textContent = items.length ? `${available} ${plural(available, ["доступна", "доступны", "доступны"])}` : "";
  let playableIndex = 0;
  ui.queue.replaceChildren(...items.map((item) => {
    const row = element("li", "queue-item");
    const button = element(item.file ? "button" : "summary", `queue-button${state.audioId === item.id ? " is-active" : ""}${item.file ? "" : " is-pending"}`);
    if (item.file) button.type = "button";
    button.dataset.trackId = item.id;
    const active = state.audioId === item.id;
    button.setAttribute("aria-label", `${item.file ? active && !ui.audio.paused ? "Приостановить" : "Слушать" : "Описание записи из Telegram"}: ${item.title}. ${originLabel(item)}`);
    if (active) button.setAttribute("aria-current", "true");
    const number = element("span", "queue-index");
    if (item.file) playableIndex += 1;
    if (active) number.append(icon(ui.audio.paused ? "play" : "pause", ui.audio.paused ? "" : "queue-pause"));
    else if (!item.file) number.append(icon("telegram"));
    else number.textContent = String(playableIndex).padStart(2, "0");
    const info = element("span", "queue-info");
    const meta = [originLabel(item), !item.file ? "В Telegram · Описание" : item.publishedAt ? shortDateFormatter.format(item.publishedAt) : ""].filter(Boolean).join(" · ");
    info.append(element("span", "queue-title", item.title), element("span", "queue-subtitle", meta));
    button.append(number, info, element("span", "queue-duration", duration(item.duration)));
    if (item.file) {
      button.addEventListener("click", () => { if (state.audioId === item.id) toggleAudio(); else selectAudio(item, true); });
      row.append(button);
    } else {
      const details = element("details", "pending-queue-details");
      const description = element("div", "pending-audio-description");
      const caption = element("p", "original-caption", item.caption || "К этой записи не добавлено описание.");
      const tags = element("div", "audio-detail-tags"); tags.append(...item.tags.map((tag) => tagChip(tag, state.tag === tag)));
      const source = element("a", "pending-audio-source", "Слушать в Telegram");
      source.href = item.telegramUrl; source.target = "_blank"; source.rel = "noopener noreferrer"; source.append(icon("arrow"));
      description.append(caption, tags, source); details.append(button, description); row.append(details);
    }
    return row;
  }));
  ui.audioEmpty.hidden = items.length > 0;
  ui.audioEmpty.textContent = state.loading ? "Загружаем аудиозаписи…" : state.error ? "Аудиоплейлист пока недоступен." : state.query.trim() || state.tag || state.audioFilter !== "all" ? "Нет аудио с такими условиями. Измените поиск или фильтр." : "Плейлист пока пуст. Аудиозаписи появятся здесь вместе со звуком из видео.";
  updateAudioControls();
  if (focusedTrack) [...ui.queue.querySelectorAll(".queue-button")].find((button) => button.dataset.trackId === focusedTrack)?.focus({ preventScroll: true });
}

function updateAudioControls() {
  const track = currentAudio();
  const queue = audioQueue();
  const index = queue.findIndex((item) => item.id === state.audioId);
  const playing = Boolean(track && !ui.audio.paused && !ui.audio.ended);
  ui.audioToggle.disabled = !queue.length && !track;
  ui.audioPrev.disabled = !track || index <= 0;
  ui.audioNext.disabled = !track || index < 0 || index >= queue.length - 1;
  ui.audioStop.disabled = !track;
  ui.audioToggle.replaceChildren(icon(playing ? "pause" : "play"));
  ui.audioToggle.classList.toggle("is-playing", playing);
  ui.audioToggle.setAttribute("aria-label", playing ? "Приостановить аудио" : track ? "Продолжить аудио" : "Начать слушать");
  ui.mobileToggle.replaceChildren(icon(playing ? "pause" : "play"));
  ui.mobileToggle.classList.toggle("is-playing", playing);
  ui.mobileToggle.setAttribute("aria-label", playing ? "Приостановить аудио" : "Продолжить аудио");
  ui.nowPlaying.classList.toggle("is-playing", playing);
  ui.mobileBar.hidden = !track;
  document.body.classList.toggle("has-audio", Boolean(track));
  $("#player-label").textContent = track ? playing ? "СЕЙЧАС ИГРАЕТ" : "ВЫБРАННАЯ ЗАПИСЬ" : "ВАШ АУДИОАРХИВ";
  syncAudioProgress();
  ui.queue.querySelectorAll(".queue-button").forEach((button) => {
    if (button.dataset.trackId === state.audioId && track) {
      button.querySelector(".queue-index").replaceChildren(icon(playing ? "pause" : "play", playing ? "queue-pause" : ""));
      button.setAttribute("aria-label", `${playing ? "Приостановить" : "Слушать"}: ${track.title}. ${originLabel(track)}`);
    }
  });
}

function syncAudioProgress() {
  const track = currentAudio();
  const mediaDuration = Number.isFinite(ui.audio.duration) && ui.audio.duration > 0 ? ui.audio.duration : Number(track?.duration) || 0;
  const current = Number.isFinite(ui.audio.currentTime) ? Math.max(0, ui.audio.currentTime) : 0;
  const progress = mediaDuration ? Math.min(1000, Math.round(current / mediaDuration * 1000)) : 0;
  ui.audioProgress.value = String(progress);
  ui.audioProgress.disabled = !track || !mediaDuration;
  ui.audioProgress.setAttribute("aria-valuetext", `${playerTime(current)} из ${playerTime(mediaDuration)}`);
  ui.audioElapsed.textContent = playerTime(current);
  ui.audioTotal.textContent = playerTime(mediaDuration);
  ui.winampTime.textContent = playerTime(current);
}

function audioMessage(text) { $("#audio-message").textContent = text; $("#audio-message").hidden = !text; }

function playAudio() {
  ui.video.pause(); audioMessage("");
  const result = ui.audio.play();
  if (result) result.catch((error) => { if (error.name !== "AbortError") audioMessage("Не удалось начать воспроизведение. Нажмите на кнопку ещё раз или скачайте запись."); });
}

function selectAudio(item, play = false) {
  if (!item.file) return;
  ui.audio.pause(); state.audioId = item.id; ui.video.pause();
  if (loadedAudioFile !== item.file) { ui.audio.src = item.file; ui.audio.load(); loadedAudioFile = item.file; }
  ui.audio.hidden = false;
  ui.audio.setAttribute("aria-label", `Аудио: ${item.title}`);
  $("#current-track-title").textContent = item.title;
  $("#current-track-title").title = item.title;
  $("#mobile-track-title").textContent = item.title;
  $("#current-track-meta").textContent = [originLabel(item), duration(item.duration)].filter(Boolean).join(" · ");
  $("#audio-details").hidden = false; $("#audio-details").open = false;
  $("#audio-caption").textContent = item.caption || "К этой записи не добавлено описание.";
  $("#audio-detail-tags").replaceChildren(...item.tags.map((tag) => tagChip(tag, state.tag === tag)));
  $("#audio-source").href = item.telegramUrl; $("#audio-download").href = item.file;
  audioMessage(""); renderAudioQueue();
  if (play) playAudio();
}

function toggleAudio() {
  const track = currentAudio();
  if (!track) { const first = audioQueue()[0]; if (first) selectAudio(first, true); }
  else if (ui.audio.paused || ui.audio.ended) playAudio();
  else ui.audio.pause();
}

function moveAudio(direction, ended = false) {
  const queue = audioQueue();
  const index = queue.findIndex((item) => item.id === state.audioId);
  const next = queue[index + direction];
  if (index >= 0 && next) selectAudio(next, true);
  else if (ended) { audioMessage("Плейлист прослушан. Можно выбрать следующую запись."); updateAudioControls(); }
}

function stopAudio() {
  if (!currentAudio()) return;
  ui.audio.pause();
  ui.audio.currentTime = 0;
  syncAudioProgress();
  updateAudioControls();
}

function extractedAudio(video) { return state.items.find((item) => item.kind === "audio" && item.file && item.sourceVideoId === video.id) || state.items.find((item) => item.kind === "audio" && video.audioFile && item.file === video.audioFile); }

function renderWatch(item, autoplay = false) {
  const changed = state.videoId !== item.id;
  state.videoId = item.id;
  ui.watch.hidden = false;
  $("#catalog-heading").textContent = "Ещё записи";
  if (changed) {
    ui.audio.pause(); ui.video.pause(); ui.video.removeAttribute("poster"); ui.video.removeAttribute("src");
    ui.video.hidden = !item.file;
    $("#pending-video").hidden = Boolean(item.file);
    if (item.file) { if (item.poster) ui.video.poster = item.poster; ui.video.src = item.file; }
    else {
      $("#pending-poster").hidden = !item.poster;
      if (item.poster) $("#pending-poster").src = item.poster;
      else $("#pending-poster").removeAttribute("src");
      $("#pending-video-source").href = item.telegramUrl;
    }
    ui.video.load(); ui.video.setAttribute("aria-label", `Видео: ${item.title}`);
    $("#watch-title").textContent = item.title;
    $("#watch-meta").textContent = [item.publishedAt ? dateFormatter.format(item.publishedAt) : "", duration(item.duration)].filter(Boolean).join(" · ");
    $("#watch-tags").replaceChildren(...item.tags.map((tag) => tagChip(tag, state.tag === tag)));
    $("#watch-caption").textContent = item.caption || "К этой записи не добавлено описание.";
    $("#watch-description").open = !item.file;
    $("#watch-source").href = item.telegramUrl;
    $("#watch-download").hidden = !item.file;
    if (item.file) $("#watch-download").href = item.file;
    else $("#watch-download").removeAttribute("href");
    const audioTrack = extractedAudio(item);
    const audioFile = audioTrack ? audioTrack.file : item.audioFile;
    $("#listen-video").hidden = !audioTrack;
    $("#listen-video").onclick = audioTrack ? () => selectAudio(audioTrack, true) : null;
    $("#watch-audio-download").hidden = !audioFile;
    if (audioFile) $("#watch-audio-download").href = audioFile;
    else $("#watch-audio-download").removeAttribute("href");
    $("#video-message").hidden = true;
  }
  if (autoplay && item.file) {
    const result = ui.video.play();
    if (result) result.catch((error) => { if (error.name !== "AbortError") { $("#video-message").textContent = "Нажмите ▶ в плеере, чтобы начать просмотр."; $("#video-message").hidden = false; } });
  }
}

function openWatch(item, autoplay = false) {
  const hash = `#watch=${encodeURIComponent(item.id)}`;
  if (state.videoId) history.replaceState(history.state, "", hash);
  else history.pushState({ recordsWatch: true }, "", hash);
  renderWatch(item, autoplay);
  ui.watch.scrollIntoView({ behavior: "smooth", block: "start" });
  $("#watch-title").setAttribute("tabindex", "-1");
  $("#watch-title").focus({ preventScroll: true });
}

function hideWatch() {
  if (!state.videoId) return;
  ui.video.pause(); ui.video.removeAttribute("src"); ui.video.removeAttribute("poster"); ui.video.load();
  state.videoId = null; ui.watch.hidden = true;
  $("#catalog-heading").textContent = "Видеоархив";
}

function syncHash() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const id = params.get("watch");
  const item = state.items.find((candidate) => candidate.kind === "video" && candidate.id === id);
  if (item) renderWatch(item);
  else hideWatch();
}

function resetFilters() {
  state.query = ""; state.tag = ""; ui.search.value = "";
  renderTags(); renderCatalog(); renderAudioQueue();
}

async function loadRecords() {
  state.loading = true; state.error = false; renderLoading(); ui.retry.hidden = true;
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.items)) throw new Error("Некорректный формат каталога");
    const unique = new Map();
    payload.items.map(normalize).filter(Boolean).forEach((item) => { if (!unique.has(item.id)) unique.set(item.id, item); });
    state.items = [...unique.values()];
  } catch (error) {
    console.error("Не удалось загрузить архив", error); state.error = true;
  } finally {
    state.loading = false; renderTags(); renderCatalog(); renderAudioQueue(); syncHash();
  }
}

$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); $("#catalog").scrollIntoView({ behavior: "smooth", block: "start" }); });
ui.search.addEventListener("input", () => { state.query = ui.search.value; renderCatalog(); renderAudioQueue(); });
ui.sort.addEventListener("change", () => { state.sort = ui.sort.value; renderCatalog(); renderAudioQueue(); });
ui.reset.addEventListener("click", () => { resetFilters(); ui.search.focus(); });
ui.retry.addEventListener("click", loadRecords);
$("#home-button").addEventListener("click", () => { history.replaceState(null, "", window.location.pathname + window.location.search); hideWatch(); resetFilters(); window.scrollTo({ top: 0, behavior: "smooth" }); });
$("#close-watch").addEventListener("click", () => {
  if (history.state && history.state.recordsWatch) history.back();
  else { history.replaceState(null, "", window.location.pathname + window.location.search); hideWatch(); }
  if (watchTrigger && watchTrigger.isConnected) watchTrigger.focus({ preventScroll: true });
});
document.querySelectorAll("[data-audio-filter]").forEach((button) => button.addEventListener("click", () => {
  state.audioFilter = button.dataset.audioFilter;
  document.querySelectorAll("[data-audio-filter]").forEach((candidate) => { const active = candidate === button; candidate.classList.toggle("is-active", active); candidate.setAttribute("aria-pressed", String(active)); });
  renderAudioQueue();
}));
ui.audioToggle.addEventListener("click", toggleAudio); ui.mobileToggle.addEventListener("click", toggleAudio);
ui.audioPrev.addEventListener("click", () => moveAudio(-1)); ui.audioNext.addEventListener("click", () => moveAudio(1));
ui.audioStop.addEventListener("click", stopAudio);
ui.audioProgress.addEventListener("input", () => {
  if (!Number.isFinite(ui.audio.duration) || ui.audio.duration <= 0) return;
  ui.audio.currentTime = Number(ui.audioProgress.value) / 1000 * ui.audio.duration;
  syncAudioProgress();
});
ui.audioVolume.addEventListener("input", () => {
  ui.audio.volume = Number(ui.audioVolume.value);
  ui.audioVolume.setAttribute("aria-valuetext", `${Math.round(ui.audio.volume * 100)}%`);
});
ui.audio.addEventListener("play", () => { ui.video.pause(); updateAudioControls(); });
ui.audio.addEventListener("pause", updateAudioControls);
ui.audio.addEventListener("timeupdate", syncAudioProgress);
ui.audio.addEventListener("loadedmetadata", syncAudioProgress);
ui.audio.addEventListener("durationchange", syncAudioProgress);
ui.audio.addEventListener("ended", () => moveAudio(1, true));
ui.audio.addEventListener("error", () => { audioMessage("Эту запись не удалось воспроизвести. Попробуйте скачать файл или выбрать другую запись."); updateAudioControls(); });
ui.video.addEventListener("play", () => { ui.audio.pause(); $("#video-message").hidden = true; });
ui.video.addEventListener("error", () => { if (state.videoId && ui.video.getAttribute("src")) { $("#video-message").textContent = "Видео не удалось воспроизвести. Можно скачать файл или открыть публикацию в Telegram."; $("#video-message").hidden = false; } });
$("#pending-poster").addEventListener("error", () => { $("#pending-poster").hidden = true; });
window.addEventListener("popstate", syncHash); window.addEventListener("hashchange", syncHash);
document.addEventListener("keydown", (event) => {
  const editable = event.target instanceof HTMLElement && (event.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName));
  if (event.key === "/" && !editable && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); ui.search.focus(); }
  if (event.key === "Escape" && state.videoId && !editable) $("#close-watch").click();
});
ui.audio.volume = Number(ui.audioVolume.value);
ui.audioVolume.setAttribute("aria-valuetext", `${Math.round(ui.audio.volume * 100)}%`);
loadRecords();
