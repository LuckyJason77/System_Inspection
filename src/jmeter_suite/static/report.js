/* 管理离线报告的筛选、复制、详情展开、批量操作和打印交互。 */

"use strict";

let activeSampleFilter = "all";
let activePeriodFilter = "all";
const printDetailState = new Map();
const BULK_DETAIL_BATCH_SIZE = 20;
let bulkDetailFrameId = null;
let bulkDetailOperation = 0;

function fallbackCopy(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.className = "copy-fallback";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  return copied;
}

async function copyRawText(button) {
  const targetId = button.dataset.copyTarget;
  const target = document.getElementById(targetId);
  if (!target) {
    return;
  }

  const text = target.textContent;
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      copied = true;
    }
  } catch (_error) {
    copied = false;
  }
  if (!copied) {
    copied = fallbackCopy(text);
  }

  const originalLabel = button.textContent;
  button.textContent = copied ? "已复制" : "复制失败";
  window.setTimeout(() => {
    button.textContent = originalLabel;
  }, 1200);
}

function collectDetailControls() {
  return Array.from(document.querySelectorAll("[data-detail-toggle]"))
    .map((button) => {
      const panelId = button.getAttribute("aria-controls");
      const panel = panelId ? document.getElementById(panelId) : null;
      if (!panel) {
        return null;
      }
      return {
        button,
        panel,
        label: button.querySelector("[data-toggle-label]"),
      };
    })
    .filter((control) => control !== null);
}

const detailControls = collectDetailControls();
const detailControlsByButton = new Map(
  detailControls.map((control) => [control.button, control]),
);
const allDetailsButton = document.querySelector("[data-all-details-toggle]");
const allDetailsLabel = allDetailsButton
  ? allDetailsButton.querySelector("[data-all-details-label]")
  : null;

function isDetailExpanded(control) {
  return control.button.getAttribute("aria-expanded") === "true";
}

function detailMatchesState(control, expanded) {
  return isDetailExpanded(control) === expanded
    && control.panel.hidden === !expanded;
}

function setDetailExpanded(control, expanded) {
  const ariaValue = expanded ? "true" : "false";
  if (control.button.getAttribute("aria-expanded") !== ariaValue) {
    control.button.setAttribute("aria-expanded", ariaValue);
  }
  if (control.panel.hidden !== !expanded) {
    control.panel.hidden = !expanded;
  }
  const labelText = expanded ? "收起" : "展开";
  if (control.label && control.label.textContent !== labelText) {
    control.label.textContent = labelText;
  }
}

function syncAllDetailsToggle() {
  if (!allDetailsButton) {
    return;
  }
  if (allDetailsButton.getAttribute("aria-busy") === "true") {
    allDetailsButton.disabled = true;
    return;
  }

  const anyExpanded = detailControls.some(isDetailExpanded);
  const shouldExpand = !anyExpanded;
  const labelText = shouldExpand ? "展开全部" : "收起全部";
  allDetailsButton.dataset.expand = shouldExpand ? "true" : "false";
  allDetailsButton.disabled = detailControls.length === 0;
  allDetailsButton.setAttribute("aria-label", labelText);
  if (allDetailsLabel) {
    allDetailsLabel.textContent = labelText;
  }
}

function setAllDetailsProgress(expanded, completed, total) {
  if (!allDetailsButton) {
    return;
  }
  const action = expanded ? "展开" : "收起";
  const progressText = `${action}中 ${completed}/${total}`;
  allDetailsButton.disabled = true;
  allDetailsButton.setAttribute("aria-busy", "true");
  allDetailsButton.setAttribute("aria-label", progressText);
  if (allDetailsLabel) {
    allDetailsLabel.textContent = progressText;
  }
}

function cancelBulkDetailUpdate() {
  bulkDetailOperation += 1;
  if (bulkDetailFrameId !== null) {
    cancelAnimationFrame(bulkDetailFrameId);
    bulkDetailFrameId = null;
  }
  if (allDetailsButton) {
    allDetailsButton.removeAttribute("aria-busy");
  }
  syncAllDetailsToggle();
}

function setAllDetailsExpanded(expanded) {
  cancelBulkDetailUpdate();
  const pendingControls = detailControls.filter(
    (control) => !detailMatchesState(control, expanded),
  );
  if (pendingControls.length === 0) {
    return;
  }

  const operation = ++bulkDetailOperation;
  let completed = 0;
  setAllDetailsProgress(expanded, completed, pendingControls.length);

  function processBatch() {
    if (operation !== bulkDetailOperation) {
      return;
    }
    const end = Math.min(
      completed + BULK_DETAIL_BATCH_SIZE,
      pendingControls.length,
    );
    for (; completed < end; completed += 1) {
      setDetailExpanded(pendingControls[completed], expanded);
    }
    setAllDetailsProgress(expanded, completed, pendingControls.length);

    if (completed < pendingControls.length) {
      bulkDetailFrameId = requestAnimationFrame(processBatch);
      return;
    }

    bulkDetailFrameId = null;
    if (allDetailsButton) {
      allDetailsButton.removeAttribute("aria-busy");
    }
    syncAllDetailsToggle();
  }

  bulkDetailFrameId = requestAnimationFrame(processBatch);
}

function sampleSearchQuery() {
  const search = document.querySelector("[data-sample-search]");
  return search ? search.value.trim().toLocaleLowerCase("zh-CN") : "";
}

function matchesPeriodFilter(item) {
  return (
    activePeriodFilter === "all"
    || item.dataset.periodAlwaysVisible === "true"
    || item.dataset.queryDataRange === activePeriodFilter
  );
}

function syncPeriodFilters() {
  document.querySelectorAll("[data-period-filter]").forEach((select) => {
    select.value = activePeriodFilter;
  });
}

function applyOverviewFilters() {
  let visibleCount = 0;
  const rows = document.querySelectorAll("[data-failed-overview-item]");
  rows.forEach((item) => {
    item.hidden = !matchesPeriodFilter(item);
    if (!item.hidden) {
      visibleCount += 1;
    }
  });

  const count = document.querySelector("[data-overview-visible-count]");
  if (count) {
    count.textContent = String(visibleCount);
  }

  const table = document.querySelector("[data-overview-table]");
  const emptyState = document.querySelector("[data-overview-filter-empty]");
  const hasFilteredEmptyState = rows.length > 0 && visibleCount === 0;
  if (table) {
    table.hidden = hasFilteredEmptyState;
  }
  if (emptyState) {
    emptyState.hidden = !hasFilteredEmptyState;
  }
}

function applySampleFilters() {
  const query = sampleSearchQuery();
  let visibleCount = 0;

  document.querySelectorAll("[data-sample-item]").forEach((item) => {
    const matchesStatus = (
      activeSampleFilter === "all"
      || item.dataset.status === activeSampleFilter
    );
    const searchableText = (item.dataset.searchText || "")
      .toLocaleLowerCase("zh-CN");
    const matchesSearch = !query || searchableText.includes(query);
    const matchesPeriod = matchesPeriodFilter(item);
    item.hidden = !(matchesStatus && matchesSearch && matchesPeriod);
    if (!item.hidden) {
      visibleCount += 1;
    }
  });

  document.querySelectorAll("[data-script-section]").forEach((section) => {
    const items = section.querySelectorAll("[data-sample-item]");
    const visibleItems = section.querySelectorAll(
      "[data-sample-item]:not([hidden])",
    );
    section.hidden = items.length > 0 && visibleItems.length === 0;
  });

  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      button.dataset.filter === activeSampleFilter ? "true" : "false",
    );
  });

  const count = document.querySelector("[data-visible-count]");
  if (count) {
    count.textContent = String(visibleCount);
  }
}

document.addEventListener("click", (event) => {
  const allDetailsButton = event.target.closest("[data-all-details-toggle]");
  if (allDetailsButton) {
    setAllDetailsExpanded(allDetailsButton.dataset.expand === "true");
    return;
  }

  const filterButton = event.target.closest("[data-filter]");
  if (filterButton) {
    activeSampleFilter = filterButton.dataset.filter;
    applySampleFilters();
    return;
  }

  const toggleButton = event.target.closest("[data-detail-toggle]");
  if (toggleButton) {
    const control = detailControlsByButton.get(toggleButton);
    if (control) {
      cancelBulkDetailUpdate();
      setDetailExpanded(control, !isDetailExpanded(control));
      syncAllDetailsToggle();
    }
    return;
  }

  const copyButton = event.target.closest("[data-copy-target]");
  if (copyButton) {
    copyRawText(copyButton);
  }
});

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-sample-search]")) {
    applySampleFilters();
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-period-filter]")) {
    activePeriodFilter = event.target.value;
    syncPeriodFilters();
    applyOverviewFilters();
    applySampleFilters();
  }
});

window.addEventListener("beforeprint", () => {
  cancelBulkDetailUpdate();
  printDetailState.clear();
  detailControls.forEach((control) => {
    const wasExpanded = isDetailExpanded(control);
    printDetailState.set(control, wasExpanded);
    setDetailExpanded(control, true);
  });
});

window.addEventListener("afterprint", () => {
  printDetailState.forEach((wasExpanded, control) => {
    setDetailExpanded(control, wasExpanded);
  });
  printDetailState.clear();
  syncAllDetailsToggle();
});

syncPeriodFilters();
applyOverviewFilters();
applySampleFilters();
syncAllDetailsToggle();
