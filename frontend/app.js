/* ===========================================================================
   StartupPulse AI — trend discovery page.

   No framework and no build step, so the structure carries the discipline
   instead: one thin API client, small render functions named after the
   components they draw, and a single `state` object that every render reads
   from. Nothing writes to the DOM outside a render function.

   There is no mock data anywhere. Every number on the page comes from the
   backend, and when the backend has said nothing yet the page shows an empty
   state rather than a plausible-looking placeholder.
   ========================================================================= */

(function () {
  "use strict";

  // -- Where the backend lives --------------------------------------------
  // Set in config.js. An empty string means "same origin", which is what you
  // want when a server hosts this page and the API together. Any other value
  // is used verbatim, which is what you want when this folder is served on its
  // own port - the backend allows cross-origin calls.
  var API_BASE = String(window.STARTUPPULSE_API_BASE || "").replace(/\/+$/, "");

  // -- API client ---------------------------------------------------------
  // The one place that knows about URLs, verbs and error shapes.
  const api = {
    async request(path, options) {
      let response;
      try {
        response = await fetch(API_BASE + path, options);
      } catch (cause) {
        // Network-level: server down, DNS, offline.
        throw new ApiError("Unable to reach the API.", cause);
      }

      if (!response.ok) {
        // The backend's detail is read for the console, never rendered raw.
        let detail = "";
        try {
          detail = (await response.json()).detail || "";
        } catch (_) { /* body was not JSON */ }
        throw new ApiError(messageForStatus(response.status), detail);
      }

      return response.json();
    },

    health() {
      return this.request("/api/trends/health");
    },

    rawTrends(region, limit) {
      const query = new URLSearchParams({ region: region, limit: String(limit) });
      return this.request("/api/trends?" + query.toString());
    },

    discover(region, limit) {
      return this.request("/api/trends/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ region: region, limit: limit }),
      });
    },
  };

  function ApiError(message, cause) {
    this.name = "ApiError";
    this.message = message;
    this.cause = cause;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function messageForStatus(status) {
    if (status === 503) return "The trend source is unavailable right now.";
    if (status === 422) return "That region or limit was rejected.";
    if (status === 429) return "Too many requests. Wait a moment and try again.";
    if (status >= 500) return "The server had a problem completing the request.";
    return "The request could not be completed.";
  }

  // -- State --------------------------------------------------------------
  const state = {
    status: "idle",     // idle | loading | ready | empty | error
    trends: [],
    collected: 0,
    relevant: 0,
    average: null,
    updatedAt: null,
    aiUsed: null,
    note: "",
    errorMessage: "",
  };

  const el = {
    controls: document.getElementById("controls"),
    region: document.getElementById("region"),
    limit: document.getElementById("limit"),
    refresh: document.getElementById("refresh"),
    results: document.getElementById("results"),
    note: document.getElementById("results-note"),
    apiStatus: document.getElementById("api-status"),
    metrics: {
      total: document.getElementById("m-total"),
      relevant: document.getElementById("m-relevant"),
      average: document.getElementById("m-average"),
      updated: document.getElementById("m-updated"),
    },
  };

  // -- Helpers ------------------------------------------------------------
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function sourceLabel(source) {
    return source === "google_trends" ? "Google Trends" : String(source || "Unknown");
  }

  // A word alongside the number, so relevance never depends on colour alone.
  function scoreBand(score) {
    if (score >= 90) return "Critical";
    if (score >= 80) return "High";
    if (score >= 70) return "Moderate";
    return "Low";
  }

  function relativeTime(date) {
    if (!date) return "Never";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 10) return "Just now";
    if (seconds < 60) return seconds + " seconds ago";
    const minutes = Math.round(seconds / 60);
    if (minutes === 1) return "1 minute ago";
    if (minutes < 60) return minutes + " minutes ago";
    const hours = Math.round(minutes / 60);
    if (hours === 1) return "1 hour ago";
    if (hours < 24) return hours + " hours ago";
    return date.toLocaleDateString();
  }

  // -- Components ---------------------------------------------------------
  // TrendItem
  function TrendItem(trend, index) {
    const score = Math.round(Number(trend.relevance_score) || 0);
    const rank = String(index + 1).padStart(2, "0");

    return (
      '<li class="trend">' +
        '<div class="trend__rank" aria-hidden="true">' + rank + "</div>" +
        '<div class="trend__main">' +
          '<p class="trend__topic">' +
            '<span class="sr-rank">' + rank + ". </span>" +
            escapeHtml(trend.topic) +
          "</p>" +
          '<div class="trend__meta">' +
            '<span class="tag tag--accent">' + escapeHtml(trend.category) + "</span>" +
            '<span class="trend__source">' + escapeHtml(sourceLabel(trend.source)) + "</span>" +
          "</div>" +
          '<p class="trend__reason">' + escapeHtml(trend.reason) + "</p>" +
        "</div>" +
        '<div class="score">' +
          '<div class="score__value">' + score +
            '<span class="score__max"> / 100</span>' +
          "</div>" +
          '<div class="score__track" role="img" aria-label="Relevance ' + score +
            ' out of 100, ' + scoreBand(score) + '">' +
            '<div class="score__fill" style="width:' + score + '%"></div>' +
          "</div>" +
          '<span class="score__band">' + scoreBand(score) + "</span>" +
        "</div>" +
      "</li>"
    );
  }

  // TrendList
  function TrendList(trends) {
    return '<ol class="trend-list">' + trends.map(TrendItem).join("") + "</ol>";
  }

  // TrendSkeleton
  function TrendSkeleton() {
    let rows = "";
    for (let i = 0; i < 5; i++) {
      rows +=
        '<div class="skeleton-row">' +
          '<div class="shimmer" style="width:22px"></div>' +
          "<div>" +
            '<div class="shimmer" style="width:' + (38 + i * 9) + '%;height:14px"></div>' +
            '<div class="shimmer" style="width:26%;margin-top:10px"></div>' +
            '<div class="shimmer" style="width:' + (72 - i * 5) + '%;margin-top:10px"></div>' +
          "</div>" +
          '<div class="shimmer" style="height:34px"></div>' +
        "</div>";
    }

    return (
      '<div class="trend-list">' +
        '<div class="loading-note">' +
          '<p class="loading-note__title">Analyzing market signals…</p>' +
          '<p class="loading-note__sub">Collecting trending topics and identifying startup relevance.</p>' +
        "</div>" +
        rows +
      "</div>"
    );
  }

  // TrendEmptyState
  function TrendEmptyState() {
    return (
      '<div class="state">' +
        '<p class="state__title">No relevant startup trends detected.</p>' +
        '<p class="state__body">Try another region or refresh the latest trend data.</p>' +
        '<div class="state__actions">' +
          '<button class="btn btn--secondary" type="button" data-action="retry">Refresh Trends</button>' +
        "</div>" +
      "</div>"
    );
  }

  // The state before anything has been requested.
  function TrendIdleState() {
    return (
      '<div class="state">' +
        '<p class="state__title">No analysis run yet.</p>' +
        '<p class="state__body">Choose a region and select Refresh Trends to collect ' +
          "trending topics and score them for startup relevance.</p>" +
        '<div class="state__actions">' +
          '<button class="btn btn--primary" type="button" data-action="retry">Refresh Trends</button>' +
        "</div>" +
      "</div>"
    );
  }

  // TrendErrorState — shows our own message, never the backend's raw error.
  function TrendErrorState(message) {
    return (
      '<div class="state state--error">' +
        '<p class="state__title">Unable to analyze trend data.</p>' +
        '<p class="state__body">' + escapeHtml(message || "Please try again.") + "</p>" +
        '<div class="state__actions">' +
          '<button class="btn btn--primary" type="button" data-action="retry">Retry</button>' +
        "</div>" +
      "</div>"
    );
  }

  // TrendMetrics
  function renderMetrics() {
    el.metrics.total.textContent = state.collected ? String(state.collected) : "—";
    el.metrics.relevant.textContent =
      state.status === "ready" || state.status === "empty" ? String(state.relevant) : "—";
    el.metrics.average.textContent =
      state.average == null ? "—" : Math.round(state.average) + "%";
    el.metrics.updated.textContent = relativeTime(state.updatedAt);
  }

  // -- Render -------------------------------------------------------------
  function render() {
    renderMetrics();

    el.note.textContent = state.note;
    el.results.setAttribute("aria-busy", state.status === "loading" ? "true" : "false");

    if (state.status === "loading") {
      el.results.innerHTML = TrendSkeleton();
    } else if (state.status === "error") {
      el.results.innerHTML = TrendErrorState(state.errorMessage);
    } else if (state.status === "empty") {
      el.results.innerHTML = TrendEmptyState();
    } else if (state.status === "ready") {
      el.results.innerHTML = TrendList(state.trends);
    } else {
      el.results.innerHTML = TrendIdleState();
    }

    el.refresh.disabled = state.status === "loading";
    el.refresh.classList.toggle("is-busy", state.status === "loading");
    el.refresh.querySelector(".btn__label").textContent =
      state.status === "loading" ? "Analyzing…" : "Refresh Trends";
  }

  // -- Actions ------------------------------------------------------------
  function currentFilters() {
    return {
      region: el.region.value,
      limit: parseInt(el.limit.value, 10) || 20,
    };
  }

  async function discover() {
    const filters = currentFilters();

    state.status = "loading";
    state.note = "";
    render();

    try {
      const data = await api.discover(filters.region, filters.limit);
      const trends = Array.isArray(data.trends) ? data.trends : [];

      state.trends = trends;
      state.collected = data.total_trends_collected || 0;
      state.relevant = data.total_relevant_trends || 0;
      state.aiUsed = data.ai_used;
      state.updatedAt = data.generated_at ? new Date(data.generated_at) : new Date();

      // The backend has no average field; it is the mean of what it returned.
      state.average = trends.length
        ? trends.reduce(function (sum, t) { return sum + (Number(t.relevance_score) || 0); }, 0) /
          trends.length
        : null;

      state.status = trends.length ? "ready" : "empty";
      state.note = noteFor(data, filters);
    } catch (error) {
      console.error("trend discovery failed:", error);
      state.status = "error";
      state.errorMessage =
        error && error.name === "ApiError" ? error.message : "Please try again.";
    }

    render();
  }

  function noteFor(data, filters) {
    const parts = [
      filters.region === "GLOBAL" ? "Global" : "India",
      data.total_trends_collected + " collected",
      data.total_relevant_trends + " relevant",
    ];
    // Say so plainly when the LLM was skipped and rules decided instead.
    if (data.ai_used === false) parts.push("scored by rules (AI unavailable)");
    return parts.join(" · ");
  }

  // A free look at what is trending: no LLM, so it costs nothing. It fills
  // the "collected" metric on load without pretending anything was analyzed.
  async function loadRawPreview() {
    const filters = currentFilters();
    try {
      const data = await api.rawTrends(filters.region, filters.limit);
      if (state.status !== "idle") return;   // a discovery already answered
      state.collected = data.total || 0;
      state.note = data.total
        ? data.total + " trending topics available · not analyzed yet"
        : "";
      render();
    } catch (error) {
      // Non-fatal: the page still works, the user just sees no preview.
      console.warn("raw trend preview unavailable:", error);
    }
  }

  async function checkHealth() {
    try {
      await api.health();
      setApiStatus("ok", "API connected");
    } catch (error) {
      setApiStatus("down", "API unreachable");
    }
  }

  function setApiStatus(stateName, text) {
    el.apiStatus.dataset.state = stateName;
    el.apiStatus.querySelector(".api-status__text").textContent = text;
  }

  // -- Wiring -------------------------------------------------------------
  el.controls.addEventListener("submit", function (event) {
    event.preventDefault();
    discover();
  });

  // Retry / refresh buttons inside the rendered states.
  el.results.addEventListener("click", function (event) {
    const button = event.target.closest("[data-action='retry']");
    if (button) discover();
  });

  // Changing a filter invalidates what is on screen, so go back to idle and
  // re-preview rather than leaving stale results under new filter labels.
  [el.region, el.limit].forEach(function (control) {
    control.addEventListener("change", function () {
      state.status = "idle";
      state.trends = [];
      state.collected = 0;
      state.relevant = 0;
      state.average = null;
      state.updatedAt = null;
      state.note = "";
      render();
      loadRawPreview();
    });
  });

  // "2 minutes ago" has to keep counting.
  setInterval(function () {
    if (state.updatedAt) el.metrics.updated.textContent = relativeTime(state.updatedAt);
  }, 30000);

  render();
  checkHealth();
  loadRawPreview();
})();
