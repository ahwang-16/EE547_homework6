/**
 * ArXiv Paper Browser — Client-Side Interactions
 *
 * This script adds dynamic behavior to the server-rendered pages.
 * Each feature checks for the presence of its target elements
 * and skips initialization if they are not on the current page.
 *
 * Expected elements:
 *   #stats-box         — server status (all pages, in base template)
 *   #auth-user         — username input
 *   #auth-pass         — password input
 *   #auth-login        — login button
 *   #auth-status       — login status text
 *   #category-select   — category dropdown (papers page)
 *   #paper-list        — paper card container (papers page)
 *   #search-input      — keyword input (search page)
 *   #search-results    — search results container (search page)
 *   [data-arxiv-id]    — paper cards (any page, for detail expansion)
 */

// ---- State ----

let authToken = null;
let authUser = null;

// ---- API ----

async function apiFetch(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (authToken) {
    headers["Authorization"] = "Bearer " + authToken;
  }
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    throw new Error(resp.status + " " + resp.statusText);
  }
  return resp.json();
}

// ---- Stats ----

async function loadStats() {
  const box = document.getElementById("stats-box");
  if (!box) { alert("app.js: missing #stats-box element"); return; }
  try {
    const s = await apiFetch("/api/stats");
    const mins = Math.floor(s.uptime_seconds / 60);
    box.innerHTML =
      "<small>" +
      s.region + " &middot; " + s.table +
      " &middot; uptime " + mins + "m" +
      " &middot; " + s.requests.total + " requests" +
      "</small>";
  } catch (_) {
    box.innerHTML = "<small>API unavailable</small>";
  }
}

// ---- Auth ----

function initAuth() {
  const btn = document.getElementById("auth-login");
  if (!btn) { alert("app.js: missing #auth-login element"); return; }

  btn.addEventListener("click", async function () {
    const user = document.getElementById("auth-user").value;
    const pass = document.getElementById("auth-pass").value;
    const status = document.getElementById("auth-status");

    try {
      const data = await apiFetch("/api/login", {
        method: "POST",
        body: JSON.stringify({ username: user, password: pass }),
      });
      authToken = data.token;
      authUser = user;
      status.textContent =
        "Logged in as " + user + " (" + authToken.slice(0, 12) + "\u2026)";
      status.style.color = "green";
    } catch (_) {
      authToken = null;
      authUser = null;
      status.textContent = "Login failed";
      status.style.color = "red";
    }
  });
}

// ---- Paper Card Rendering ----

function renderPaperCard(paper) {
  const card = document.createElement("article");
  card.setAttribute("data-arxiv-id", paper.arxiv_id);
  card.style.cursor = "pointer";

  const title = document.createElement("strong");
  const link = document.createElement("a");
  link.href = "/papers/" + paper.arxiv_id;
  link.textContent = paper.title;
  title.appendChild(link);

  const authors = document.createElement("small");
  authors.textContent = (paper.authors || []).join(", ");

  const date = document.createElement("small");
  date.textContent = paper.published || "";

  const detail = document.createElement("div");
  detail.className = "paper-detail";
  detail.style.display = "none";

  card.appendChild(title);
  card.appendChild(document.createElement("br"));
  card.appendChild(authors);
  card.appendChild(document.createTextNode(" \u00b7 "));
  card.appendChild(date);
  card.appendChild(detail);

  return card;
}

function renderPaperCards(container, papers) {
  container.innerHTML = "";
  if (!papers || papers.length === 0) {
    container.innerHTML = "<p>No papers found.</p>";
    return;
  }
  papers.forEach(function (p) {
    container.appendChild(renderPaperCard(p));
  });
}

// ---- Category Browse ----

function initCategoryBrowse() {
  const select = document.getElementById("category-select");
  const list = document.getElementById("paper-list");
  if (!select || !list) return;

  select.addEventListener("change", async function () {
    if (!authToken) {
      list.innerHTML = "<p>Login to browse by category.</p>";
      return;
    }
    list.innerHTML = "<p>Loading\u2026</p>";
    try {
      const data = await apiFetch(
        "/api/papers?category=" +
          encodeURIComponent(select.value) +
          "&limit=20"
      );
      renderPaperCards(list, data.papers);
    } catch (err) {
      list.innerHTML = "<p>Error: " + err.message + "</p>";
    }
  });
}

// ---- Keyword Search ----

function initSearch() {
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  if (!input || !results) return;

  let timer = null;
  input.addEventListener("input", function () {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      return;
    }
    if (!authToken) {
      results.innerHTML = "<p>Login to search.</p>";
      return;
    }
    timer = setTimeout(async function () {
      results.innerHTML = "<p>Searching\u2026</p>";
      try {
        const data = await apiFetch(
          "/api/papers/keyword/" + encodeURIComponent(q) + "?limit=20"
        );
        renderPaperCards(results, data.papers);
      } catch (err) {
        results.innerHTML = "<p>Error: " + err.message + "</p>";
      }
    }, 300);
  });
}

// ---- Paper Detail Expand ----

function initPaperExpand() {
  document.addEventListener("click", async function (e) {
    // ignore clicks on links
    if (e.target.tagName === "A") return;

    var card = e.target.closest("[data-arxiv-id]");
    if (!card) return;

    // find or create detail container
    var detail = card.querySelector(".paper-detail");
    if (!detail) {
      detail = document.createElement("div");
      detail.className = "paper-detail";
      detail.style.display = "none";
      card.appendChild(detail);
    }

    // toggle if already loaded
    if (detail.dataset.loaded) {
      detail.style.display =
        detail.style.display === "none" ? "block" : "none";
      return;
    }

    if (!authToken) return;

    var arxivId = card.getAttribute("data-arxiv-id");
    detail.innerHTML = "<small>Loading\u2026</small>";
    detail.style.display = "block";

    try {
      var paper = await apiFetch(
        "/api/papers/" + encodeURIComponent(arxivId)
      );
      detail.innerHTML =
        "<hr>" +
        "<p>" + (paper.abstract || "No abstract available.") + "</p>" +
        "<small>Categories: " +
        (paper.categories || []).join(", ") +
        "</small>";
      detail.dataset.loaded = "true";
    } catch (_) {
      detail.innerHTML = "<small>Could not load details.</small>";
    }
  });
}

// ---- Init ----

document.addEventListener("DOMContentLoaded", function () {
  loadStats();
  initAuth();
  initCategoryBrowse();
  initSearch();
  initPaperExpand();
});
