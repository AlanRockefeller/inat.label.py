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

  function remainingRunCapacity(existingCount, runLimit = 1000) {
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
  const QUEUE_STATE_MAX_ROWS = 1000;
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

  // ---------------------------------------------------------------------------
  // CSV upload: find iNaturalist observation numbers and ignore other columns.
  // ---------------------------------------------------------------------------

  const CSV_UPLOAD_MAX_OBSERVATIONS = 1000;
  const CSV_UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

  // Header names (lowercased, non-alphanumerics removed) that name the
  // observation number, best first.  iNaturalist's own export calls it "id".
  const CSV_ID_HEADERS = [
    "id",
    "observationid",
    "observationnumber",
    "observationno",
    "observation",
    "obsid",
    "obsnumber",
    "inatid",
    "inatnumber",
    "inatobservationid",
    "inatobservationnumber",
    "inat",
    "inaturalist",
    "inaturalistid",
    "inaturalistnumber",
    "inaturalistobservationid",
    "inaturalistobservationnumber",
    "url",
    "observationurl",
    "inaturl",
    "inaturalisturl",
  ];

  const CSV_INAT_URL =
    /(?:inaturalist|naturalista|biodiversity4all|argentinat)[a-z.]*\/observations\/(\d+)/i;
  const CSV_PLAIN_ID = /^\d{1,12}$/;

  function csvError(code, message, extra = {}) {
    return Object.assign({ ok: false, code, error: message }, extra);
  }

  // RFC 4180 with the usual relaxations: CRLF/LF/CR line ends, a byte-order
  // mark, and comma, semicolon or tab delimiters.  Returns null when a quoted
  // field never closes.
  function parseCsvRows(text) {
    const source = String(text).replace(/^﻿/, "");
    const firstLine = source.split(/\r\n|\n|\r/, 1)[0] || "";
    const counts = { ",": 0, ";": 0, "\t": 0 };
    let quoted = false;
    for (const ch of firstLine) {
      if (ch === '"') quoted = !quoted;
      else if (!quoted && ch in counts) counts[ch] += 1;
    }
    const delimiter = Object.keys(counts).reduce((best, ch) =>
      counts[ch] > counts[best] ? ch : best, ",");

    const rows = [];
    let row = [];
    let field = "";
    let inQuotes = false;
    for (let i = 0; i < source.length; i += 1) {
      const ch = source[i];
      if (inQuotes) {
        if (ch === '"') {
          if (source[i + 1] === '"') {
            field += '"';
            i += 1;
          } else {
            inQuotes = false;
          }
        } else {
          field += ch;
        }
      } else if (ch === '"' && field.trim() === "") {
        inQuotes = true;
        field = "";
      } else if (ch === delimiter) {
        row.push(field);
        field = "";
      } else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && source[i + 1] === "\n") i += 1;
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
      } else {
        field += ch;
      }
    }
    if (inQuotes) return null;
    if (field !== "" || row.length) {
      row.push(field);
      rows.push(row);
    }
    return rows
      .map((cells) => cells.map((cell) => cell.trim()))
      .filter((cells) => cells.some((cell) => cell !== ""));
  }

  function csvCellObservationId(cell) {
    const value = String(cell || "").trim();
    if (CSV_PLAIN_ID.test(value)) {
      const id = value.replace(/^0+/, "");
      return id === "" ? null : id;
    }
    const match = value.match(CSV_INAT_URL);
    return match ? match[1].replace(/^0+/, "") || null : null;
  }

  function csvHeaderKey(cell) {
    return String(cell || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  /**
   * Find iNaturalist observation numbers in CSV text.
   *
   * The ID column is, in order: a column whose header names the observation
   * number ("id", "Observation ID", "url", ...), a column of iNaturalist
   * observation URLs, the only column of plain numbers, or the only column.
   * Other columns are ignored, so an iNaturalist export's user_id and
   * taxon_id are never mistaken for observations.
   *
   * Returns {ok: true, ids, column, duplicates, skipped} or
   * {ok: false, code, error}.
   */
  function extractCsvObservationIds(text, limit = CSV_UPLOAD_MAX_OBSERVATIONS) {
    if (typeof text !== "string") {
      return csvError("unreadable", "The file could not be read as text.");
    }
    if (/\u0000/.test(text.slice(0, 4096)) || /^PK\u0003\u0004/.test(text)) {
      return csvError(
        "not_csv",
        "This doesn't look like a CSV file. If it is an Excel or Numbers spreadsheet, " +
          "save or export it as CSV first, then upload that file.",
      );
    }
    const rows = parseCsvRows(text);
    if (rows === null) {
      return csvError(
        "unparseable",
        "This CSV couldn't be read: a quoted value is never closed. Check for a stray " +
          "quotation mark (\") in the file, or re-export it as CSV.",
      );
    }
    if (!rows.length) {
      return csvError("empty", "This CSV file is empty.");
    }

    const width = Math.max(...rows.map((cells) => cells.length));
    const first = rows[0];
    const hasHeader = first.some(
      (cell) => cell !== "" && csvCellObservationId(cell) === null,
    );
    const header = hasHeader ? first : [];
    const data = hasHeader ? rows.slice(1) : rows;
    const columns = Array.from({ length: width }, (_, index) => {
      let filled = 0;
      let plain = 0;
      let urls = 0;
      data.forEach((cells) => {
        const cell = cells[index] || "";
        if (cell === "") return;
        filled += 1;
        if (CSV_PLAIN_ID.test(cell)) plain += 1;
        else if (CSV_INAT_URL.test(cell)) urls += 1;
      });
      return { index, filled, plain, urls, key: csvHeaderKey(header[index]) };
    });
    const usable = (column) => column.plain + column.urls > 0;

    let chosen = null;
    for (const name of CSV_ID_HEADERS) {
      chosen = columns.find((column) => column.key === name && usable(column));
      if (chosen) break;
    }
    if (!chosen) {
      const withUrls = columns.filter((column) => column.urls > 0);
      if (withUrls.length) {
        chosen = withUrls.reduce((best, column) => (column.urls > best.urls ? column : best));
      }
    }
    if (!chosen) {
      const numeric = columns.filter(
        (column) => column.filled > 0 && column.plain === column.filled,
      );
      if (numeric.length === 1) {
        chosen = numeric[0];
      } else if (numeric.length > 1) {
        const names = numeric
          .map((column) => header[column.index] || `column ${column.index + 1}`)
          .map((name) => `“${name}”`)
          .join(", ");
        return csvError(
          "ambiguous",
          `This CSV has several columns of numbers (${names}), so it isn't clear which ` +
            "one holds iNaturalist observation numbers. Name that column “id” in the " +
            "header row, or upload a CSV with just that one column.",
        );
      } else if (width === 1 && usable(columns[0])) {
        chosen = columns[0];
      }
    }
    if (!chosen) {
      return csvError(
        "no_ids",
        "No iNaturalist observation numbers were found in this CSV. Put the numbers " +
          "(or iNaturalist observation URLs) in a column, ideally with the header “id”.",
      );
    }

    const ids = [];
    const seen = new Set();
    let duplicates = 0;
    let skipped = 0;
    data.forEach((cells) => {
      const cell = cells[chosen.index] || "";
      if (cell === "") return;
      const id = csvCellObservationId(cell);
      if (id === null) {
        skipped += 1;
      } else if (seen.has(id)) {
        duplicates += 1;
      } else {
        seen.add(id);
        ids.push(id);
      }
    });
    if (!ids.length) {
      return csvError(
        "no_ids",
        "No iNaturalist observation numbers were found in this CSV. Put the numbers " +
          "(or iNaturalist observation URLs) in a column, ideally with the header “id”.",
      );
    }
    if (ids.length > limit) {
      return csvError(
        "too_many",
        `This CSV has ${ids.length.toLocaleString("en-US")} iNaturalist observations, ` +
          `but one upload can add at most ${limit.toLocaleString("en-US")}. ` +
          "Split it into smaller files and upload them one at a time.",
        { count: ids.length },
      );
    }
    return {
      ok: true,
      ids,
      column: header[chosen.index] || (width > 1 ? `column ${chosen.index + 1}` : ""),
      duplicates,
      skipped,
    };
  }

  return {
    CSV_UPLOAD_MAX_OBSERVATIONS,
    CSV_UPLOAD_MAX_BYTES,
    extractCsvObservationIds,
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
