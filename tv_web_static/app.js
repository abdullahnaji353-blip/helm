(() => {
  "use strict";

  const params = new URLSearchParams(location.search);
  let token = params.get("token");
  if (token) {
    localStorage.setItem("helmToken", token);
    history.replaceState({}, "", location.pathname);
  } else {
    token = localStorage.getItem("helmToken") || "";
  }

  const toastEl = document.getElementById("toast");
  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toastEl.classList.remove("show"), 2200);
  }

  async function api(path, body) {
    const url = new URL(path, location.origin);
    const res = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-Helm-Token": token },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) {
      toast("Not authorized — open the link Helm printed again.");
      throw new Error("unauthorized");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      toast(data.error || `Request failed (${res.status}).`);
      throw new Error(data.error || String(res.status));
    }
    return data;
  }

  // --- Clock ---
  const clockEl = document.getElementById("clock");
  function tickClock() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  tickClock();
  setInterval(tickClock, 15000);

  // --- Note ---
  const noteEl = document.getElementById("note");
  let noteDirty = false;
  noteEl.addEventListener("input", () => { noteDirty = true; });
  noteEl.addEventListener("blur", () => {
    if (!noteDirty) return;
    noteDirty = false;
    api("/api/note", { text: noteEl.value }).catch(() => {});
  });

  // --- Status / apps ---
  const statusDot = document.getElementById("status-dot");
  const deviceLabel = document.getElementById("device-label");
  const appGrid = document.getElementById("app-grid");
  const searchAppSelect = document.getElementById("search-app");
  let knownApps = [];

  function renderAppGrid(apps) {
    appGrid.innerHTML = "";
    apps.slice(0, 9).forEach((name) => {
      const btn = document.createElement("button");
      btn.textContent = name;
      btn.addEventListener("click", () => {
        api("/api/open", { name }).then(() => toast(`Opening ${name}…`)).catch(() => {});
      });
      appGrid.appendChild(btn);
    });
  }

  function renderSearchApps(apps) {
    searchAppSelect.innerHTML = "";
    apps.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      searchAppSelect.appendChild(opt);
    });
  }

  async function refreshStatus() {
    try {
      const data = await api("/api/status");
      statusDot.classList.toggle("online", !!data.connected);
      deviceLabel.textContent = data.connected ? data.device : "No TV connected";
      knownApps = data.apps || [];
      renderAppGrid(knownApps);
      renderSearchApps(data.searchApps || []);
      if (document.activeElement !== noteEl) noteEl.value = data.note || "";
    } catch {
      statusDot.classList.remove("online");
      deviceLabel.textContent = "Can't reach Helm";
    }
  }
  refreshStatus();
  setInterval(refreshStatus, 15000);

  // --- Key / D-pad buttons ---
  document.querySelectorAll("[data-key]").forEach((btn) => {
    btn.addEventListener("click", () => {
      api("/api/key", { key: btn.dataset.key }).catch(() => {});
    });
  });

  // --- Search ---
  document.getElementById("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const app = searchAppSelect.value;
    const query = document.getElementById("search-query").value.trim();
    if (!app || !query) return;
    api("/api/search", { app, query }).then(() => toast(`Searching ${app} for "${query}"…`)).catch(() => {});
  });

  // --- Volume ---
  document.getElementById("volume-slider").addEventListener("change", (event) => {
    api("/api/volume", { level: Number(event.target.value) }).catch(() => {});
  });

  // --- Power ---
  document.getElementById("wake-btn").addEventListener("click", () => {
    api("/api/wake").then(() => toast("Waking the TV…")).catch(() => {});
  });
  document.getElementById("sleep-btn").addEventListener("click", () => {
    api("/api/sleep").then(() => toast("Putting the TV to sleep…")).catch(() => {});
  });

  // --- Voice ("Hey Helm") ---
  const micBtn = document.getElementById("mic-btn");
  const micLabel = document.getElementById("mic-label");
  const micHint = document.getElementById("mic-hint");
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
  const WAKE_PHRASE = "hey helm";

  function runVoiceCommand(text) {
    const command = text.toLowerCase();
    if (command.includes("home")) return api("/api/key", { key: "home" });
    if (command.includes("back")) return api("/api/key", { key: "back" });
    if (command.includes("sleep")) return api("/api/sleep");
    if (command.includes("wake")) return api("/api/wake");
    if (command.includes("volume up")) return api("/api/key", { key: "volume_up" });
    if (command.includes("volume down")) return api("/api/key", { key: "volume_down" });
    const searchMatch = command.match(/search (\w[\w\s]*?) for (.+)/);
    if (searchMatch) return api("/api/search", { app: searchMatch[1].trim(), query: searchMatch[2].trim() });
    const openMatch = command.match(/open (.+)/);
    if (openMatch) {
      const name = openMatch[1].trim();
      const match = knownApps.find((app) => app.toLowerCase() === name) || name;
      return api("/api/open", { name: match });
    }
    toast(`Didn't understand: "${text}"`);
    return Promise.resolve();
  }

  if (!SpeechRecognitionImpl || !window.isSecureContext) {
    micBtn.disabled = true;
    micHint.textContent = !window.isSecureContext
      ? "Voice needs HTTPS — this page is loaded over plain HTTP, so your browser blocks the microphone here. Use the buttons above, or Helm's voice on the Mac."
      : "Voice isn't supported in this browser. Use the buttons above instead.";
  } else {
    let recognition = null;
    let listening = false;
    let awake = false;

    function start() {
      recognition = new SpeechRecognitionImpl();
      recognition.continuous = true;
      recognition.interimResults = false;
      recognition.lang = "en-US";
      recognition.onresult = (event) => {
        const transcript = event.results[event.results.length - 1][0].transcript.trim();
        if (!awake) {
          if (transcript.toLowerCase().includes(WAKE_PHRASE)) {
            awake = true;
            micBtn.classList.add("listening");
            micLabel.textContent = "Listening…";
            toast("Yes?");
            setTimeout(() => {
              if (awake) { awake = false; micBtn.classList.remove("listening"); micLabel.textContent = "Hey Helm"; }
            }, 6000);
          }
          return;
        }
        awake = false;
        micBtn.classList.remove("listening");
        micLabel.textContent = "Hey Helm";
        runVoiceCommand(transcript).catch(() => {});
      };
      recognition.onerror = () => { /* mic drops are common on phones; just keep listening */ };
      recognition.onend = () => { if (listening) recognition.start(); };
      recognition.start();
    }

    micBtn.disabled = false;
    micHint.textContent = 'Tap, say "Hey Helm", then your command — e.g. "open Netflix".';
    micBtn.addEventListener("click", () => {
      listening = !listening;
      if (listening) {
        start();
        micBtn.classList.add("listening");
        micLabel.textContent = "Listening for wake word…";
      } else {
        awake = false;
        if (recognition) recognition.stop();
        micBtn.classList.remove("listening");
        micLabel.textContent = "Hey Helm";
      }
    });
  }
})();
