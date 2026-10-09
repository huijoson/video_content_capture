"use strict";

// Classic scripts share one global scope, so keep these names out of workspace.js's way.
(() => {
  // Narrow windows (spec section 3): 字幕／問答 tabs and a folded library. Panels are only
  // hidden with CSS, so drafts, selections and the playback position survive tab switches.
  const element = (id) => document.getElementById(id);
  const narrowQuery = window.matchMedia("(max-width: 60rem)");
  const narrowTabs = [element("tab-subtitles"), element("tab-qa")];

  function selectNarrowTab(tab, focus = false) {
    element("workspace").dataset.narrowTab = tab === element("tab-qa") ? "qa" : "subtitles";
    for (const candidate of narrowTabs) {
      const selected = candidate === tab;
      candidate.setAttribute("aria-selected", String(selected));
      candidate.tabIndex = selected ? 0 : -1;
    }
    if (focus) tab.focus();
  }

  function applyNarrowLayout() {
    for (const tab of narrowTabs) {
      const panel = element(tab.getAttribute("aria-controls"));
      if (narrowQuery.matches) {
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tab.id);
      } else {
        panel.removeAttribute("role");
        panel.removeAttribute("aria-labelledby");
      }
    }
  }

  function setLibraryCollapsed(collapsed) {
    const library = document.querySelector(".library");
    library.dataset.collapsed = String(collapsed);
    element("library-toggle").setAttribute("aria-expanded", String(!collapsed));
    element("library-toggle").textContent = collapsed ? "展開影片庫" : "收合影片庫";
  }

  for (const tab of narrowTabs) {
    tab.addEventListener("click", () => selectNarrowTab(tab));
    tab.addEventListener("keydown", (event) => {
      const index = narrowTabs.indexOf(tab);
      const last = narrowTabs.length - 1;
      const moves = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: last - index };
      if (!(event.key in moves)) return;
      event.preventDefault();
      const next = (index + moves[event.key] + narrowTabs.length) % narrowTabs.length;
      selectNarrowTab(narrowTabs[next], true);
    });
  }
  // Citation links and the 無問答依據 shortcuts must reveal the panel they point into.
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.('a[href^="#"]');
    if (!link) return;
    const target = document.querySelector(link.getAttribute("href"));
    if (!target) return;
    const panel = target.closest("details");
    if (panel) panel.open = true;
    if (element("subtitle-panels").contains(target)) selectNarrowTab(narrowTabs[0]);
  });
  element("library-toggle").addEventListener("click", () => {
    setLibraryCollapsed(document.querySelector(".library").dataset.collapsed !== "true");
  });
  element("video-list").addEventListener("click", (event) => {
    if (narrowQuery.matches && event.target.closest("button, a")) setLibraryCollapsed(true);
  });
  narrowQuery.addEventListener("change", applyNarrowLayout);
  applyNarrowLayout();
})();
