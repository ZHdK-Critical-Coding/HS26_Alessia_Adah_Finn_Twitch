/* global Twitch */
const statusBox = document.querySelector("#status");
const statusText = document.querySelector("#status-text");
const slotA = document.querySelector("#player-a");
const slotB = document.querySelector("#player-b");

// Kein Auto-Skip mehr: sichtbarer Stream wechselt NUR bei Enter.
const AD_POLL_MS = 2000;
const UNMUTE_WATCH_MS = 250;
const STORAGE_UNLOCK = "twitchNullAudioUnlocked";

let current = null;
let busy = false;
let playWatchTimer = null;
let adPollTimer = null;
let unmuteWatchTimer = null;
let sawPlaying = false;
let pausedSince = null;
let skipReason = null;
/** User-Geste in diesem Page-Load. */
let pageGesture = false;
let lastMuteLog = "";

/** Zwei Embeds: einer sichtbar, einer unsichtbar vorgeladen. */
let activeSlot = slotA;
let preloadSlot = slotB;
let activePlayer = null;
let preloadPlayer = null;
let preloadStream = null;
let preloadReady = false;
const boundPlayers = new WeakSet();

/** API-Kandidat (Login), bevor/ohne Player-Puffer. */
let prefetch = null;
let prefetchPromise = null;
let prefetchExclude = null;

function parentHosts() {
  return [...new Set([location.hostname, "localhost", "127.0.0.1"])];
}

function readMuteState() {
  if (!activePlayer) return { muted: null, vol: null };
  let muted = null;
  let vol = null;
  try {
    muted = activePlayer.getMuted();
  } catch (_) {
    /* ignore */
  }
  try {
    vol = activePlayer.getVolume();
  } catch (_) {
    /* ignore */
  }
  return { muted, vol };
}

function audioIsOk(state) {
  return (
    state.muted === false &&
    typeof state.vol === "number" &&
    state.vol > 0.05
  );
}

function updateBadge(tag) {
  const state = readMuteState();
  const { muted, vol } = state;
  const line = `${tag} gesture=${pageGesture} muted=${muted} vol=${vol} ch=${current && current.login} pre=${preloadStream && preloadStream.login}${preloadReady ? "*" : ""}`;
  if (line !== lastMuteLog) {
    console.info(`[audio] ${line}`);
    lastMuteLog = line;
  }
  if (pageGesture && audioIsOk(state)) hideUnlockGate();
  else if (activePlayer) ensureUnlockGate();
}

function showStatus(message) {
  statusText.textContent = message;
  statusBox.hidden = false;
}

function hideStatus() {
  statusBox.hidden = true;
}

function clearWatchdogs() {
  if (playWatchTimer) {
    clearTimeout(playWatchTimer);
    playWatchTimer = null;
  }
  pausedSince = null;
}

function hideUnlockGate() {
  const gate = document.querySelector("#unlock-gate");
  if (gate) gate.remove();
}

function forceMutePlayer(player) {
  if (!player) return;
  try {
    player.setMuted(true);
    player.setVolume(0);
  } catch (_) {
    /* ignore */
  }
}

function forceUnmuteRaw(reason) {
  if (!activePlayer || !pageGesture) {
    updateBadge(`skip(${reason})`);
    return false;
  }
  try {
    activePlayer.setMuted(false);
    activePlayer.setVolume(1);
    activePlayer.setMuted(false);
    activePlayer.setVolume(1);
    if (typeof activePlayer.play === "function") activePlayer.play();
  } catch (err) {
    console.warn("[audio] forceUnmuteRaw", reason, err);
  }
  updateBadge(`force(${reason})`);
  return audioIsOk(readMuteState());
}

function scheduleUnmuteBurst(reason) {
  if (!pageGesture) return;
  const delays = [0, 0, 50, 100, 200, 400, 800, 1600, 3200];
  delays.forEach((ms, i) => {
    setTimeout(() => forceUnmuteRaw(`${reason}#${i}`), ms);
  });
}

function startUnmuteWatchdog() {
  if (unmuteWatchTimer) return;
  unmuteWatchTimer = setInterval(() => {
    if (!activePlayer) return;
    forceMutePlayer(preloadPlayer);
    if (!pageGesture) {
      updateBadge("watch");
      return;
    }
    if (!audioIsOk(readMuteState())) forceUnmuteRaw("watchdog");
    else updateBadge("watch-ok");
  }, UNMUTE_WATCH_MS);
}

function playerOptions(channel) {
  return {
    channel,
    width: "100%",
    height: "100%",
    parent: parentHosts(),
    autoplay: true,
    muted: true,
    controls: false,
  };
}

function bindPlayerRoleListeners(player) {
  if (!player || boundPlayers.has(player)) return;
  boundPlayers.add(player);

  const onActiveKick = (ev) => {
    if (player !== activePlayer) return;
    if (ev === "PLAYING" || ev === "ONLINE") {
      sawPlaying = true;
      pausedSince = null;
      clearTimeout(playWatchTimer);
      playWatchTimer = null;
    }
    updateBadge(ev);
    if (pageGesture) scheduleUnmuteBurst(ev);
  };

  const onPreloadReady = () => {
    if (player !== preloadPlayer) return;
    preloadReady = true;
    console.info("[preload] PLAYING/READY", preloadStream && preloadStream.login);
    forceMutePlayer(player);
  };

  player.addEventListener(Twitch.Player.READY, () => {
    onActiveKick("READY");
    onPreloadReady();
  });
  player.addEventListener(Twitch.Player.PLAYING, () => {
    onActiveKick("PLAYING");
    onPreloadReady();
  });
  player.addEventListener(Twitch.Player.PLAY, () => onActiveKick("PLAY"));
  player.addEventListener(Twitch.Player.ONLINE, () => {
    onActiveKick("ONLINE");
    onPreloadReady();
  });
  player.addEventListener(Twitch.Player.OFFLINE, () => {
    if (player === activePlayer) {
      // Sichtbar bleibt — kein Auto-Wechsel ohne Enter.
      console.warn("[live] OFFLINE (warte auf Enter)", current && current.login);
      updateBadge("OFFLINE");
      return;
    }
    if (player === preloadPlayer) {
      console.warn("[preload] OFFLINE — verwerfen", preloadStream && preloadStream.login);
      preloadStream = null;
      preloadReady = false;
      prefetch = null;
      warmPrefetch();
    }
  });
  player.addEventListener(Twitch.Player.PAUSE, () => {
    if (player !== activePlayer || !sawPlaying) return;
    pausedSince = Date.now();
    updateBadge("PAUSE");
    if (pageGesture) scheduleUnmuteBurst("PAUSE");
  });
  if (Twitch.Player.PLAYBACK_BLOCKED) {
    player.addEventListener(Twitch.Player.PLAYBACK_BLOCKED, () => {
      if (player !== activePlayer) return;
      console.warn("[audio] PLAYBACK_BLOCKED (kein Remute)");
      updateBadge("PLAYBACK_BLOCKED");
      ensureUnlockGate();
      if (pageGesture) scheduleUnmuteBurst("after-blocked");
    });
  }
}

function applySlotClasses() {
  activeSlot.classList.add("is-active");
  activeSlot.classList.remove("is-preload");
  activeSlot.removeAttribute("aria-hidden");
  preloadSlot.classList.add("is-preload");
  preloadSlot.classList.remove("is-active");
  preloadSlot.setAttribute("aria-hidden", "true");
}

function createPlayerIn(slotEl, channel) {
  if (typeof Twitch === "undefined" || !Twitch.Player) {
    throw new Error("Twitch Embed-Skript nicht geladen.");
  }
  slotEl.innerHTML = "";
  return new Twitch.Player(slotEl.id, playerOptions(channel));
}

function ensureActivePlayer(channel) {
  if (activePlayer) {
    console.info("[audio] setChannel active", channel);
    activePlayer.setChannel(channel);
    if (pageGesture) scheduleUnmuteBurst("setChannel");
    return activePlayer;
  }
  activePlayer = createPlayerIn(activeSlot, channel);
  bindPlayerRoleListeners(activePlayer);
  if (!adPollTimer) adPollTimer = setInterval(pollAdOrLockHeuristics, AD_POLL_MS);
  startUnmuteWatchdog();
  updateBadge("built-active");
  return activePlayer;
}

function ensurePreloadPlayer(stream) {
  if (!stream || !stream.login) return;
  if (
    current &&
    stream.login.toLowerCase() === current.login.toLowerCase()
  ) {
    return;
  }
  const same =
    preloadStream &&
    preloadStream.login &&
    preloadStream.login.toLowerCase() === stream.login.toLowerCase();
  preloadStream = stream;
  if (same && preloadPlayer) {
    forceMutePlayer(preloadPlayer);
    return;
  }
  preloadReady = false;
  if (preloadPlayer) {
    console.info("[preload] setChannel", stream.login);
    preloadPlayer.setChannel(stream.login);
  } else {
    console.info("[preload] build", stream.login);
    preloadPlayer = createPlayerIn(preloadSlot, stream.login);
    bindPlayerRoleListeners(preloadPlayer);
  }
  forceMutePlayer(preloadPlayer);
  // Nach setChannel erneut muten (Embed startet manchmal laut).
  [0, 50, 200, 500, 1000].forEach((ms) => {
    setTimeout(() => forceMutePlayer(preloadPlayer), ms);
  });
}

function swapPreloadToActive() {
  if (!preloadPlayer || !preloadStream) return false;

  const oldActivePlayer = activePlayer;
  const oldActiveSlot = activeSlot;
  const nextStream = preloadStream;

  // Rollen tauschen — Preload wird sichtbar, alter Slot wird neuer Preload.
  activePlayer = preloadPlayer;
  preloadPlayer = oldActivePlayer;
  activeSlot = preloadSlot;
  preloadSlot = oldActiveSlot;
  applySlotClasses();

  current = nextStream;
  preloadStream = null;
  preloadReady = false;
  forceMutePlayer(preloadPlayer);
  if (pageGesture) scheduleUnmuteBurst("swap");
  else forceMutePlayer(activePlayer);

  console.info("[preload] SWAP →", current.login);
  return true;
}

function unlockAudio(source) {
  pageGesture = true;
  sessionStorage.setItem(STORAGE_UNLOCK, "1");
  console.info("[audio] UNLOCK via", source);
  forceMutePlayer(preloadPlayer);
  scheduleUnmuteBurst(source || "gesture");
  startUnmuteWatchdog();
}

function ensureUnlockGate() {
  if (document.querySelector("#unlock-gate")) return;
  if (pageGesture && audioIsOk(readMuteState())) return;

  const gate = document.createElement("button");
  gate.id = "unlock-gate";
  gate.type = "button";
  gate.setAttribute("aria-label", "Ton einschalten — klicken");
  const handler = () => unlockAudio("unlock-gate");
  gate.addEventListener("pointerdown", handler);
  gate.addEventListener("click", handler);
  document.body.appendChild(gate);
}

function schedulePlayWatch(login) {
  clearWatchdogs();
  sawPlaying = false;
  // Früher: Timeout → Auto-Skip. Jetzt nur Status; Wechsel nur per Enter.
  playWatchTimer = null;
  void login;
}

function pollAdOrLockHeuristics() {
  if (!activePlayer || !current || busy) return;
  forceMutePlayer(preloadPlayer);
  // Nur Audio halten — keine Auto-Skips bei Ads/Qualities/Pause.
  if (pageGesture && !audioIsOk(readMuteState())) forceUnmuteRaw("ad-poll");
}

function adoptOnServer(stream) {
  if (!stream || !stream.login) return;
  fetch("/api/current", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(stream),
    cache: "no-store",
  }).catch(() => {});
}

function showStream(stream, opts) {
  const options = opts || {};
  current = stream;
  hideStatus();
  if (!options.fromSwap) {
    ensureActivePlayer(stream.login);
  }
  schedulePlayWatch(stream.login);
  ensureUnlockGate();
  if (pageGesture) scheduleUnmuteBurst("showStream");
  adoptOnServer(stream);
  prefetch = null;
  preloadStream = null;
  preloadReady = false;
  warmPrefetch();
}

async function fetchRandom(exclude, reason, prefetchOnly) {
  const params = new URLSearchParams();
  if (exclude) params.set("exclude", exclude);
  if (reason) params.set("reason", reason);
  if (prefetchOnly) params.set("prefetch", "1");
  const qs = params.toString() ? `?${params}` : "";
  const response = await fetch(`/api/random${qs}`, { cache: "no-store" });
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) throw new Error(data.error || "Der Stream konnte nicht geladen werden.");
  if (data.viewerCount !== 0 || !data.login) {
    throw new Error("Der Stream hat nicht genau 0 Zuschauer*innen.");
  }
  return data;
}

function warmPrefetch() {
  const exclude = current && current.login ? current.login : null;
  if (prefetchPromise && prefetchExclude === exclude) return;
  prefetchExclude = exclude;
  prefetchPromise = fetchRandom(exclude, null, true)
    .then((data) => {
      if (current && data.login.toLowerCase() === current.login.toLowerCase()) {
        prefetch = null;
        return null;
      }
      prefetch = data;
      console.info("[prefetch] api ready", data.login);
      ensurePreloadPlayer(data);
      return data;
    })
    .catch((err) => {
      console.warn("[prefetch]", err && err.message ? err.message : err);
      prefetch = null;
      return null;
    })
    .finally(() => {
      prefetchPromise = null;
    });
}

function takePrefetch(previous) {
  if (preloadStream && preloadStream.login) {
    if (
      previous &&
      previous.login &&
      preloadStream.login.toLowerCase() === previous.login.toLowerCase()
    ) {
      return null;
    }
    const data = preloadStream;
    return data;
  }
  if (!prefetch || !prefetch.login) return null;
  if (
    previous &&
    previous.login &&
    prefetch.login.toLowerCase() === previous.login.toLowerCase()
  ) {
    return null;
  }
  const data = prefetch;
  prefetch = null;
  return data;
}

async function loadNext(opts) {
  const options = opts || {};
  if (busy) return;
  const previous = current;
  busy = true;
  if (!previous && !options.silentStatus) {
    showStatus("Suche einen Livestream mit 0 Zuschauer*innen …");
  }
  try {
    // Schnellster Weg: unsichtbarer Player schon am Puffern → CSS-Swap.
    if (
      previous &&
      preloadPlayer &&
      preloadStream &&
      preloadStream.login &&
      preloadStream.login.toLowerCase() !== previous.login.toLowerCase()
    ) {
      skipReason = null;
      prefetch = null;
      if (swapPreloadToActive()) {
        showStream(current, { fromSwap: true });
        return;
      }
    }

    let data = takePrefetch(previous);
    if (!data && prefetchPromise) {
      try {
        await Promise.race([
          prefetchPromise,
          new Promise((resolve) => setTimeout(resolve, 400)),
        ]);
      } catch (_) {
        /* ignore */
      }
      data = takePrefetch(previous);
    }
    if (!data) {
      const reason = skipReason;
      skipReason = null;
      data = await fetchRandom(previous && previous.login, reason, false);
    } else {
      skipReason = null;
    }
    if (
      previous &&
      data.login &&
      data.login.toLowerCase() === previous.login.toLowerCase()
    ) {
      throw new Error("Es wurde derselbe Stream erneut geliefert.");
    }
    // Falls Prefetch-Player schon auf data zeigt: trotzdem Swap bevorzugen.
    if (
      preloadPlayer &&
      preloadStream &&
      preloadStream.login.toLowerCase() === data.login.toLowerCase()
    ) {
      if (swapPreloadToActive()) {
        showStream(current, { fromSwap: true });
        return;
      }
    }
    showStream(data);
  } catch (error) {
    if (previous) console.warn(error.message || error);
    else {
      showStatus(error.message || "Der Stream konnte nicht geladen werden.");
      current = null;
    }
  } finally {
    busy = false;
    if (!prefetch && !prefetchPromise && !preloadStream) warmPrefetch();
  }
}

document.addEventListener("keydown", (event) => {
  if (event.repeat) return;
  if (!pageGesture || !audioIsOk(readMuteState())) unlockAudio("keydown");
  if (event.key === "Enter" || event.code === "Enter" || event.code === "NumpadEnter") {
    event.preventDefault();
    clearWatchdogs();
    loadNext();
  }
});

["pointerdown", "click", "touchstart"].forEach((type) => {
  document.addEventListener(
    type,
    (event) => {
      if (event.target && event.target.id === "unlock-gate") return;
      if (!pageGesture || !audioIsOk(readMuteState())) unlockAudio(type);
    },
    { capture: true, passive: true }
  );
});

function boot() {
  if (typeof Twitch === "undefined") {
    showStatus("Twitch-Player wird geladen …");
    setTimeout(boot, 200);
    return;
  }
  document.querySelectorAll("#audio-debug").forEach((el) => el.remove());
  applySlotClasses();
  ensureUnlockGate();
  startUnmuteWatchdog();
  if (sessionStorage.getItem(STORAGE_UNLOCK) === "1") {
    pageGesture = true;
  }
  // Erster Stream einmalig; danach nur noch Enter wechselt sichtbar.
  loadNext();
}

boot();
