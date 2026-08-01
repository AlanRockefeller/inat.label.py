from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    url_for,
    render_template,
    send_from_directory,
    redirect,
)
from flask_cors import CORS
import subprocess
import os
import requests
import csv
import io
import logging
import time
import re
import json
import traceback
import sys
from collections import defaultdict, OrderedDict
from datetime import date
from uuid import uuid4
from functools import partial

import threading
from logging.handlers import RotatingFileHandler

from date_windows import build_date_windows

INAT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "flask-labels (inat.label.py frontend)",
}

ICONIC_TAXON_COLOR_GROUPS = {
    "Fungi": "fungi",
    "Plantae": "plantae",
    "Protozoa": "protozoa",
    "Chromista": "chromista",
    "Mollusca": "orange-animal",
    "Arachnida": "orange-animal",
    "Insecta": "orange-animal",
    "Amphibia": "blue-animal",
    "Reptilia": "blue-animal",
    "Aves": "blue-animal",
    "Mammalia": "blue-animal",
    "Actinopterygii": "blue-animal",
    "Animalia": "blue-animal",
}

LEGACY_COLORS_BY_TAXON_GROUP = {
    "fungi": "magenta",
    "plantae": "green",
    "protozoa": "purple",
    "chromista": "brown",
    "orange-animal": "red",
    "blue-animal": "blue",
    "unknown": "black",
}


def taxon_color_group(iconic_taxon_name):
    """Return the UI color group for an iNaturalist iconic taxon."""
    if not isinstance(iconic_taxon_name, str):
        return "unknown"
    return ICONIC_TAXON_COLOR_GROUPS.get(iconic_taxon_name, "unknown")


def legacy_color_for_taxon_group(color_group):
    """Retain the pre-existing color response field for API compatibility."""
    return LEGACY_COLORS_BY_TAXON_GROUP.get(color_group, "black")


# Rate limiting for iNaturalist API
api_lock = threading.Lock()
next_api_call_time = 0.0

# Hardening settings
MAX_OBS_PER_REQUEST = int(os.environ.get("MAX_OBS_PER_REQUEST", "500"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
# Mushroom Observer has no histogram endpoint, so per-day counts for the date
# windows can only come from walking result pages.  Each page is ~1000 records
# and a few seconds upstream, and this app runs on a single Gunicorn worker, so
# the walk is bounded; past the budget the counts are marked incomplete and the
# window chips are skipped rather than tying up the worker.
MO_MAX_WINDOW_PAGES = int(os.environ.get("MO_MAX_WINDOW_PAGES", "5"))
# The daily counts behind the date windows are identical for identical filters,
# so a short-lived cache keeps debounced keystrokes off the rate-limited,
# lock-serialized iNaturalist client.
INAT_HISTOGRAM_CACHE_TTL = int(os.environ.get("INAT_HISTOGRAM_CACHE_TTL", "300"))
INAT_HISTOGRAM_CACHE_MAX_ENTRIES = 64
FINISHED_JOB_TTL = int(
    os.environ.get("FINISHED_JOB_TTL", "300")
)  # Time in seconds to keep finished jobs
ENABLE_MO_DEBUG = bool(int(os.environ.get("ENABLE_MO_DEBUG", "0")))
# Label sort orders accepted by inat.label.py's --sort option
SORT_MODES = ("none", "date", "date-desc", "voucher", "custom")
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS"
)  # comma-separated list of allowed origins

# Observation fields that inat.label.py automatically includes on labels when present.
# These should be checked by default in the Add Fields modal.
# This list is derived from create_inaturalist_label() in inat.label.py.
DEFAULT_LABEL_FIELDS = [
    # DNA Barcode fields
    "DNA Barcode ITS",
    "DNA Barcode LSU",
    "DNA Barcode RPB1",
    "DNA Barcode RPB2",
    "DNA Barcode TEF1",
    # Other optional fields that get added if present
    "GenBank Accession Number",
    "GenBank Accession",
    "Provisional Species Name",
    "Species Name Override",
    "Microscopy Performed",
    "Fungal Microscopy",
    "Mobile or Traditional Photography?",
    "Collector's name",
    "Herbarium Catalog Number",
    "Fungarium Catalog Number",
    "Herbarium Secondary Catalog Number",
    "Habitat",
    "Microhabitat",
    "Collection Number",
    "Associated Species",
    "Herbarium Name",
    "Mycoportal ID",
    "Voucher Number",
    "Voucher Number(s)",
    "Accession Number",
    "Mushroom Observer URL",
]

# In-memory job store for streaming print jobs
_jobs = {}
_jobs_lock = threading.Lock()


def _reap_finished_jobs_locked():
    now = time.time()
    finished_to_reap = []
    for job_id, job in _jobs.items():
        if job.get("finished_time"):
            if now > job["finished_time"] + FINISHED_JOB_TTL:
                finished_to_reap.append(job_id)
        elif job["proc"].poll() is not None:
            # Process finished, but not yet marked. Mark it now.
            job["finished_time"] = now

    for job_id in finished_to_reap:
        _jobs.pop(job_id, None)


def _reap_finished_jobs():
    with _jobs_lock:
        _reap_finished_jobs_locked()


def inat_api_get(url, **kwargs):
    """A rate-limited GET request helper for the iNaturalist API."""
    global next_api_call_time
    with api_lock:
        now = time.time()
        if now < next_api_call_time:
            time.sleep(next_api_call_time - now)

        try:
            kwargs.setdefault("headers", INAT_HEADERS)
            kwargs.setdefault("timeout", 20)
            response = requests.get(url, **kwargs)
            next_api_call_time = time.time() + 1.0
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException:
            app.logger.exception("Error during iNaturalist API request.")
            next_api_call_time = time.time() + 1.0
            raise


app = Flask(__name__, static_url_path="/labels/static")

cmd_logger = logging.getLogger("cmd_logger")
api_error_logger = logging.getLogger("api_error_logger")


def _env_flag_enabled(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _has_handler(logger, handler_name):
    return any(
        getattr(handler, "_labels_handler_name", None) == handler_name
        for handler in logger.handlers
    )


def _add_rotating_file_handler(
    logger, handler_name, path, max_bytes, backup_count, formatter, level
):
    if _has_handler(logger, handler_name):
        return

    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count)
    handler._labels_handler_name = handler_name
    handler.setFormatter(formatter)
    handler.setLevel(level)
    logger.addHandler(handler)


def configure_file_logging(flask_app):
    flask_app.logger.setLevel(logging.WARNING)
    cmd_logger.setLevel(logging.INFO)
    api_error_logger.setLevel(logging.WARNING)

    if _env_flag_enabled("LABELS_DISABLE_FILE_LOGGING"):
        # Skip file handlers only. Records still propagate to the root logger
        # (and thus to stderr / the systemd journal). This flag disables file
        # logging, not all logging.
        return

    log_dir = os.environ.get("LABELS_LOG_DIR") or os.path.join(
        flask_app.root_path, "logs"
    )
    os.makedirs(log_dir, exist_ok=True)

    warning_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
    )
    command_formatter = logging.Formatter("%(asctime)s: %(message)s")

    handler_specs = (
        (flask_app.logger, "app_error_log", "error.log", 10, warning_formatter, logging.WARNING),
        (cmd_logger, "cmd_log", "app.log", 5, command_formatter, logging.INFO),
        (api_error_logger, "api_error_log", "api_error.log", 5, warning_formatter, logging.WARNING),
    )
    for logger, handler_name, filename, backup_count, formatter, level in handler_specs:
        _add_rotating_file_handler(
            logger,
            handler_name,
            os.path.join(log_dir, filename),
            1024 * 1024,
            backup_count,
            formatter,
            level,
        )


configure_file_logging(app)

# Only enable CORS when explicitly configured; same-origin requests do not need CORS
if ALLOWED_ORIGINS:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
    if origins:
        CORS(app, resources={r"/labels/*": {"origins": origins}})


# Helper function to extract observation type and ID from raw input
def extract_obs_id(obs_input):
    # Convert to lowercase for case-insensitive comparison
    input_lower = obs_input.lower()

    # Handles mo:XXXXX format separately for direct access
    if input_lower.startswith("mo:"):
        mo_number = obs_input[3:].strip()
        if mo_number.isdigit():
            return "mo_direct", mo_number

    # Original logic
    if "://" in obs_input:
        if "inaturalist.org/observations/" in input_lower:
            match = re.search(r"/observations/(\d+)", obs_input)
            if match:
                return "inat", match.group(1)
        elif "mushroomobserver.org" in input_lower:
            match = re.search(r"/(\d+)$", obs_input)
            if match:
                return "mo_direct", match.group(1)
    else:
        # Handle MOTOINAT/MOINAT/INATMO formats for conversion (MO -> iNat)
        if (
            input_lower.startswith("motoinat")
            or input_lower.startswith("moinat")
            or input_lower.startswith("inatmo")
        ):
            mo_number = re.search(r"\d+", obs_input)
            if mo_number:
                return "mo", mo_number.group(0)
        # Regular MO format - now treated as direct (pass through to generator)
        elif input_lower.startswith("mo"):
            mo_number = re.search(r"\d+", obs_input)
            if mo_number:
                return "mo_direct", mo_number.group(0)
        elif input_lower.startswith("bg") or input_lower.startswith("bugguide"):
            bg_match = re.match(r"^(bg|bugguide)\s*(\d+)$", input_lower)
            if bg_match:
                return "bg", bg_match.group(2)
        elif obs_input.isdigit():
            return "inat", obs_input
    raise ValueError(f"Invalid observation input: {obs_input}")


# Function to convert MO number to iNaturalist ID or return iNaturalist ID
def get_inat_id(obs_input):
    obs_type, obs_id = extract_obs_id(obs_input)

    # Direct MO observation - return with MO prefix (uppercase) for generator compatibility
    if obs_type == "mo_direct":
        return f"MO{obs_id}"

    if obs_type == "bg":
        return f"BG{obs_id}"

    # Convert MO to iNat (motoinat)
    if obs_type == "mo":
        try:
            result = subprocess.run(
                ["python", os.path.join(app.root_path, "motoinat.py"), "-q", obs_id],
                capture_output=True,
                text=True,
                check=True,
            )
            inat_id = result.stdout.strip()
            if inat_id.isdigit():
                return inat_id
            else:
                raise ValueError(f"No iNaturalist observation found for MO #{obs_id}")
        except subprocess.CalledProcessError as e:
            raise ValueError(
                f"Error converting MO #{obs_id} to iNaturalist: {e.stderr.strip()}"
            )

    # iNat ID
    return obs_id


@app.route("/")
@app.route("/labels")
@app.route("/labels/")
def labels():
    return render_template("index.html", default_label_fields=DEFAULT_LABEL_FIELDS)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


def lookup_batch_internal(obs_inputs):
    # Prepare containers
    results = [{"input": oi} for oi in obs_inputs]
    inat_ids = []
    inat_map_indices = {}
    mo_numbers = []
    mo_map_indices = {}

    # Resolve inputs to either iNat IDs or MO IDs
    for idx, obs_input in enumerate(obs_inputs):
        try:
            resolved = get_inat_id(obs_input)
        except ValueError as e:
            results[idx]["error"] = str(e)
            results[idx]["status"] = 400
            continue
        # Direct MO
        if isinstance(resolved, str) and resolved.upper().startswith("MO"):
            mo_num = resolved[2:]
            mo_numbers.append(mo_num)
            mo_map_indices.setdefault(mo_num, []).append(idx)
        elif isinstance(resolved, str) and resolved.upper().startswith("BG"):
            bg_num = resolved[2:]
            results[idx].update(
                {
                    "original_input": obs_input,
                    "inat_id": f"BG{bg_num}",
                    "scientific_name": "BugGuide",
                    "user_login": "",
                    "color": "black",
                    "iconic_taxon_name": "",
                    "taxon_color_group": "unknown",
                    "ofvs": [{"name": "BugGuide URL", "value": f"https://bugguide.net/node/view/{bg_num}"}],
                }
            )
        else:
            # iNat numeric ID
            inat_ids.append(str(resolved))
            inat_map_indices.setdefault(str(resolved), []).append(idx)

    # Batch fetch iNat observations in chunks to avoid throttling
    if inat_ids:
        global next_api_call_time
        id_to_result = {}
        chunk_size = 30  # Max 30 IDs per request is a safe bet
        for i in range(0, len(inat_ids), chunk_size):
            chunk = inat_ids[i : i + chunk_size]

            try:
                params = {"id": ",".join(chunk)}
                response = inat_api_get(
                    "https://api.inaturalist.org/v1/observations", params=params
                )
                data = response.json()
                for r in data.get("results", []):
                    if "id" in r:
                        id_to_result[str(r["id"])] = r
            except requests.exceptions.RequestException as e:
                # Apply a generic error to all inat IDs in the failed chunk
                msg = f"Error fetching iNaturalist data: {str(e)}"
                for inat_id in chunk:
                    for idx in inat_map_indices.get(inat_id, []):
                        if (
                            "error" not in results[idx]
                        ):  # Avoid overwriting previous errors
                            results[idx]["error"] = msg
                            results[idx]["status"] = 500
                continue

        # Fill results from the fetched data
        for inat_id in inat_ids:
            indices = inat_map_indices.get(inat_id, [])
            # Skip if an error was already recorded for this chunk
            if any("error" in results[idx] for idx in indices):
                continue
            r = id_to_result.get(inat_id)
            if not r:
                for idx in indices:
                    results[idx][
                        "error"
                    ] = f"iNaturalist Observation #{inat_id} does not exist"
                    results[idx]["status"] = 404
                continue
            taxon = r.get("taxon") or {}
            user = r.get("user") or {}
            scientific_name = taxon.get("name", "Unknown")
            user_login = user.get("login", "Unknown")
            iconic = taxon.get("iconic_taxon_name", "")
            color_group = taxon_color_group(iconic)
            color = legacy_color_for_taxon_group(color_group)
            for idx in indices:
                results[idx].update(
                    {
                        "original_input": obs_inputs[idx],
                        "inat_id": inat_id,
                        "scientific_name": scientific_name,
                        "user_login": user_login,
                        "color": color,
                        "iconic_taxon_name": iconic,
                        "taxon_color_group": color_group,
                        "ofvs": r.get("ofvs", []),
                    }
                )

    # Fetch MO observations (API supports detail=high, ids= may support multiple; use per-ID for safety)
    for mo_num in mo_numbers:
        try:
            api_url = f"https://mushroomobserver.org/api2/observations/{mo_num}.json?detail=high"
            mo_response = requests.get(api_url, timeout=20)
            mo_response.raise_for_status()
            mo_data = mo_response.json()
            result = None
            if mo_data and "results" in mo_data and mo_data["results"]:
                result = mo_data["results"][0]
                if isinstance(result, int):
                    # Fetch full detail via ids=
                    api_url = f"https://mushroomobserver.org/api2/observations?ids={mo_num}&detail=high"
                    mo_response = requests.get(api_url, timeout=20)
                    mo_response.raise_for_status()
                    mo_data = mo_response.json()
                    if mo_data and "results" in mo_data and mo_data["results"]:
                        result = mo_data["results"][0]
                    else:
                        result = None
            if not isinstance(result, dict):
                for idx in mo_map_indices.get(mo_num, []):
                    results[idx][
                        "error"
                    ] = f"No data found for Mushroom Observer #{mo_num}"
                    results[idx]["status"] = 404
                continue

            consensus = result.get("consensus") or {}
            owner = result.get("owner") or {}
            scientific_name = consensus.get("name") or result.get("name", "Unknown")
            user_login = (
                owner.get("login_name") or result.get("login_name") or "Unknown"
            )

            ofvs = []
            mo_url = f"https://mushroomobserver.org/obs/{mo_num}"
            ofvs.append({"name": "Mushroom Observer URL", "value": mo_url})
            if "herbarium_name" in result:
                ofvs.append(
                    {
                        "name": "Herbarium Name",
                        "value": result.get("herbarium_name", ""),
                    }
                )
            if "herbarium_id" in result:
                ofvs.append(
                    {
                        "name": "Herbarium Catalog Number",
                        "value": result.get("herbarium_id", ""),
                    }
                )
            if "sequences" in result and result["sequences"]:
                for sequence in result["sequences"]:
                    locus = sequence.get("locus", "").upper()
                    bases = sequence.get("bases", "")
                    if locus and bases:
                        locus_mapping = {
                            "ITS": "DNA Barcode ITS",
                            "LSU": "DNA Barcode LSU",
                            "TEF1": "DNA Barcode TEF1",
                            "EF1": "DNA Barcode TEF1",
                            "RPB1": "DNA Barcode RPB1",
                            "RPB2": "DNA Barcode RPB2",
                        }
                        field_name = locus_mapping.get(locus)
                        if field_name:
                            cleaned_bases = "".join(bases.split())
                            bp_count = len(cleaned_bases)
                            ofvs.append({"name": field_name, "value": f"{bp_count} bp"})
            for idx in mo_map_indices.get(mo_num, []):
                results[idx].update(
                    {
                        "original_input": obs_inputs[idx],
                        "inat_id": f"MO{mo_num}",
                        "scientific_name": scientific_name,
                        "user_login": user_login,
                        "color": "magenta",
                        "iconic_taxon_name": "Fungi",
                        "taxon_color_group": "fungi",
                        "ofvs": ofvs,
                    }
                )
        except Exception as e:
            app.logger.exception(e)
            for idx in mo_map_indices.get(mo_num, []):
                results[idx][
                    "error"
                ] = f"Error processing Mushroom Observer #{mo_num}: {str(e)}"
                results[idx]["status"] = 500

    # Normalize output: ensure items list of dicts with either error or data
    items = []
    for idx, base in enumerate(results):
        if "error" in base:
            items.append(
                {
                    "input": obs_inputs[idx],
                    "error": base["error"],
                    "status": base.get("status", 400),
                }
            )
        else:
            items.append(
                {
                    "original_input": base.get("original_input", obs_inputs[idx]),
                    "inat_id": base.get("inat_id", ""),
                    "scientific_name": base.get("scientific_name", "Unknown"),
                    "user_login": base.get("user_login", "Unknown"),
                    "color": base.get("color", "black"),
                    "iconic_taxon_name": base.get("iconic_taxon_name", ""),
                    "taxon_color_group": base.get("taxon_color_group", "unknown"),
                    "ofvs": base.get("ofvs", []),
                }
            )
    return {"items": items}


@app.route("/labels/lookup_batch", methods=["POST"])
def lookup_batch():
    obs_inputs = request.form.getlist("obs_ids[]")
    if not obs_inputs:
        return jsonify({"error": "No observation IDs provided"}), 400
    if len(obs_inputs) > MAX_OBS_PER_REQUEST:
        return (
            jsonify(
                {
                    "error": f"Too many observations in one request (max {MAX_OBS_PER_REQUEST})"
                }
            ),
            400,
        )
    payload = lookup_batch_internal(obs_inputs)
    return jsonify(payload)


@app.route("/labels/submit", methods=["POST"])
def submit():
    try:
        raw_observations = [
            obs.strip() for obs in request.form.getlist("observations[]") if obs.strip()
        ]
        if not raw_observations:
            return "No observations provided", 400
        if len(raw_observations) > MAX_OBS_PER_REQUEST:
            return (
                f"Too many observations in one request (max {MAX_OBS_PER_REQUEST})",
                400,
            )

        # Resolve inputs into either iNat IDs or MO IDs
        resolved = []
        for obs in raw_observations:
            try:
                rid = get_inat_id(obs)
                resolved.append(rid)
            except ValueError as e:
                app.logger.warning(str(e))
                # Skip invalid entries
                continue

        # Partition into iNat and MO
        inat_ids = [
            str(x)
            for x in resolved
            if not (isinstance(x, str) and x.upper().startswith("MO"))
        ]

        # Batch fetch iNat observations in chunks
        id_to_inat = {}
        if inat_ids:
            CHUNK = 50
            for i in range(0, len(inat_ids), CHUNK):
                chunk = inat_ids[i : i + CHUNK]
                try:
                    params = {"id": ",".join(chunk)}
                    resp = inat_api_get(
                        "https://api.inaturalist.org/v1/observations",
                        params=params,
                        timeout=30,
                    )
                    data = resp.json()
                    for r in data.get("results", []):
                        if "id" in r:
                            id_to_inat[str(r["id"])] = r
                except requests.exceptions.RequestException as e:
                    app.logger.warning(
                        f"iNat batch fetch failed for chunk starting {chunk[0]}: {e}"
                    )
                    continue

        def safe_csv_field(val):
            try:
                s = str(val)
            except Exception:
                s = ""
            if s and s[0] in ("=", "+", "-", "@"):
                return "'" + s
            return s

        csv_data = [["ID", "Observation Number", "Scientific Name", "Observer"]]
        valid_counter = 0

        # Build CSV rows in original order
        for rid in resolved:
            rid_str = str(rid)
            # MO observations (fetch per-ID with detail fallback)
            if rid_str.upper().startswith("MO"):
                valid_counter += 1
                mo_number = rid_str[2:]
                try:
                    mo_response = requests.get(
                        f"https://mushroomobserver.org/api2/observations/{mo_number}.json?detail=high",
                        timeout=20,
                    )
                    mo_response.raise_for_status()
                    mo_data = mo_response.json()
                    if (
                        mo_data
                        and "results" in mo_data
                        and mo_data["results"]
                        and isinstance(mo_data["results"][0], int)
                    ):
                        mo_response = requests.get(
                            f"https://mushroomobserver.org/api2/observations?ids={mo_number}&detail=high",
                            timeout=20,
                        )
                        mo_response.raise_for_status()
                        mo_data = mo_response.json()
                    if (
                        mo_data
                        and "results" in mo_data
                        and mo_data["results"]
                        and isinstance(mo_data["results"][0], dict)
                    ):
                        result = mo_data["results"][0]

                        consensus = result.get("consensus") or {}
                        owner = result.get("owner") or {}
                        scientific_name = consensus.get("name") or result.get(
                            "name", "Unknown"
                        )
                        user_login = owner.get("login_name") or result.get(
                            "login_name", "Unknown"
                        )

                        csv_data.append(
                            [
                                valid_counter,
                                safe_csv_field(rid_str),
                                safe_csv_field(scientific_name),
                                safe_csv_field(user_login),
                            ]
                        )
                    else:
                        csv_data.append(
                            [
                                valid_counter,
                                safe_csv_field(rid_str),
                                "Unknown",
                                "Unknown",
                            ]
                        )
                except Exception as e:
                    app.logger.warning(f"Error fetching MO data: {str(e)}")
                    csv_data.append(
                        [valid_counter, rid_str, "Unknown (API Error)", "Unknown"]
                    )
                continue
            # iNaturalist observation from batch map
            r = id_to_inat.get(rid_str)
            if r:
                valid_counter += 1
                taxon = r.get("taxon") or {}
                user = r.get("user") or {}
                scientific_name = taxon.get("name", "Unknown")
                user_login = user.get("login", "Unknown")
                csv_data.append(
                    [
                        valid_counter,
                        safe_csv_field(rid_str),
                        safe_csv_field(scientific_name),
                        safe_csv_field(user_login),
                    ]
                )
            # If r missing, skip adding a row to preserve numbering semantics like previous implementation

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(csv_data)
        csv_content = output.getvalue()

        return (
            csv_content,
            200,
            {
                "Content-Type": "text/csv",
                "Content-Disposition": "attachment; filename=observations.csv",
            },
        )
    except Exception as e:
        app.logger.exception(e)
        return "An internal error occurred while generating the CSV file.", 500


# Streaming printing support
@app.route("/labels/print_start", methods=["POST"])
def print_start():
    fmt = (request.form.get("format") or "rtf").lower()
    if fmt not in ("rtf", "pdf"):
        app.logger.warning(f"print_start: Invalid format requested: {fmt}")
        return jsonify({"error": "Invalid format"}), 400

    omit_qr_codes = request.form.get("omit_qr_codes")
    print_duplicate_labels = bool(request.form.get("print_duplicate_labels"))

    # Label sort order.  An empty value keeps inat.label.py's default
    # observation-number sort, so no --sort flag is passed in that case.
    sort_mode = (request.form.get("sort") or "").strip().lower()
    sort_field = (request.form.get("sort_field") or "").strip()
    if sort_mode and sort_mode not in SORT_MODES:
        app.logger.warning(f"print_start: Invalid sort mode requested: {sort_mode}")
        return jsonify({"error": "Invalid sort order"}), 400
    if sort_mode == "custom" and not sort_field:
        app.logger.warning("print_start: Custom sort requested without a field name")
        return jsonify({"error": "Sorting by field requires a field name"}), 400
    if sort_mode != "custom":
        sort_field = ""
    raw_observations = request.form.getlist("observations[]")
    if not raw_observations:
        app.logger.warning("print_start: No observations provided")
        return jsonify({"error": "No observations provided"}), 400
    if len(raw_observations) > MAX_OBS_PER_REQUEST:
        app.logger.warning(
            f"print_start: Too many observations requested: {len(raw_observations)}, max is {MAX_OBS_PER_REQUEST}"
        )
        return (
            jsonify(
                {
                    "error": f"Too many observations in one request (max {MAX_OBS_PER_REQUEST})"
                }
            ),
            400,
        )
    with _jobs_lock:
        _reap_finished_jobs_locked()  # clean finished/expired entries immediately

        active = 0
        for job in _jobs.values():
            proc = job.get("proc")
            if proc and proc.poll() is None:
                active += 1

        if active >= MAX_CONCURRENT_JOBS:
            error_message = f"Too many concurrent jobs ({active}), max is {MAX_CONCURRENT_JOBS}. Please try again shortly."
            app.logger.warning(f"print_start: {error_message}")
            return jsonify({"error": error_message}), 429

    inat_ids = []
    bg_omitted = False
    is_minilabel = bool(request.form.get("minilabel"))

    for obs in raw_observations:
        try:
            inat_id = str(get_inat_id(obs))
            if inat_id.upper().startswith("BG"):
                if not is_minilabel:
                    bg_omitted = True
                    continue
            inat_ids.append(inat_id)
            if print_duplicate_labels:
                inat_ids.append(inat_id)
        except ValueError as e:
            app.logger.warning(str(e))
            continue

    if not inat_ids:
        app.logger.warning(
            "print_start: No valid observations found after processing raw input."
        )
        if bg_omitted:
            return jsonify({"error": "No labels were generated because all provided observations were BugGuide entries, which are only supported when minilabels are enabled."}), 400
        return jsonify({"error": "No valid observations provided"}), 400

    script_path = os.path.join(app.root_path, "inat.label.py")
    static_dir = os.path.join(app.root_path, "static")
    job_id = str(uuid4())
    job_dir = os.path.join(static_dir, "jobs", job_id)
    filename = "labels.rtf" if fmt == "rtf" else "labels.pdf"
    output_path = os.path.join(job_dir, filename)
    os.makedirs(job_dir, exist_ok=True)
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    command = [
        sys.executable,
        "-u",
        script_path,
        *inat_ids,
        *(["--rtf", output_path] if fmt == "rtf" else ["--pdf", output_path]),
    ]
    if omit_qr_codes:
        command.append("--no-qr")
    if request.form.get("minilabel"):
        command.append("--minilabel")
        minilabel_size = request.form.get("minilabel_size", "").strip()
        if minilabel_size and minilabel_size.isdecimal():
            size_val = int(minilabel_size)
            if 1 <= size_val <= 10:
                command.extend(["--minilabel-size", str(size_val)])
    if request.form.get("common_names"):
        command.append("--common-names")
    if request.form.get("omit_notes"):
        command.append("--omit-notes")
    if sort_mode:
        command.extend(["--sort", sort_mode])
        if sort_mode == "custom":
            command.extend(["--sort-field", sort_field])
    if request.form.get("use_custom"):
        custom_args = request.form.getlist("custom_args[]")
        if custom_args:
            command.append("--custom")
            # Join all custom fields with commas as inat.label.py expects a comma-separated list
            command.append(", ".join(custom_args))
    cmd_logger.info(" ".join(command))
    app.logger.debug(f"Starting streaming command: {' '.join(command)}")

    # Start subprocess for streaming
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,  # line-buffered
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    with _jobs_lock:
        _jobs[job_id] = {
            "proc": proc,
            "output_path": output_path,
            "filename": filename,
        }

    return jsonify({
        "job_id": job_id,
        "warning": "BugGuide observations were omitted because minilabels are not enabled." if bg_omitted else None
    })


@app.route("/labels/print_stream")
def print_stream():
    job_id = request.args.get("job_id")

    with _jobs_lock:
        if not job_id or job_id not in _jobs:
            # Job is already gone, possibly reaped.
            # Return an immediate SSE 'done' event with an error.
            def generate_reaped_error():
                error_payload = json.dumps(
                    {
                        "success": False,
                        "error": "Job not found. It may have been completed and cleaned up.",
                        "exit_code": -1,
                    }
                )
                yield f"event: done\ndata: {error_payload}\n\n"

            return app.response_class(
                generate_reaped_error(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        job = _jobs[job_id]
        proc = job["proc"]
        output_path = job["output_path"]

    rel_path = os.path.relpath(
        output_path, os.path.join(app.root_path, "static")
    ).replace("\\", "/")
    download_url = url_for("static", filename=rel_path)

    def generate():
        try:
            # Stream already-written and future output
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\n")
                # Small debug hook if you want:
                # app.logger.debug(f"SSE log line: {line!r}")

                yield f"event: log\ndata: {json.dumps(line)}\n\n"

            # When the loop ends, the process has finished
            exit_code = proc.wait()

            if os.path.exists(output_path):
                done_payload = json.dumps(
                    {
                        "success": True,
                        "download_url": download_url,
                        "exit_code": exit_code,
                    }
                )
            else:
                error_message = (
                    f"Output file was not created. "
                    f"Process exited with code {exit_code}."
                )
                done_payload = json.dumps(
                    {
                        "success": False,
                        "error": error_message,
                        "exit_code": exit_code,
                    }
                )

            yield f"event: done\ndata: {done_payload}\n\n"

        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception as e:
                        app.logger.debug(f"Error waiting for process termination: {e}")
                        pass
            except Exception as e:
                app.logger.debug(f"Error terminating process: {e}")
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception as e:
                app.logger.debug(f"Error closing stdout: {e}")

            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["finished_time"] = time.time()

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx hint; harmless even if not used
        },
    )


def _clean_locality_text(value):
    """Apply only low-risk cleanup to an upstream locality label."""
    if not isinstance(value, str):
        return ""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    while len(parts) >= 3 and parts[-1].casefold() == parts[-2].casefold():
        parts.pop()
    return ", ".join(parts)


_COORDINATE_PAIR_RE = re.compile(
    r"^[-+]?\d{1,3}(?:\.\d+)?\s*,\s*[-+]?\d{1,3}(?:\.\d+)?$"
)


def _looks_like_coordinates(value):
    """True for a bare "lat, lng" pair, which is not a place name."""
    return bool(_COORDINATE_PAIR_RE.match(value.strip())) if value else False


def _concise_locality(observation):
    """Prefer an existing human-readable locality without another API lookup."""
    for key in ("locality", "location_name", "where"):
        cleaned = _clean_locality_text(observation.get(key))
        if cleaned:
            return cleaned

    location = observation.get("location")
    if isinstance(location, dict):
        for nested_key in ("display_name", "name", "text"):
            cleaned = _clean_locality_text(location.get(nested_key))
            if cleaned:
                return cleaned

    cleaned_place_guess = _clean_locality_text(observation.get("place_guess"))
    if cleaned_place_guess:
        return cleaned_place_guess

    if isinstance(location, dict):
        return _clean_locality_text(location.get("location"))

    # iNaturalist's ``location`` is the "lat,lng" pair.  A coordinate string is
    # not a locality, so report nothing rather than something that reads like a
    # place name.
    cleaned_location = _clean_locality_text(location)
    if _looks_like_coordinates(cleaned_location):
        return ""
    return cleaned_location


def _thumbnail_url(observation, *, allow_photo_url=False):
    """Return only an upstream thumbnail/square image URL, if one is available."""
    candidates = []
    for key in ("photos", "images"):
        value = observation.get(key)
        if isinstance(value, list):
            candidates.extend(value[:1])
    for key in ("primary_image", "photo", "image"):
        value = observation.get(key)
        if value:
            candidates.append(value)

    thumbnail_keys = ("square_url", "thumbnail_url", "thumb_url", "small_url")
    if allow_photo_url:
        thumbnail_keys += ("url",)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in thumbnail_keys:
            url = candidate.get(key)
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _mushroom_observer_thumbnail_url(observation):
    """Return Mushroom Observer's 160px thumbnail for the primary image."""
    direct_url = _thumbnail_url(observation)
    if direct_url:
        return direct_url

    image_id = observation.get("primary_image_id")
    if isinstance(image_id, bool):
        return None
    if isinstance(image_id, int):
        normalized_id = str(image_id) if image_id > 0 else ""
    elif isinstance(image_id, str):
        normalized_id = image_id.strip()
        if not normalized_id.isdigit() or int(normalized_id) < 1:
            normalized_id = ""
    else:
        normalized_id = ""

    if not normalized_id:
        return None
    return f"https://mushroomobserver.org/images/thumb/{normalized_id}.jpg"


def _date_key(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _daily_counts_from_histogram(payload):
    """Normalize the iNaturalist histogram response into ISO-date counts."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return {}

    mappings = [results] if any(isinstance(value, int) for value in results.values()) else [
        value for value in results.values() if isinstance(value, dict)
    ]
    daily_counts = defaultdict(int)
    for mapping in mappings:
        for raw_date, raw_count in mapping.items():
            date_key = _date_key(raw_date)
            if date_key is None or not isinstance(raw_count, int) or raw_count < 1:
                continue
            daily_counts[date_key] += raw_count
    return dict(daily_counts)


_histogram_cache = OrderedDict()
_histogram_cache_lock = threading.Lock()


def _inat_daily_counts(histogram_params):
    """Fetch per-day counts, reusing a recent result for identical filters.

    The Add Observations search re-fires on every debounced keystroke while the
    filters are being typed, and ``inat_api_get`` serializes all iNaturalist
    traffic behind one lock, so an uncached histogram would hold that lock for
    an extra round trip per edit and stall unrelated lookups.
    """
    key = tuple(sorted((str(k), str(v)) for k, v in histogram_params.items()))
    now = time.time()

    with _histogram_cache_lock:
        entry = _histogram_cache.get(key)
        if entry is not None:
            expires_at, cached_counts = entry
            if expires_at > now:
                _histogram_cache.move_to_end(key)
                return cached_counts
            del _histogram_cache[key]

    response = inat_api_get(
        "https://api.inaturalist.org/v1/observations/histogram",
        params=histogram_params,
        timeout=30,
    )
    daily_counts = _daily_counts_from_histogram(response.json())

    with _histogram_cache_lock:
        _histogram_cache[key] = (
            time.time() + INAT_HISTOGRAM_CACHE_TTL,
            daily_counts,
        )
        _histogram_cache.move_to_end(key)
        while len(_histogram_cache) > INAT_HISTOGRAM_CACHE_MAX_ENTRIES:
            _histogram_cache.popitem(last=False)

    return daily_counts


def _windows_for_complete_daily_counts(
    daily_counts, total_count, requested_start, requested_end
):
    """Build windows only when every upstream match has a usable selected date."""
    if total_count <= MAX_OBS_PER_REQUEST:
        return []
    dated_total = sum(daily_counts.values())
    if dated_total != total_count:
        app.logger.warning(
            "Skipping date windows because dated count %s did not match total %s",
            dated_total,
            total_count,
        )
        return []
    try:
        return build_date_windows(
            daily_counts,
            requested_start,
            requested_end,
            cap=MAX_OBS_PER_REQUEST,
            newest_first=True,
        )
    except ValueError:
        app.logger.warning(
            "Skipping date windows because the requested range was invalid",
            exc_info=True,
        )
        return []


@app.route("/labels/find_observations", methods=["POST"])
def find_observations():
    """Find observation IDs by date range, username, and taxon (including descendants)."""
    d1_str = (request.form.get("d1") or "").strip()
    d2_str = (request.form.get("d2") or "").strip()
    username_raw = (request.form.get("username") or "").strip()
    username_inat = username_raw.replace(" ", "_")
    username_mo = username_raw
    taxon_input = (request.form.get("taxon") or "").strip()
    source = request.form.get("source", "inat").strip().lower()
    date_mode = request.form.get("date_mode", "observed").strip().lower()

    if source not in ("inat", "mo"):
        return jsonify({"error": "Unsupported source"}), 400

    if date_mode not in ("observed", "created"):
        return jsonify({"error": "Unsupported date_mode. Use 'observed' or 'created'."}), 400

    if not d1_str or not d2_str or not username_raw:
        missing_fields = []
        if not d1_str:
            missing_fields.append("Start Date")
        if not d2_str:
            missing_fields.append("End Date")
        if not username_raw:
            missing_fields.append("Username")
        return (
            jsonify({"error": f'Missing required fields: {", ".join(missing_fields)}'}),
            400,
        )

    # Default taxon by source when left blank
    if not taxon_input:
        taxon_input = "Fungi" if source == "mo" else "Life"

    # Validate dates YYYY-MM-DD
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d1_str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}$", d2_str
    ):
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Query observations
    found = []
    cap = MAX_OBS_PER_REQUEST + 1
    current_batch = []
    total_count = 0
    windows = []

    if source == "inat":
        # Resolve taxon_id (accept numeric id or search by name)
        taxon_id = None
        if taxon_input.isdigit():
            taxon_id = int(taxon_input)
        else:
            try:
                resp = inat_api_get(
                    "https://api.inaturalist.org/v1/taxa",
                    params={"q": taxon_input, "per_page": 1},
                    timeout=15,
                )
                tdata = resp.json()
                if tdata.get("results"):
                    taxon_id = tdata["results"][0].get("id")
                else:
                    return jsonify({"error": f"Taxon not found: {taxon_input}"}), 404
            except requests.RequestException as e:
                api_error_logger.warning(
                    f"Taxon lookup failed: {str(e)}", exc_info=True
                )
                return jsonify({"error": f"Error looking up taxon: {str(e)}"}), 500

        inat_search_params = {
            "user_login": username_inat,
            "taxon_id": taxon_id,
        }
        if date_mode == "created":
            inat_search_params["created_d1"] = d1_str
            inat_search_params["created_d2"] = d2_str
        else:
            inat_search_params["d1"] = d1_str
            inat_search_params["d2"] = d2_str

        last_id = 0
        first_page = True
        try:
            while len(current_batch) < cap:
                params = {
                    **inat_search_params,
                    "per_page": 200,
                    "order": "asc",
                    "order_by": "id",
                }
                if last_id > 0:
                    params["id_above"] = last_id

                resp = inat_api_get(
                    "https://api.inaturalist.org/v1/observations",
                    params=params,
                    timeout=30,
                )
                data = resp.json()
                if first_page:
                    # Only the first page reports the true match count.
                    # ``id_above`` is a filter, so every later page reports the
                    # matches still ahead of the cursor, not the full total.
                    upstream_total = data.get("total_results")
                    if isinstance(upstream_total, int) and upstream_total >= 0:
                        total_count = upstream_total
                    first_page = False
                results = data.get("results", [])
                if not results:
                    break

                for r in results:
                    if len(current_batch) >= cap:
                        break
                    oid = r.get("id")
                    if oid:
                        last_id = oid
                    taxon = r.get("taxon") or {}

                    iconic = taxon.get("iconic_taxon_name", "")
                    color_group = taxon_color_group(iconic)
                    color = legacy_color_for_taxon_group(color_group)

                    user_login = (r.get("user") or {}).get("login") or username_inat
                    current_batch.append(
                        {
                            "id": oid,
                            "inat_id": str(oid),
                            "scientific_name": taxon.get("name", ""),
                            "user_login": user_login,
                            "iconic_taxon_name": iconic,
                            "taxon_color_group": color_group,
                            "observed_on": r.get("observed_on"),
                            "place_guess": _concise_locality(r),
                            "photo_url": _thumbnail_url(r, allow_photo_url=True),
                            "color": color,
                        }
                    )
            found = current_batch
            total_count = max(total_count, len(current_batch))

            if total_count > MAX_OBS_PER_REQUEST:
                histogram_params = {
                    **inat_search_params,
                    "interval": "day",
                    "date_field": date_mode,
                }
                try:
                    daily_counts = _inat_daily_counts(histogram_params)
                    windows = _windows_for_complete_daily_counts(
                        daily_counts,
                        total_count,
                        d1_str,
                        d2_str,
                    )
                except (requests.RequestException, ValueError) as e:
                    api_error_logger.warning(
                        "iNaturalist date-window histogram failed: %s",
                        str(e),
                        exc_info=True,
                    )
        except requests.RequestException as e:
            api_error_logger.warning(
                f"Observation fetch for user '{username_inat}' failed: {str(e)}",
                exc_info=True,
            )
            error_message = f"Error fetching observations: {str(e)}"
            try:
                if e.response:
                    error_details = e.response.json()
                    if "error" in error_details:
                        error_message = (
                            f"Error fetching observations: {error_details['error']}"
                        )
            except ValueError:
                pass
            return jsonify({"error": error_message}), 500
    else:
        # source == "mo"
        try:
            mo_params = {
                "user": username_mo,
                "detail": "low",
                "format": "json",
            }
            if date_mode == "created":
                mo_params["created_at"] = f"{d1_str}-{d2_str}"
            else:
                mo_params["date"] = f"{d1_str}-{d2_str}"
            if not taxon_input.isdigit():
                mo_params["children_of"] = taxon_input
            else:
                return (
                    jsonify(
                        {
                            "error": "Mushroom Observer taxon lookup in this modal expects a taxon name, not a numeric ID."
                        }
                    ),
                    400,
                )

            daily_counts = defaultdict(int)
            mo_result_count = 0
            mo_dates_complete = True
            page = 1
            while True:
                try:
                    resp = requests.get(
                        "https://mushroomobserver.org/api2/observations",
                        params={**mo_params, "page": page},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as e:
                    if len(current_batch) < cap:
                        raise
                    mo_dates_complete = False
                    api_error_logger.warning(
                        "Mushroom Observer date-window page %s failed: %s",
                        page,
                        str(e),
                        exc_info=True,
                    )
                    break
                upstream_total = next(
                    (
                        data.get(key)
                        for key in (
                            # api2's own key; the rest are defensive fallbacks.
                            "number_of_records",
                            "number_of_results",
                            "total_results",
                            "total",
                        )
                        if isinstance(data.get(key), int) and data.get(key) >= 0
                    ),
                    None,
                )
                if upstream_total is not None:
                    total_count = upstream_total
                results = data.get("results", [])
                if not results:
                    break
                mo_result_count += len(results)

                for r in results:
                    selected_date = (
                        r.get("created_at")
                        if date_mode == "created"
                        else r.get("date") or r.get("when")
                    )
                    date_key = _date_key(selected_date)
                    if date_key:
                        daily_counts[date_key] += 1

                    if len(current_batch) >= cap:
                        continue

                    obs_date = r.get("date") or r.get("when") or ""
                    oid = r.get("id")
                    if not oid:
                        continue

                    sci_name = r.get("consensus_name") or r.get("name") or ""
                    u_login = username_mo

                    current_batch.append(
                        {
                            "id": oid,
                            "inat_id": f"MO{oid}",
                            "scientific_name": sci_name,
                            "user_login": u_login,
                            "iconic_taxon_name": "Fungi",
                            "taxon_color_group": "fungi",
                            "observed_on": obs_date,
                            "place_guess": _concise_locality(r),
                            "photo_url": _mushroom_observer_thumbnail_url(r),
                            "color": "magenta",
                        }
                    )

                if data.get("number_of_pages", 1) <= page:
                    break
                if page >= MO_MAX_WINDOW_PAGES and len(current_batch) >= cap:
                    # The preview rows are already filled; the remaining pages
                    # would be fetched only to tally dates.  Give up on the
                    # window chips rather than hold the worker any longer.
                    mo_dates_complete = False
                    api_error_logger.info(
                        "Stopped Mushroom Observer date-window paging for '%s' "
                        "after %s pages (budget MO_MAX_WINDOW_PAGES)",
                        username_mo,
                        page,
                    )
                    break
                page += 1

            found = current_batch
            total_count = max(total_count, mo_result_count)
            if mo_dates_complete:
                windows = _windows_for_complete_daily_counts(
                    daily_counts,
                    total_count,
                    d1_str,
                    d2_str,
                )
        except requests.RequestException as e:
            api_error_logger.warning(
                f"Mushroom Observer fetch for user '{username_mo}' failed: {str(e)}",
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": f"Error fetching Mushroom Observer observations: {str(e)}"
                    }
                ),
                500,
            )

    found.reverse()
    total_count = max(total_count, len(found))
    response_payload = {
        "count": len(found),
        "total_count": total_count,
        "items": found,
    }
    if windows:
        response_payload["windows"] = windows
    return jsonify(response_payload), 200


@app.route("/labels/help")
def serve_help():
    return app.send_static_file("help.html")


@app.route("/labels/todo", methods=["GET", "POST"])
def todo():
    todo_file = os.path.join(app.root_path, "static", "todos.txt")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        suggestion = request.form.get("suggestion", "").strip()

        # Sanitize input: allow only alphanumeric, spaces, and some punctuation, including Spanish characters
        name = re.sub(r"[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]", "", name)
        suggestion = re.sub(r"[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]", "", suggestion)

        if name and suggestion:
            with open(todo_file, "a") as f:
                submitted_on = time.strftime("%Y-%m-%d", time.gmtime())
                f.write(f"[{submitted_on}] {name}: {suggestion}\n")
        return redirect(url_for("todo"))

    todos = []
    if os.path.exists(todo_file):
        with open(todo_file, "r") as f:
            todos = [line.strip() for line in f.readlines()]
    return render_template("todo.html", todos=todos)


# Start a background thread to reap finished jobs
def _reaper_thread():
    while True:
        time.sleep(10)
        _reap_finished_jobs()


reaper = threading.Thread(target=_reaper_thread, daemon=True)
reaper.start()

if __name__ == "__main__":
    # Only for local dev; never auto-enable from env
    app.run(debug=False)
