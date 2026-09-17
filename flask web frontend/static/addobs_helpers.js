(function (root, factory) {
  const helpers = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.LabelsAddObsHelpers = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ICONIC_TAXON_GROUPS = {
    Fungi: "fungi",
    Plantae: "plantae",
    Protozoa: "protozoa",
    Chromista: "chromista",
    Mollusca: "orange-animal",
    Arachnida: "orange-animal",
    Insecta: "orange-animal",
    Amphibia: "blue-animal",
    Reptilia: "blue-animal",
    Aves: "blue-animal",
    Mammalia: "blue-animal",
    Actinopterygii: "blue-animal",
    Animalia: "blue-animal",
  };

  const TAXON_GROUPS = new Set([
    "fungi",
    "plantae",
    "protozoa",
    "chromista",
    "orange-animal",
    "blue-animal",
    "unknown",
  ]);

  function normalizedSource(sourceHint) {
    const source = String(sourceHint || "")
      .trim()
      .toLowerCase();
    if (["mo", "mushroomobserver", "mushroom-observer"].includes(source))
      return "mo";
    if (["bg", "bugguide"].includes(source)) return "bg";
    return "inat";
  }

  function parseObservationInput(input) {
    const normalizedInput = String(input || "")
      .replace(/\b(?:bg|bugguide)\s+(\d+)/gi, "BG$1")
      .replace(/\b(?:mo|mushroomobserver)\s+(\d+)/gi, "MO$1")
      .replace(/\b(?:inat|inaturalist)\s+(\d+)/gi, "$1");
    const results = [];

    normalizedInput
      .split(/[\s,]+/)
      .map((part) => part.trim())
      .filter(Boolean)
      .forEach((part) => {
        let match = part.match(/inaturalist\.org\/observations\/(\d+)/i);
        if (match) {
          results.push(match[1]);
          return;
        }

        if (/mushroomobserver\.org/i.test(part)) {
          match =
            part.match(/\/(?:obs|observations)\/(\d+)(?:[/?#]|$)/i) ||
            part.match(/[?&]ids=(\d+)(?:[&#]|$)/i) ||
            part.match(/\/(\d+)(?:[/?#]|$)/);
          if (match) {
            results.push(`MO${match[1]}`);
            return;
          }
        }

        match = part.match(/bugguide\.net\/node\/view\/(\d+)/i);
        if (match) {
          results.push(`BG${match[1]}`);
          return;
        }

        match = part.match(/^(motoinat|moinat|inatmo)\s*:?[-]?(\d+)$/i);
        if (match) {
          results.push(`${match[1].toLowerCase()}${match[2]}`);
          return;
        }

        match = part.match(/^mo\s*:?[-]?(\d+)$/i);
        if (match) {
          results.push(`MO${match[1]}`);
          return;
        }

        match = part.match(/^(?:bg|bugguide)\s*:?[-]?(\d+)$/i);
        if (match) {
          results.push(`BG${match[1]}`);
          return;
        }

        match = part.match(/^(?:inat|inaturalist)\s*:?[-]?(\d+)$/i);
        if (match) {
          results.push(match[1]);
          return;
        }

        if (/^\d+$/.test(part)) results.push(part);
      });

    return results;
  }

  function queuedObservationIds(values) {
    return Array.from(values || []).flatMap((value) =>
      parseObservationInput(value),
    );
  }

  function printedLabelCount(queuedCount, printDuplicates = false) {
    return Math.max(Number(queuedCount) || 0, 0) * (printDuplicates ? 2 : 1);
  }

  function standardSheetCount(printedCount) {
    return Math.max(1, Math.ceil(Math.max(Number(printedCount) || 0, 0) / 8));
  }

  function sheetBreakAfterRows(
    queuedCount,
    printDuplicates = false,
    mini = false,
  ) {
    const count = Math.max(Number(queuedCount) || 0, 0);
    if (mini || count < 1) return [];
    const rowsPerSheet = printDuplicates ? 4 : 8;
    const breaks = [];
    for (let row = rowsPerSheet; row < count; row += rowsPerSheet) {
      breaks.push(row);
    }
    return breaks;
  }

  function shouldShowPrintPreflight(state, largeJobThreshold = 100) {
    return (
      Number(state?.printedLabelCount || 0) >= largeJobThreshold ||
      Boolean(state?.printDuplicates) ||
      Boolean(state?.miniLabelSize) ||
      Number(state?.addedFields?.length || 0) > 0 ||
      Number(state?.suppressedFields?.length || 0) > 0 ||
      state?.sortMode === "custom"
    );
  }

  function canonicalObservationKey(value, sourceHint = "") {
    const raw = String(value || "").trim();
    if (!raw) return "";

    let match = raw.match(/inaturalist\.org\/observations\/(\d+)/i);
    if (match) return `inat:${match[1]}`;

    if (/mushroomobserver\.org/i.test(raw)) {
      match =
        raw.match(/\/(?:obs|observations)\/(\d+)(?:[/?#]|$)/i) ||
        raw.match(/[?&]ids=(\d+)(?:[&#]|$)/i) ||
        raw.match(/\/(\d+)(?:[/?#]|$)/);
    }
    if (match) return `mo:${match[1]}`;

    match = raw.match(/bugguide\.net\/node\/view\/(\d+)/i);
    if (match) return `bg:${match[1]}`;

    match = raw.match(/^(?:motoinat|moinat|inatmo)\s*:?[\s-]*(\d+)$/i);
    if (match) return `motoinat:${match[1]}`;

    match = raw.match(/^mo\s*:?[\s-]*(\d+)$/i);
    if (match) return `mo:${match[1]}`;

    match = raw.match(/^(?:bg|bugguide)\s*:?[\s-]*(\d+)$/i);
    if (match) return `bg:${match[1]}`;

    match = raw.match(/^(?:inat|inaturalist)\s*:?[\s-]*(\d+)$/i);
    if (match) return `inat:${match[1]}`;

    if (/^\d+$/.test(raw)) return `${normalizedSource(sourceHint)}:${raw}`;
    return "";
  }

  function observationUrl(value, sourceHint = "") {
    const canonicalKey = canonicalObservationKey(value, sourceHint);
    const [source, id] = canonicalKey.split(":");
    if (!/^\d+$/.test(id || "")) return "";
    if (source === "inat") {
      return `https://www.inaturalist.org/observations/${id}`;
    }
    if (source === "mo") {
      return `https://mushroomobserver.org/observations/${id}`;
    }
    if (source === "bg") {
      return `https://bugguide.net/node/view/${id}`;
    }
    return "";
  }

  function taxonColorGroup(item) {
    const suppliedGroup = String(item?.taxon_color_group || "")
      .trim()
      .toLowerCase();
    if (TAXON_GROUPS.has(suppliedGroup)) return suppliedGroup;
    return ICONIC_TAXON_GROUPS[item?.iconic_taxon_name] || "unknown";
  }

  function validObservedDate(item) {
    const value = String(item?.observed_on || "").slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = Date.parse(`${value}T00:00:00Z`);
    if (!Number.isFinite(parsed)) return null;
    return new Date(parsed).toISOString().slice(0, 10) === value
      ? parsed
      : null;
  }

  function compareResultItems(a, b, sortMode = "newest") {
    const aDate = validObservedDate(a);
    const bDate = validObservedDate(b);
    let comparison = 0;

    if (sortMode === "name") {
      comparison = String(a?.scientific_name || "").localeCompare(
        String(b?.scientific_name || ""),
        undefined,
        { sensitivity: "base" },
      );
    } else if (aDate === null && bDate !== null) {
      comparison = 1;
    } else if (aDate !== null && bDate === null) {
      comparison = -1;
    } else if (aDate !== null && bDate !== null) {
      comparison = sortMode === "oldest" ? aDate - bDate : bDate - aDate;
    }

    if (comparison !== 0) return comparison;
    return Number(a?._originalIndex || 0) - Number(b?._originalIndex || 0);
  }

  function sortResultItems(items, sortMode = "newest") {
    return Array.from(items || []).sort((a, b) =>
      compareResultItems(a, b, sortMode),
    );
  }

  function resultMatchesQuery(item, query) {
    const needle = String(query || "")
      .trim()
      .toLocaleLowerCase();
    if (!needle) return true;
    return [item?.scientific_name, item?.display_id, item?.place_guess].some(
      (value) =>
        String(value || "")
          .toLocaleLowerCase()
          .includes(needle),
    );
  }

  function formattedCount(count) {
    return Number(count || 0).toLocaleString();
  }

  function observationMatchText(count) {
    return Number(count) === 1
      ? `${formattedCount(count)} observation matches`
      : `${formattedCount(count)} observations match`;
  }

  function searchSummary(totalCount) {
    return observationMatchText(totalCount);
  }

  function selectionSummary(state) {
    return `${formattedCount(state?.shown)} shown · ${formattedCount(state?.selected)} selected · ${formattedCount(state?.alreadyOnSheet)} on sheet`;
  }

  function overCapSummary(totalCount, shownCount) {
    const normalizedTotal = Number(totalCount) || 0;
    const normalizedShown = Number(shownCount) || 0;
    const omittedCount = Math.max(normalizedTotal - normalizedShown, 0);
    const omittedText = `${formattedCount(omittedCount)} observation${omittedCount === 1 ? " is" : "s are"} not shown`;
    return `${observationMatchText(normalizedTotal)}. ${formattedCount(normalizedShown)} shown; ${omittedText}.`;
  }

  function remainingRunCapacity(existingCount, runLimit = 500) {
    const normalizedExisting = Math.max(Number(existingCount) || 0, 0);
    const normalizedLimit = Math.max(Number(runLimit) || 0, 0);
    return Math.max(normalizedLimit - normalizedExisting, 0);
  }

  function selectionState(rows) {
    const normalizedRows = Array.from(rows || []);
    const selected = normalizedRows.filter(
      (row) => !row.disabled && row.checked,
    ).length;
    const alreadyOnSheet = normalizedRows.filter((row) => row.disabled).length;
    const visibleEligible = normalizedRows.filter(
      (row) => !row.hidden && !row.disabled,
    );
    const visibleSelected = visibleEligible.filter((row) => row.checked).length;

    return {
      shown: normalizedRows.filter((row) => !row.hidden).length,
      selected,
      alreadyOnSheet,
      visibleEligible: visibleEligible.length,
      visibleSelected,
      selectDisabled: visibleEligible.length === 0,
      selectChecked:
        visibleEligible.length > 0 &&
        visibleSelected === visibleEligible.length,
      selectIndeterminate:
        visibleSelected > 0 && visibleSelected < visibleEligible.length,
    };
  }

  function updateRecentObservers(existing, observer, limit = 5) {
    const nextObserver = String(observer || "").trim();
    if (!nextObserver) return Array.from(existing || []).slice(0, limit);
    const folded = nextObserver.toLocaleLowerCase();
    return [
      nextObserver,
      ...Array.from(existing || []).filter(
        (value) =>
          String(value || "")
            .trim()
            .toLocaleLowerCase() !== folded,
      ),
    ].slice(0, limit);
  }

  // The saved queue is only ever the text the user typed. Options, added fields,
  // and fetched observation data are deliberately not persisted.
  const QUEUE_STATE_VERSION = 1;
  const QUEUE_STATE_MAX_ROWS = 500;
  const QUEUE_STATE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

  function serializeQueueState(rawValues, nowMs = Date.now()) {
    const observations = Array.from(rawValues || [])
      .map((value) => String(value == null ? "" : value).trim())
      .filter((value) => value !== "")
      .slice(0, QUEUE_STATE_MAX_ROWS);

    if (!observations.length) return null;

    return {
      v: QUEUE_STATE_VERSION,
      savedAt: nowMs,
      observations,
    };
  }

  function deserializeQueueState(json, nowMs = Date.now()) {
    let parsed;
    try {
      parsed = JSON.parse(json);
    } catch (_) {
      return [];
    }

    if (!parsed || typeof parsed !== "object") return [];
    if (parsed.v !== QUEUE_STATE_VERSION) return [];
    if (!Array.isArray(parsed.observations)) return [];

    const savedAt = Number(parsed.savedAt);
    if (!Number.isFinite(savedAt)) return [];
    if (nowMs - savedAt > QUEUE_STATE_MAX_AGE_MS) return [];

    return parsed.observations
      .filter((value) => typeof value === "string")
      .map((value) => value.trim())
      .filter((value) => value !== "")
      .slice(0, QUEUE_STATE_MAX_ROWS);
  }

  return {
    serializeQueueState,
    deserializeQueueState,
    parseObservationInput,
    queuedObservationIds,
    printedLabelCount,
    standardSheetCount,
    sheetBreakAfterRows,
    shouldShowPrintPreflight,
    canonicalObservationKey,
    observationUrl,
    compareResultItems,
    resultMatchesQuery,
    observationMatchText,
    searchSummary,
    selectionSummary,
    overCapSummary,
    remainingRunCapacity,
    selectionState,
    sortResultItems,
    taxonColorGroup,
    updateRecentObservers,
    validObservedDate,
  };
});
