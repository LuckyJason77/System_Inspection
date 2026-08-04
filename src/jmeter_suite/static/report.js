/* 管理离线报告的接口分组、筛选、正文解压、复制和批量操作。 */

"use strict";

let activeSampleFilter = "all";
let activePeriodFilter = "all";
let filterGroupStateSnapshot = null;
const BULK_DETAIL_BATCH_SIZE = 20;
const BULK_GROUP_BATCH_SIZE = 20;
const MAX_PAYLOAD_CONCURRENCY = 4;
let bulkDetailOperation = 0;
let bulkGroupOperation = 0;

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

function collectDetailControls() {
  return Array.from(document.querySelectorAll("[data-detail-toggle]"))
    .map((button) => {
      const panelId = button.getAttribute("aria-controls");
      const panel = panelId ? document.getElementById(panelId) : null;
      const item = button.closest("[data-sample-item]");
      if (!panel || !item) {
        return null;
      }
      const request = item.querySelector("[data-raw-request]");
      const response = item.querySelector("[data-raw-response]");
      const copyButtons = Array.from(
        item.querySelectorAll("[data-copy-target]"),
      );
      const bodyTargetIds = new Set(
        [request, response]
          .filter((target) => target && target.id)
          .map((target) => target.id),
      );
      return {
        button,
        panel,
        item,
        label: button.querySelector("[data-toggle-label]"),
        request,
        response,
        copyButtons,
        bodyCopyButtons: copyButtons.filter(
          (copyButton) => bodyTargetIds.has(copyButton.dataset.copyTarget),
        ),
        payloadTemplate: item.querySelector("[data-compressed-payload]"),
        payloadError: item.querySelector("[data-payload-error]"),
        hydrationPromise: null,
      };
    })
    .filter((control) => control !== null);
}

function collectGroupControls() {
  return Array.from(document.querySelectorAll("[data-endpoint-group-toggle]"))
    .map((button) => {
      const panelId = button.getAttribute("aria-controls");
      const panel = panelId ? document.getElementById(panelId) : null;
      const container = button.closest("[data-endpoint-group]");
      if (!panel || !container) {
        return null;
      }
      return {
        button,
        panel,
        container,
        label: button.querySelector("[data-endpoint-group-label]"),
      };
    })
    .filter((control) => control !== null);
}

const detailControls = collectDetailControls();
const detailControlsByButton = new Map(
  detailControls.map((control) => [control.button, control]),
);
const detailControlsByItem = new Map(
  detailControls.map((control) => [control.item, control]),
);
const groupControls = collectGroupControls();
const groupControlsByButton = new Map(
  groupControls.map((control) => [control.button, control]),
);
const allDetailsButton = document.querySelector("[data-all-details-toggle]");
const allDetailsLabel = allDetailsButton
  ? allDetailsButton.querySelector("[data-all-details-label]")
  : null;
const allGroupsButton = document.querySelector("[data-all-groups-toggle]");
const allGroupsLabel = allGroupsButton
  ? allGroupsButton.querySelector("[data-all-groups-label]")
  : null;
const operationStatus = document.querySelector("[data-report-operation-status]");
const compatibilityWarning = document.querySelector(
  "[data-decompression-warning]",
);
const payloadCompressionSupported = typeof DecompressionStream === "function";
const hasCompressedPayloads = detailControls.some(
  (control) => control.payloadTemplate !== null,
);

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

function isGroupExpanded(control) {
  return control.button.getAttribute("aria-expanded") === "true";
}

function groupMatchesState(control, expanded) {
  return isGroupExpanded(control) === expanded
    && control.panel.hidden === !expanded;
}

function setGroupExpanded(control, expanded) {
  const ariaValue = expanded ? "true" : "false";
  if (control.button.getAttribute("aria-expanded") !== ariaValue) {
    control.button.setAttribute("aria-expanded", ariaValue);
  }
  if (control.panel.hidden !== !expanded) {
    control.panel.hidden = !expanded;
  }
  const labelText = expanded ? "收起接口" : "展开接口";
  if (control.label && control.label.textContent !== labelText) {
    control.label.textContent = labelText;
  }
}

function showOperationStatus(message, failed = false) {
  if (!operationStatus) {
    return;
  }
  operationStatus.textContent = message;
  operationStatus.classList.toggle("operation-status-error", failed);
  operationStatus.hidden = !message;
}

function setButtonProgress(button, label, message) {
  if (!button) {
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.setAttribute("aria-label", message);
  if (label) {
    label.textContent = message;
  }
}

function clearButtonProgress(button) {
  if (button) {
    button.removeAttribute("aria-busy");
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
  const labelText = shouldExpand ? "展开全部正文" : "收起全部正文";
  allDetailsButton.dataset.expand = shouldExpand ? "true" : "false";
  allDetailsButton.disabled = detailControls.length === 0
    || (
      shouldExpand
      && hasCompressedPayloads
      && !payloadCompressionSupported
    );
  allDetailsButton.setAttribute("aria-label", labelText);
  if (allDetailsLabel) {
    allDetailsLabel.textContent = labelText;
  }
}

function syncAllGroupsToggle() {
  if (!allGroupsButton) {
    return;
  }
  if (allGroupsButton.getAttribute("aria-busy") === "true") {
    allGroupsButton.disabled = true;
    return;
  }

  const anyExpanded = groupControls.some(isGroupExpanded);
  const shouldExpand = !anyExpanded;
  const labelText = shouldExpand
    ? "展开全部接口组"
    : "收起全部接口组";
  allGroupsButton.dataset.expand = shouldExpand ? "true" : "false";
  allGroupsButton.disabled = groupControls.length === 0;
  allGroupsButton.setAttribute("aria-label", labelText);
  if (allGroupsLabel) {
    allGroupsLabel.textContent = labelText;
  }
}

function cancelBulkDetailUpdate() {
  bulkDetailOperation += 1;
  clearButtonProgress(allDetailsButton);
  syncAllDetailsToggle();
}

function cancelBulkGroupUpdate() {
  bulkGroupOperation += 1;
  clearButtonProgress(allGroupsButton);
  syncAllGroupsToggle();
}

function nextAnimationFrame() {
  return new Promise((resolve) => {
    requestAnimationFrame(resolve);
  });
}

function base64ToBytes(encoded) {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function setPayloadButtonsDisabled(control, disabled) {
  control.copyButtons.forEach((button) => {
    button.disabled = disabled;
  });
}

function disableUnsupportedPayloadControls() {
  if (payloadCompressionSupported) {
    return;
  }
  detailControls.forEach((control) => {
    if (!control.payloadTemplate) {
      return;
    }
    control.button.disabled = true;
    control.button.title = "当前浏览器不支持离线正文解压";
    control.bodyCopyButtons.forEach((button) => {
      button.disabled = true;
      button.title = "当前浏览器不支持离线正文解压";
    });
  });
}

function setPayloadError(control, message) {
  control.item.dataset.payloadState = "error";
  if (control.request) {
    control.request.textContent = "正文加载失败，请重试";
  }
  if (control.response) {
    control.response.textContent = "正文加载失败，请重试";
  }
  if (control.payloadError) {
    control.payloadError.textContent = message;
    control.payloadError.hidden = false;
  }
}

async function hydrateSamplePayload(control) {
  if (control.item.dataset.payloadState === "ready") {
    return;
  }
  if (control.hydrationPromise) {
    return control.hydrationPromise;
  }
  if (!control.payloadTemplate) {
    control.item.dataset.payloadState = "ready";
    return;
  }
  if (!payloadCompressionSupported) {
    const error = new Error("当前浏览器不支持离线正文解压");
    setPayloadError(control, error.message);
    throw error;
  }

  control.item.dataset.payloadState = "loading";
  control.button.setAttribute("aria-busy", "true");
  setPayloadButtonsDisabled(control, true);
  if (control.request) {
    control.request.textContent = "正文加载中…";
  }
  if (control.response) {
    control.response.textContent = "正文加载中…";
  }
  if (control.payloadError) {
    control.payloadError.hidden = true;
  }

  control.hydrationPromise = (async () => {
    try {
      const encoded = control.payloadTemplate.content.textContent.trim();
      const compressed = base64ToBytes(encoded);
      const decompressed = new Blob([compressed])
        .stream()
        .pipeThrough(new DecompressionStream("gzip"));
      const payloadText = await new Response(decompressed).text();
      const payload = JSON.parse(payloadText);
      if (
        typeof payload.request !== "string"
        || typeof payload.response !== "string"
      ) {
        throw new Error("正文数据格式无效");
      }
      if (control.request) {
        control.request.textContent = payload.request || "无请求参数";
      }
      if (control.response) {
        control.response.textContent = payload.response || "无响应参数";
      }
      control.item.dataset.payloadState = "ready";
      control.payloadTemplate.remove();
      control.payloadTemplate = null;
      if (control.payloadError) {
        control.payloadError.hidden = true;
      }
    } catch (_error) {
      setPayloadError(control, "正文加载失败，请重试。");
      throw _error;
    } finally {
      control.button.removeAttribute("aria-busy");
      setPayloadButtonsDisabled(control, false);
      control.hydrationPromise = null;
    }
  })();
  return control.hydrationPromise;
}

async function hydrateControls(
  controls,
  isCurrent,
  onProgress,
  beforeHydrate = null,
) {
  let nextIndex = 0;
  let completed = 0;
  const failures = [];

  async function worker() {
    while (isCurrent()) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= controls.length) {
        return;
      }
      const control = controls[index];
      if (beforeHydrate) {
        beforeHydrate(control);
      }
      try {
        await hydrateSamplePayload(control);
      } catch (_error) {
        failures.push(control);
      }
      completed += 1;
      onProgress(completed, controls.length);
      await nextAnimationFrame();
    }
  }

  const workerCount = Math.min(MAX_PAYLOAD_CONCURRENCY, controls.length);
  await Promise.all(
    Array.from({ length: workerCount }, () => worker()),
  );
  return {
    cancelled: !isCurrent(),
    failures,
  };
}

async function setAllDetailsExpanded(expanded) {
  cancelBulkDetailUpdate();
  const pendingControls = detailControls.filter(
    (control) => !detailMatchesState(control, expanded)
      || (
        expanded
        && control.item.dataset.payloadState !== "ready"
      ),
  );
  if (pendingControls.length === 0) {
    return { cancelled: false, failures: [] };
  }

  const operation = ++bulkDetailOperation;
  const isCurrent = () => operation === bulkDetailOperation;
  let completed = 0;
  const action = expanded ? "展开正文" : "收起正文";
  setButtonProgress(
    allDetailsButton,
    allDetailsLabel,
    `${action}中 0/${pendingControls.length}`,
  );

  let result = { cancelled: false, failures: [] };
  if (expanded) {
    result = await hydrateControls(
      pendingControls,
      isCurrent,
      (done, total) => {
        setButtonProgress(
          allDetailsButton,
          allDetailsLabel,
          `${action}中 ${done}/${total}`,
        );
      },
      (control) => setDetailExpanded(control, true),
    );
  } else {
    for (
      let start = 0;
      start < pendingControls.length && isCurrent();
      start += BULK_DETAIL_BATCH_SIZE
    ) {
      const end = Math.min(
        start + BULK_DETAIL_BATCH_SIZE,
        pendingControls.length,
      );
      for (let index = start; index < end; index += 1) {
        setDetailExpanded(pendingControls[index], false);
      }
      completed = end;
      setButtonProgress(
        allDetailsButton,
        allDetailsLabel,
        `${action}中 ${completed}/${pendingControls.length}`,
      );
      await nextAnimationFrame();
    }
    result.cancelled = !isCurrent();
  }

  if (isCurrent()) {
    clearButtonProgress(allDetailsButton);
    syncAllDetailsToggle();
    if (result.failures.length > 0) {
      showOperationStatus(
        `正文加载失败 ${result.failures.length} 条，其余请求已处理。`,
        true,
      );
    }
  }
  return result;
}

async function setAllGroupsExpanded(expanded) {
  cancelBulkGroupUpdate();
  const pendingControls = groupControls.filter(
    (control) => !groupMatchesState(control, expanded),
  );
  if (pendingControls.length === 0) {
    return { cancelled: false };
  }

  const operation = ++bulkGroupOperation;
  const action = expanded ? "展开接口组" : "收起接口组";
  setButtonProgress(
    allGroupsButton,
    allGroupsLabel,
    `${action}中 0/${pendingControls.length}`,
  );
  for (
    let start = 0;
    start < pendingControls.length && operation === bulkGroupOperation;
    start += BULK_GROUP_BATCH_SIZE
  ) {
    const end = Math.min(
      start + BULK_GROUP_BATCH_SIZE,
      pendingControls.length,
    );
    for (let index = start; index < end; index += 1) {
      setGroupExpanded(pendingControls[index], expanded);
    }
    setButtonProgress(
      allGroupsButton,
      allGroupsLabel,
      `${action}中 ${end}/${pendingControls.length}`,
    );
    await nextAnimationFrame();
  }

  const cancelled = operation !== bulkGroupOperation;
  if (!cancelled) {
    clearButtonProgress(allGroupsButton);
    syncAllGroupsToggle();
  }
  return { cancelled };
}

async function copyRawText(button) {
  const targetId = button.dataset.copyTarget;
  const target = targetId ? document.getElementById(targetId) : null;
  if (!target) {
    return;
  }
  const item = button.closest("[data-sample-item]");
  const control = item ? detailControlsByItem.get(item) : null;
  const needsHydration = control
    && (target === control.request || target === control.response);
  if (needsHydration) {
    cancelBulkDetailUpdate();
    try {
      await hydrateSamplePayload(control);
    } catch (_error) {
      const originalLabel = button.textContent;
      button.textContent = "加载失败";
      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1200);
      return;
    }
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
    let visibleRanges = 0;
    item.querySelectorAll("[data-failure-target]").forEach((link) => {
      link.hidden = !matchesPeriodFilter(link);
      if (!link.hidden) {
        visibleRanges += 1;
      }
    });
    item.hidden = visibleRanges === 0;
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

function filtersAreActive() {
  return activeSampleFilter !== "all"
    || activePeriodFilter !== "all"
    || sampleSearchQuery() !== "";
}

function captureFilterGroupState() {
  return new Map(
    groupControls.map((control) => [control, isGroupExpanded(control)]),
  );
}

function restoreFilterGroupState() {
  if (!filterGroupStateSnapshot) {
    return;
  }
  filterGroupStateSnapshot.forEach((expanded, control) => {
    setGroupExpanded(control, expanded);
  });
  filterGroupStateSnapshot = null;
}

function applySampleFilters() {
  const query = sampleSearchQuery();
  const filtering = filtersAreActive();
  if (filtering && !filterGroupStateSnapshot) {
    filterGroupStateSnapshot = captureFilterGroupState();
  } else if (!filtering && filterGroupStateSnapshot) {
    restoreFilterGroupState();
  }

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

  groupControls.forEach((control) => {
    const visibleItems = Array.from(
      control.container.querySelectorAll("[data-sample-item]"),
    ).filter((item) => !item.hidden);
    control.container.hidden = visibleItems.length === 0;
    if (filtering && visibleItems.length > 0) {
      setGroupExpanded(control, true);
    }
  });

  document.querySelectorAll("[data-script-section]").forEach((section) => {
    const groups = section.querySelectorAll("[data-endpoint-group]");
    const visibleGroups = section.querySelectorAll(
      "[data-endpoint-group]:not([hidden])",
    );
    section.hidden = groups.length > 0 && visibleGroups.length === 0;
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
  syncAllGroupsToggle();
}

async function locateFailedSample(button) {
  const targetId = button.dataset.failureTarget;
  const target = targetId ? document.getElementById(targetId) : null;
  if (!target) {
    return;
  }

  const search = document.querySelector("[data-sample-search]");
  if (search) {
    search.value = "";
  }
  activeSampleFilter = "failed";
  applySampleFilters();
  cancelBulkGroupUpdate();
  cancelBulkDetailUpdate();

  const group = target.closest("[data-endpoint-group]");
  const groupButton = group
    ? group.querySelector("[data-endpoint-group-toggle]")
    : null;
  const groupControl = groupButton
    ? groupControlsByButton.get(groupButton)
    : null;
  if (groupControl) {
    setGroupExpanded(groupControl, true);
  }

  const detailControl = detailControlsByItem.get(target);
  if (detailControl) {
    setDetailExpanded(detailControl, true);
    try {
      await hydrateSamplePayload(detailControl);
    } catch (_error) {
      showOperationStatus("目标请求正文加载失败，请重试。", true);
    }
  }
  target.classList.add("sample-card-highlight");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  if (detailControl) {
    detailControl.button.focus({ preventScroll: true });
  }
  window.setTimeout(() => {
    target.classList.remove("sample-card-highlight");
  }, 2200);
  syncAllGroupsToggle();
  syncAllDetailsToggle();
}

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const failureTarget = event.target.closest("[data-failure-target]");
  if (failureTarget) {
    void locateFailedSample(failureTarget);
    return;
  }

  const allGroupsToggle = event.target.closest("[data-all-groups-toggle]");
  if (allGroupsToggle) {
    void setAllGroupsExpanded(allGroupsToggle.dataset.expand === "true");
    return;
  }

  const allDetailsToggle = event.target.closest("[data-all-details-toggle]");
  if (allDetailsToggle) {
    void setAllDetailsExpanded(allDetailsToggle.dataset.expand === "true");
    return;
  }

  const groupButton = event.target.closest("[data-endpoint-group-toggle]");
  if (groupButton) {
    const control = groupControlsByButton.get(groupButton);
    if (control) {
      cancelBulkGroupUpdate();
      setGroupExpanded(control, !isGroupExpanded(control));
      syncAllGroupsToggle();
    }
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
      const expanded = !isDetailExpanded(control);
      setDetailExpanded(control, expanded);
      if (expanded) {
        void hydrateSamplePayload(control).catch(() => {
          showOperationStatus("正文加载失败，请重试。", true);
        });
      }
      syncAllDetailsToggle();
    }
    return;
  }

  const copyButton = event.target.closest("[data-copy-target]");
  if (copyButton) {
    void copyRawText(copyButton);
  }
});

document.addEventListener("input", (event) => {
  if (
    event.target instanceof Element
    && event.target.matches("[data-sample-search]")
  ) {
    applySampleFilters();
  }
});

document.addEventListener("change", (event) => {
  if (
    event.target instanceof Element
    && event.target.matches("[data-period-filter]")
  ) {
    activePeriodFilter = event.target.value;
    syncPeriodFilters();
    applyOverviewFilters();
    applySampleFilters();
  }
});

if (!payloadCompressionSupported && hasCompressedPayloads) {
  disableUnsupportedPayloadControls();
  if (compatibilityWarning) {
    compatibilityWarning.hidden = false;
  }
}
syncPeriodFilters();
applyOverviewFilters();
applySampleFilters();
syncAllGroupsToggle();
syncAllDetailsToggle();
