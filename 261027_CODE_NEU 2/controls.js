const nextButton = document.querySelector("#next");
const shotsButton = document.querySelector("#shots");
const printLiveButton = document.querySelector("#print-live");
const hud = document.querySelector("#hud");
const channelName = document.querySelector("#channel-name");
const viewerLine = document.querySelector("#viewer-line");
const channelMeta = document.querySelector("#channel-meta");
const statusText = document.querySelector("#status-text");
const toast = document.querySelector("#toast");
const printMeta = document.querySelector("#print-meta");

let current = null;
let busy = false;

function showToast(message) {
  toast.hidden = false;
  toast.textContent = message;
}

function clearToast() {
  toast.hidden = true;
}

function setBusy(isBusy) {
  busy = isBusy;
  nextButton.disabled = isBusy;
  nextButton.textContent = isBusy
    ? "Lädt …"
    : current
      ? "Nächster Stream"
      : "Stream laden";
}

function showStream(stream) {
  current = stream;
  clearToast();
  channelName.textContent = stream.displayName || stream.login;
  viewerLine.textContent = "0 Zuschauer*innen";
  channelMeta.textContent = stream.game || "";
  channelMeta.hidden = !stream.game;
  hud.hidden = false;
  statusText.textContent =
    "Aktueller Stream (Live-Seite und Screenshots nutzen denselben Zustand).";
}

async function loadNext() {
  if (busy) return;
  const previous = current;
  setBusy(true);
  statusText.textContent = "Suche einen Livestream mit 0 Zuschauer*innen …";
  try {
    const query = previous
      ? `?exclude=${encodeURIComponent(previous.login)}`
      : "";
    const response = await fetch(`/api/random${query}`, { cache: "no-store" });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok) {
      throw new Error(
        data.error || "Der Stream konnte nicht geladen werden. Bitte erneut versuchen."
      );
    }
    if (data.viewerCount !== 0 || !data.login) {
      throw new Error("Der Stream hat nicht genau 0 Zuschauer*innen.");
    }
    if (previous && data.login.toLowerCase() === previous.login.toLowerCase()) {
      throw new Error("Es wurde derselbe Stream erneut geliefert.");
    }
    showStream(data);
  } catch (error) {
    showToast(
      error.message || "Der Stream konnte nicht geladen werden. Bitte erneut versuchen."
    );
    statusText.textContent = previous
      ? "Vorheriger Stream bleibt aktiv (Fehler beim Wechsel)."
      : "Kein Stream geladen.";
  } finally {
    setBusy(false);
  }
}

function showShots(enabled) {
  shotsButton.setAttribute("aria-pressed", enabled ? "true" : "false");
  shotsButton.textContent = enabled ? "Screenshots an" : "Screenshots aus";
}

async function setShots(enabled) {
  shotsButton.disabled = true;
  try {
    const response = await fetch("/api/screenshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Der Schalter hat nicht reagiert.");
    }
    showShots(Boolean(data.enabled));
  } catch (error) {
    showToast(error.message || "Screenshots konnten nicht umgestellt werden.");
  } finally {
    shotsButton.disabled = false;
  }
}

function showPrintLive(enabled, meta) {
  printLiveButton.setAttribute("aria-pressed", enabled ? "true" : "false");
  printLiveButton.textContent = enabled ? "Live-Druck an" : "Live-Druck aus";
  if (meta && printMeta) {
    const printer = meta.printer || "?";
    const last = meta.last || {};
    const lastLine = last.jobId
      ? `Zuletzt: ${last.jobId}${last.at ? ` · ${last.at}` : ""}`
      : last.error
        ? `Zuletzt Fehler: ${last.error}`
        : "Noch kein Live-Druck-Job.";
    printMeta.textContent = `Drucker: ${printer}. ${lastLine}`;
  }
}

async function setPrintLive(enabled) {
  printLiveButton.disabled = true;
  try {
    const response = await fetch("/api/print-live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Der Schalter hat nicht reagiert.");
    }
    showPrintLive(Boolean(data.enabled), data);
  } catch (error) {
    showToast(error.message || "Live-Druck konnte nicht umgestellt werden.");
  } finally {
    printLiveButton.disabled = false;
  }
}

nextButton.addEventListener("click", loadNext);
shotsButton.addEventListener("click", () => {
  const enabled = shotsButton.getAttribute("aria-pressed") === "true";
  setShots(!enabled);
});
printLiveButton.addEventListener("click", () => {
  const enabled = printLiveButton.getAttribute("aria-pressed") === "true";
  setPrintLive(!enabled);
});

fetch("/api/screenshots", { cache: "no-store" })
  .then((response) => response.json())
  .then((data) => showShots(Boolean(data.enabled)))
  .catch(() => {});

fetch("/api/print-live", { cache: "no-store" })
  .then((response) => response.json())
  .then((data) => showPrintLive(Boolean(data.enabled), data))
  .catch(() => {});

fetch("/api/current", { cache: "no-store" })
  .then((response) => response.json())
  .then((data) => {
    if (data.stream && data.stream.login) {
      showStream(data.stream);
    }
  })
  .catch(() => {});

const categoryList = document.querySelector("#category-list");
const languageList = document.querySelector("#language-list");
const gamingEnabled = document.querySelector("#gaming-enabled");
const filtersSave = document.querySelector("#filters-save");
const filtersReset = document.querySelector("#filters-reset");
const filterStatus = document.querySelector("#filter-status");

function renderChecks(container, keys, stateMap) {
  container.innerHTML = "";
  keys.forEach((key) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = key;
    input.checked = stateMap[key] !== false;
    label.appendChild(input);
    label.appendChild(document.createTextNode(key));
    container.appendChild(label);
  });
}

function collectMap(container) {
  const map = {};
  container.querySelectorAll('input[type="checkbox"]').forEach((input) => {
    map[input.value] = input.checked;
  });
  return map;
}

function showFilters(payload) {
  const filters = payload.filters || {};
  const cats = payload.categoryNames || Object.keys(filters.categories || {});
  const langs = payload.languages || Object.keys(filters.languages || {});
  renderChecks(categoryList, cats, filters.categories || {});
  renderChecks(languageList, langs, filters.languages || {});
  gamingEnabled.checked = filters.gamingEnabled !== false;
  const onCats = Object.values(filters.categories || {}).filter(Boolean).length;
  const onLangs = Object.values(filters.languages || {}).filter(Boolean).length;
  filterStatus.textContent =
    `${onCats}/${cats.length} Kategorien, ${onLangs}/${langs.length} Sprachen, ` +
    `Gaming ${filters.gamingEnabled !== false ? "an" : "aus"}, ` +
    `Verhältnis ${filters.preferredRatio ?? 7}:${filters.gamingRatio ?? 3}`;
}

async function loadFilters() {
  try {
    const response = await fetch("/api/filters", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Filter nicht geladen.");
    showFilters(data);
  } catch (error) {
    showToast(error.message || "Filter konnten nicht geladen werden.");
  }
}

async function saveFilters() {
  filtersSave.disabled = true;
  try {
    const body = {
      filters: {
        categories: collectMap(categoryList),
        languages: collectMap(languageList),
        gamingEnabled: gamingEnabled.checked,
        preferredRatio: 7,
        gamingRatio: 3,
      },
    };
    const response = await fetch("/api/filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Speichern fehlgeschlagen.");
    showFilters(data);
    clearToast();
    filterStatus.textContent += " · gespeichert";
  } catch (error) {
    showToast(error.message || "Filter speichern fehlgeschlagen.");
  } finally {
    filtersSave.disabled = false;
  }
}

async function resetFilters() {
  filtersReset.disabled = true;
  try {
    const response = await fetch("/api/filters/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Reset fehlgeschlagen.");
    showFilters(data);
    clearToast();
    filterStatus.textContent += " · Standard wiederhergestellt";
  } catch (error) {
    showToast(error.message || "Reset fehlgeschlagen.");
  } finally {
    filtersReset.disabled = false;
  }
}

filtersSave.addEventListener("click", saveFilters);
filtersReset.addEventListener("click", resetFilters);
loadFilters();
