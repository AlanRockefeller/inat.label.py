from flask import (
    Flask,
    g,
    has_request_context,
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
import importlib.util
from collections import defaultdict, OrderedDict
from datetime import date
from uuid import uuid4
from functools import partial, cmp_to_key

import threading
from logging.handlers import RotatingFileHandler

from date_windows import build_date_windows
from ratelimit import RateLimiter
import usage_stats

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
INAT_MIN_REQUEST_INTERVAL = float(os.environ.get("INAT_MIN_REQUEST_INTERVAL", "1.0"))

# Hardening settings
MAX_OBS_PER_REQUEST = int(os.environ.get("MAX_OBS_PER_REQUEST", "500"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
# Mushroom Observer has no histogram endpoint, so per-day counts for the date
# windows can only come from walking result pages.  Each page is ~1000 records
# and a few seconds upstream, and this app runs on a single Gunicorn worker, so
# the walk is bounded; past the budget the counts are marked incomplete and the
# window chips are skipped rather than tying up the worker.
MO_MAX_WINDOW_PAGES = int(os.environ.get("MO_MAX_WINDOW_PAGES", "5"))
# Bound cursor-based iNaturalist searches even when local filtering prevents the
# preview batch from filling.
INAT_MAX_SEARCH_PAGES = int(os.environ.get("INAT_MAX_SEARCH_PAGES", "10"))
# The daily counts behind the date windows are identical for identical filters,
# so a short-lived cache keeps debounced keystrokes off the rate-limited,
# lock-serialized iNaturalist client.
INAT_HISTOGRAM_CACHE_TTL = int(os.environ.get("INAT_HISTOGRAM_CACHE_TTL", "300"))
INAT_HISTOGRAM_CACHE_MAX_ENTRIES = 64
INAT_OBSERVATION_FIELD_CACHE_TTL = int(
    os.environ.get("INAT_OBSERVATION_FIELD_CACHE_TTL", "300")
)
INAT_OBSERVATION_FIELD_CACHE_MAX_ENTRIES = 64
INAT_OBSERVATION_FIELD_QUERY_MAX_LENGTH = 100
INAT_OBSERVATION_FIELD_RESULT_LIMIT = 12
FINISHED_JOB_TTL = int(
    os.environ.get("FINISHED_JOB_TTL", "300")
)  # Time in seconds to keep finished jobs
ENABLE_MO_DEBUG = bool(int(os.environ.get("ENABLE_MO_DEBUG", "0")))
# Label sort orders accepted by inat.label.py's --sort option
SORT_MODES = ("none", "date", "date-desc", "voucher", "custom")
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS"
)  # comma-separated list of allowed origins

# Every observation input that needs an MO -> iNat conversion costs one
# subprocess and one upstream request, run inline while the request holds a
# worker thread.  Without a cap, a single 500-entry request spawns 500 of them.
MAX_MO_CONVERSIONS_PER_REQUEST = int(
    os.environ.get("MAX_MO_CONVERSIONS_PER_REQUEST", "25")
)
MOTOINAT_TIMEOUT_SECONDS = int(os.environ.get("MOTOINAT_TIMEOUT_SECONDS", "45"))
# An SSE stream occupies a worker thread for the life of the job.  Gunicorn
# currently runs 1 worker with 4 threads, so this stays at the job cap: three
# streams can be open and a thread is still free to serve everything else.
MAX_CONCURRENT_STREAMS = int(
    os.environ.get("MAX_CONCURRENT_STREAMS", str(MAX_CONCURRENT_JOBS))
)
# Requests are small forms; anything larger is either a mistake or an attempt to
# tie up the worker parsing a body that will be rejected anyway.
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

# Sliding-window request limits as {bucket: (max_requests, window_seconds)}.
# Buckets are keyed by client IP; see _rate_limit_buckets() for the endpoint map.
RATE_LIMIT_RULES = {
    # Label generation: the most expensive thing an anonymous client can start.
    "print": (int(os.environ.get("RATE_LIMIT_PRINT_PER_MIN", "12")), 60),
    "print_daily": (int(os.environ.get("RATE_LIMIT_PRINT_PER_DAY", "300")), 86400),
    # Multi-page observation searches: the heaviest read path.
    "search": (int(os.environ.get("RATE_LIMIT_SEARCH_PER_MIN", "30")), 60),
    # Row lookups and field autocomplete fire while the user types, so these are
    # generous; the real upstream cost is already paced at one request/second
    # globally, and these limits exist to stop one client hogging threads.
    "lookup": (int(os.environ.get("RATE_LIMIT_LOOKUP_PER_MIN", "120")), 60),
    "autocomplete": (int(os.environ.get("RATE_LIMIT_AUTOCOMPLETE_PER_MIN", "120")), 60),
    "client_event": (int(os.environ.get("RATE_LIMIT_CLIENT_EVENT_PER_MIN", "30")), 60),
    # To-Do suggestions stay open to everyone, but bounded per day.
    "todo": (int(os.environ.get("RATE_LIMIT_TODO_PER_DAY", "10")), 86400),
    "default": (int(os.environ.get("RATE_LIMIT_DEFAULT_PER_MIN", "240")), 60),
}
# Endpoints whose work is cheap enough that only the catch-all applies.
RATE_LIMIT_ENDPOINTS = {
    "print_start": ("print", "print_daily"),
    "find_observations": ("search",),
    "observation_fields": ("autocomplete",),
    "client_event": ("client_event",),
    "lookup_batch": ("lookup",),
    "submit": ("lookup",),
    "todo": ("todo",),
}
# Endpoints where the endpoint-specific limit applies to submissions only, so
# that reading the page stays unlimited.
RATE_LIMIT_WRITE_ONLY_ENDPOINTS = {"todo"}
# Sort field names are forwarded to inat.label.py as a command argument.  An
# allowlist keeps that boundary allowlist-shaped instead of blacklist-shaped.
SORT_FIELD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()/&.,'#_-]{0,63}$")
# Custom label field names travel to the generator the same way.
CUSTOM_FIELD_RE = re.compile(r"^[+-]?[A-Za-z0-9][A-Za-z0-9 ()/&.'#_-]{0,63}$")
MAX_CUSTOM_FIELDS = int(os.environ.get("MAX_CUSTOM_FIELDS", "40"))

# Security logging deliberately distinguishes commodity Internet scans from
# requests crafted around labelmaker's real controls.  Values are JSON-escaped
# before logging, and a per-value bound prevents a single request from turning
# into an unbounded log record while retaining enough of the payload to
# investigate it.
MAX_DIAGNOSTIC_VALUE_LENGTH = int(
    os.environ.get("MAX_DIAGNOSTIC_VALUE_LENGTH", "4096")
)
# Values arrive straight off the wire, where MAX_CONTENT_LENGTH alone still
# allows megabytes.  Classification only ever needs the head of a value, so
# scanning is bounded well below that: an attacker must not be able to choose
# how much text the regex engine walks.
MAX_DIAGNOSTIC_SCAN_LENGTH = int(
    os.environ.get("MAX_DIAGNOSTIC_SCAN_LENGTH", "2048")
)
# Recorded indicators are bounded far more tightly than MAX_DIAGNOSTIC_VALUE_LENGTH.
# A security log that an attacker can rotate out of retention is worse than no
# log at all, so one request can only ever contribute a small, fixed record.
MAX_DIAGNOSTIC_INDICATORS = int(os.environ.get("MAX_DIAGNOSTIC_INDICATORS", "5"))
MAX_DIAGNOSTIC_INDICATOR_LENGTH = int(
    os.environ.get("MAX_DIAGNOSTIC_INDICATOR_LENGTH", "256")
)
# Nested containers are walked to a fixed depth.  Without this, a small payload
# of nested JSON arrays exhausts the C stack inside the walker.
MAX_DIAGNOSTIC_DEPTH = int(os.environ.get("MAX_DIAGNOSTIC_DEPTH", "10"))
# Per-value bounds still leave a record that is wide rather than deep: one
# event can carry many fields, or a list of many individually-bounded
# strings.  A single record larger than the handler's maxBytes rotates every
# backup out of retention in one request, so the assembled record is bounded
# as well as each value inside it.
MAX_DIAGNOSTIC_RECORD_LENGTH = int(
    os.environ.get("MAX_DIAGNOSTIC_RECORD_LENGTH", "65536")
)
# Kept verbatim when a record has to be truncated: without them the surviving
# line cannot be tied back to the request that produced it.
DIAGNOSTIC_RECORD_IDENTITY_FIELDS = (
    "event",
    "request_id",
    "client_ip",
    "method",
    "path",
    "endpoint",
)
# Rotated 1 MB at a time; see configure_file_logging.
SECURITY_LOG_BACKUP_COUNT = int(os.environ.get("SECURITY_LOG_BACKUP_COUNT", "30"))
COMMON_SCANNER_TARGET_RE = re.compile(
    r"(?:"
    r"(?:^|/)(?:\.env|\.git|\.svn|wp-admin|wp-login(?:\.php)?|wordpress|"
    r"phpmyadmin|cgi-bin|server-status|actuator|vendor/phpunit|boaform|"
    r"HNAP1|\.aws)(?:/|$)|"
    r"/etc/passwd|proc/self/environ|\.\./|%2e%2e|%00|"
    r"(?:^|/)(?:config|backup|database)\.(?:php|sql|ya?ml|json)(?:$|[/?])"
    r")",
    re.IGNORECASE,
)
DIRECTORY_TRAVERSAL_VALUE_RE = re.compile(
    r"(?:\.\.[/\\]|%(?:25)?2e%(?:25)?2e(?:%(?:25)?2f|%(?:25)?5c|[/\\])|"
    r"/etc/passwd|proc/self/(?:environ|cmdline)|[A-Za-z]:\\(?:windows|users)\\)",
    re.IGNORECASE,
)
# Every quantifier here is bounded, and callers truncate to
# MAX_DIAGNOSTIC_SCAN_LENGTH first, so match cost stays linear in the scanned
# length.  An unbounded ".*" between literals backtracks quadratically and is a
# denial-of-service vector on a path that runs for every request.
#
# Both subprocess call sites build argv lists (never shell=True) and
# print_stream emits "--" before user-supplied ids, so shell metacharacters are
# indicators for the log rather than live injection vectors.  Bare ";" is left
# out: it is ordinary punctuation in a taxon name or note and produced more
# noise than signal.  Flag injection is handled by
# ARGUMENT_INJECTION_VALUE_RE below.
COMMAND_EXECUTION_VALUE_RE = re.compile(
    r"(?:\x00|\r|\n|`[^`]{0,200}`|\$\(|\|\||&&|<script\b|"
    r"\{\{[^{}]{0,200}\}\})",
    re.IGNORECASE,
)
# Option-shaped values are classified separately from the metacharacters
# above, because shape alone does not make one an attack.  The UI's
# "suppress field" control posts custom_args[]=-Habitat, which is the exact
# shape of a short option, so treating every leading dash as argument
# injection files ordinary prints in the targeted-attack log -- and, because
# log_targeted_attack sets g.security_event_logged, suppresses their
# http_error_response records too.  A value the label endpoints would accept
# as a field name is not injection into anything, so CUSTOM_FIELD_RE -- the
# rule those endpoints validate with -- decides.  It rejects the "--flag"
# form outright, so real argument injection stays flagged.  The match is
# anchored to the start of the value: that is the only position where it
# could become an argv option.
ARGUMENT_INJECTION_VALUE_RE = re.compile(r"^\s{0,8}--?[A-Za-z][A-Za-z0-9_-]{0,64}")
LABELMAKER_ATTACK_SURFACE_ENDPOINTS = frozenset(
    {
        "lookup_batch",
        "submit",
        "print_start",
        "print_stream",
        "find_observations",
        "observation_fields",
    }
)

# How many dropped observations the CSV export names in its response header.
# The rest are counted only, so one bad paste cannot push a huge header at the
# browser.
MAX_SKIPPED_IDS_REPORTED = 20

# Sanitizing limits for To-Do suggestions.
TODO_MAX_NAME_LENGTH = 60
TODO_MAX_SUGGESTION_LENGTH = 500
TODO_MAX_FILE_BYTES = int(os.environ.get("TODO_MAX_FILE_BYTES", str(256 * 1024)))

# Job output retention.  Directories are pruned on a timer inside the reaper
# thread; their daily counts are archived first so the usage graph keeps its
# history.  The first sweep is delayed so importing this module (tests, shell
# sessions) never deletes anything unexpectedly.
JOB_PRUNE_INTERVAL_SECONDS = int(
    os.environ.get("JOB_PRUNE_INTERVAL_SECONDS", str(6 * 3600))
)
JOB_PRUNE_STARTUP_DELAY_SECONDS = int(
    os.environ.get("JOB_PRUNE_STARTUP_DELAY_SECONDS", "120")
)

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

# Count of SSE log streams currently held open (one worker thread each).
_open_streams = [0]
_stream_count_lock = threading.Lock()


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
    """A rate-limited GET request helper for the iNaturalist API.

    The outbound pace stays at one request per second across all threads, but
    only the scheduling is serialized: the lock is released before sleeping and
    before the request itself.  Holding it across the network call made one slow
    upstream response block every other request in the process.
    """
    global next_api_call_time
    with api_lock:
        scheduled_at = max(time.time(), next_api_call_time)
        next_api_call_time = scheduled_at + INAT_MIN_REQUEST_INTERVAL

    delay = scheduled_at - time.time()
    if delay > 0:
        time.sleep(delay)

    try:
        kwargs.setdefault("headers", INAT_HEADERS)
        kwargs.setdefault("timeout", 20)
        response = requests.get(url, **kwargs)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException:
        app.logger.exception("Error during iNaturalist API request.")
        raise


app = Flask(__name__, static_url_path="/labels/static")

cmd_logger = logging.getLogger("cmd_logger")
api_error_logger = logging.getLogger("api_error_logger")
user_problem_logger = logging.getLogger("user_problem_logger")
internet_scanner_logger = logging.getLogger("internet_scanner_logger")
targeted_attack_logger = logging.getLogger("targeted_attack_logger")


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
    user_problem_logger.setLevel(logging.WARNING)
    internet_scanner_logger.setLevel(logging.WARNING)
    targeted_attack_logger.setLevel(logging.WARNING)

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
        (
            flask_app.logger,
            "app_error_log",
            "error.log",
            10,
            warning_formatter,
            logging.WARNING,
        ),
        (cmd_logger, "cmd_log", "app.log", 5, command_formatter, logging.INFO),
        (
            api_error_logger,
            "api_error_log",
            "api_error.log",
            5,
            warning_formatter,
            logging.WARNING,
        ),
        (
            user_problem_logger,
            "user_problem_log",
            "user_problems.log",
            10,
            warning_formatter,
            logging.WARNING,
        ),
        # Deeper retention than the other logs.  These two are the ones an
        # attacker has an interest in flushing, and bounded record sizes alone
        # only slow that down; history has to outlive a sustained flood.
        (
            internet_scanner_logger,
            "internet_scanner_log",
            "internet_scanners.log",
            SECURITY_LOG_BACKUP_COUNT,
            warning_formatter,
            logging.WARNING,
        ),
        (
            targeted_attack_logger,
            "targeted_attack_log",
            "targeted_attacks.log",
            SECURITY_LOG_BACKUP_COUNT,
            warning_formatter,
            logging.WARNING,
        ),
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

# Reject oversized bodies before Werkzeug parses them.  nginx allows 200M on
# this vhost, and every byte of a form POST is parsed by the single worker.
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

_rate_limiter = RateLimiter()
# Loopback only: Gunicorn binds to 127.0.0.1, so a request that did not come
# from the local nginx has no business claiming a forwarded client address.
_TRUSTED_PROXY_ADDRESSES = {"127.0.0.1", "::1"}


def _request_limits_disabled():
    """Rate limiting and origin checks are off under test and by opt-out."""
    return app.config.get("TESTING") or _env_flag_enabled("LABELS_DISABLE_REQUEST_LIMITS")


def client_ip():
    """Best-effort client address, trusting proxy headers only from loopback."""
    remote_addr = request.remote_addr or "unknown"
    if remote_addr not in _TRUSTED_PROXY_ADDRESSES:
        return remote_addr

    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip

    forwarded_for = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded_for:
        # nginx appends the peer address, so the last hop is the trustworthy one.
        return forwarded_for.split(",")[-1].strip() or remote_addr

    return remote_addr


def _bounded_diagnostic_value(value, depth=0):
    """Return a log-safe, bounded representation without redacting content.

    Depth is capped as well as width: a client event body is attacker-supplied
    JSON, and json.loads accepts nesting deeper than this walker could recurse
    through, so an unbounded walk turns a few kilobytes into a 500.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, dict)) and depth >= MAX_DIAGNOSTIC_DEPTH:
        return f"[nested {type(value).__name__} truncated at depth {depth}]"
    if isinstance(value, (list, tuple)):
        return [_bounded_diagnostic_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_diagnostic_value(item, depth + 1)
            for key, item in list(value.items())[:100]
        }

    text = str(value)
    if len(text) <= MAX_DIAGNOSTIC_VALUE_LENGTH:
        return text
    omitted = len(text) - MAX_DIAGNOSTIC_VALUE_LENGTH
    return f"{text[:MAX_DIAGNOSTIC_VALUE_LENGTH]}...[{omitted} chars omitted]"


def _request_diagnostic_fields():
    if not has_request_context():
        return {}
    return {
        "request_id": getattr(g, "request_id", None),
        "client_ip": client_ip(),
        "method": request.method,
        "path": request.path,
        "endpoint": request.endpoint,
        "user_agent": request.headers.get("User-Agent", ""),
    }


def _write_event(logger, event, **fields):
    payload = {"event": event, **_request_diagnostic_fields(), **fields}
    payload = {
        key: _bounded_diagnostic_value(value)
        for key, value in payload.items()
        if value is not None
    }
    # JSON escaping keeps every event on one physical line even when a payload
    # contains attacker-controlled newlines or terminal control characters.
    line = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    if len(line) > MAX_DIAGNOSTIC_RECORD_LENGTH:
        line = json.dumps(
            _truncated_diagnostic_record(payload, len(line)),
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
    logger.warning(line)


def _truncated_diagnostic_record(payload, record_length):
    """Reduce an oversized record to its identifying fields plus an excerpt.

    Keeping the record valid, bounded, and on one line matters more than
    keeping all of it: the reader still learns which request produced it and
    how much was dropped, and no single request can rotate the log away.
    """
    record = {
        key: value
        for key, value in payload.items()
        if key in DIAGNOSTIC_RECORD_IDENTITY_FIELDS
    }
    body = {
        key: value
        for key, value in payload.items()
        if key not in DIAGNOSTIC_RECORD_IDENTITY_FIELDS
    }
    record["record_truncated"] = True
    record["record_length"] = record_length
    record["record_excerpt"] = json.dumps(
        body, ensure_ascii=True, sort_keys=True, default=str
    )[:MAX_DIAGNOSTIC_VALUE_LENGTH]
    return record


def log_user_problem(event, **fields):
    if has_request_context():
        g.user_problem_event_logged = True
    _write_event(user_problem_logger, event, **fields)


def log_internet_scanner(event, **fields):
    if has_request_context():
        g.security_event_logged = True
    _write_event(internet_scanner_logger, event, **fields)


def log_targeted_attack(event, **fields):
    if has_request_context():
        g.security_event_logged = True
    _write_event(targeted_attack_logger, event, **fields)


def _request_target():
    """Return the original request target when the server exposes it."""
    return request.environ.get("RAW_URI") or request.environ.get(
        "REQUEST_URI"
    ) or request.full_path


def _looks_like_injection(value):
    return bool(_exploit_techniques(value))


def _looks_like_argument_injection(text):
    """True for option-shaped values the label endpoints would reject."""
    if not ARGUMENT_INJECTION_VALUE_RE.match(text):
        return False
    # Endpoints strip() before validating, so compare on the same footing.
    return not CUSTOM_FIELD_RE.match(text.strip())


def _exploit_techniques(value):
    # Truncate before matching, not just before logging.  The regex engine must
    # never walk an attacker-chosen number of bytes on a per-request path.
    text = str(value or "")[:MAX_DIAGNOSTIC_SCAN_LENGTH]
    techniques = []
    if DIRECTORY_TRAVERSAL_VALUE_RE.search(text):
        techniques.append("directory_traversal")
    if COMMAND_EXECUTION_VALUE_RE.search(text) or _looks_like_argument_injection(
        text
    ):
        techniques.append("command_execution")
    return techniques


def _request_exploit_indicators():
    """Find exploit syntax in fields consumed by real labelmaker endpoints."""
    indicators = []
    for source, values in (("query", request.args), ("form", request.form)):
        for field, submitted_values in values.lists():
            for value in submitted_values:
                techniques = _exploit_techniques(value)
                if techniques:
                    indicators.append(
                        {
                            "source": source,
                            "field": field,
                            "value": str(value)[:MAX_DIAGNOSTIC_INDICATOR_LENGTH],
                            "techniques": techniques,
                        }
                    )
                    if len(indicators) >= MAX_DIAGNOSTIC_INDICATORS:
                        return indicators
    return indicators


def _origin_host(header_value):
    """Return the host[:port] of an Origin/Referer header value."""
    if not header_value:
        return None
    value = header_value.strip()
    if value == "null":
        return "null"
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]+)", value)
    return match.group(1).lower() if match else None


def _allowed_request_origins():
    hosts = {(request.host or "").lower()}
    if ALLOWED_ORIGINS:
        for origin in ALLOWED_ORIGINS.split(","):
            host = _origin_host(origin.strip())
            if host:
                hosts.add(host)
    hosts.discard("")
    return hosts


def _is_cross_site_write():
    """True when a state-changing request declares a foreign origin.

    This is the CSRF control: it costs nothing, needs no token or cookie, and
    does not interfere with embedding the app in a third-party iframe, because a
    framed page still posts with this app's own origin.  Requests with no Origin
    or Referer (curl, scripts) are allowed through -- browsers always send one
    for cross-site form posts and fetches, which is the case being defended
    against.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False

    declared = _origin_host(request.headers.get("Origin")) or _origin_host(
        request.headers.get("Referer")
    )
    if not declared:
        return False
    return declared not in _allowed_request_origins()


def _rate_limit_buckets():
    endpoint = request.endpoint
    if endpoint in RATE_LIMIT_WRITE_ONLY_ENDPOINTS and request.method in (
        "GET",
        "HEAD",
        "OPTIONS",
    ):
        return ("default",)
    return RATE_LIMIT_ENDPOINTS.get(endpoint, ()) + ("default",)


@app.before_request
def _start_request_diagnostics():
    """Per-request state only.

    Deliberately cheap.  Classification reads request.form and runs regexes over
    attacker-controlled bytes, so it must not happen before the rate limiter has
    had its say; it lives in _scan_request_for_attacks, registered below.
    """
    g.request_id = str(uuid4()).replace("-", "")[:12]
    g.request_started_at = time.monotonic()
    g.security_event_logged = False
    g.user_problem_event_logged = False
    return None


@app.before_request
def _enforce_request_limits():
    if request.endpoint == "static" or _request_limits_disabled():
        return None

    if _is_cross_site_write():
        log_targeted_attack(
            "cross_site_write_blocked",
            origin=request.headers.get("Origin") or request.headers.get("Referer"),
        )
        return (
            jsonify(
                {
                    "error": "This request was blocked because it came from another website."
                }
            ),
            403,
        )

    ip = client_ip()
    for bucket in _rate_limit_buckets():
        limit, window = RATE_LIMIT_RULES[bucket]
        allowed, retry_after = _rate_limiter.check((bucket, ip), limit, window)
        if not allowed:
            # A user clicking too fast trips this far more often than an
            # attacker does, so it belongs in the user-problem log.  Real floods
            # are visible in nginx's access log.
            log_user_problem(
                "endpoint_rate_limit_exceeded",
                bucket=bucket,
                limit=limit,
                window_seconds=window,
                retry_after_seconds=retry_after,
            )
            window_text = "day" if window >= 86400 else (
                "minute" if window == 60 else f"{window} seconds"
            )
            message = (
                f"Rate limit reached: at most {limit} of these requests per "
                f"{window_text}. Try again in {retry_after} seconds."
            )
            if request.endpoint == "todo":
                # Reached by a browser form, so answer with the page itself
                # rather than a JSON body the user would have to read raw.
                return (
                    render_template(
                        "todo.html",
                        todos=_read_todos(_todo_file_path()),
                        notice=message,
                    ),
                    429,
                    {"Retry-After": str(retry_after)},
                )
            response = jsonify({"error": message, "retry_after": retry_after})
            return response, 429, {"Retry-After": str(retry_after)}

    return None


@app.before_request
def _scan_request_for_attacks():
    """Classify the request payload, after the rate limiter has admitted it.

    Registered after _enforce_request_limits on purpose: this reads request.form
    and runs regexes over it, so running it first let one unauthenticated POST
    burn unbounded CPU that the rate limiter never got to refuse.  A request
    that is rejected as cross-site or rate-limited is logged by that check
    instead and is not classified here.
    """
    target = _request_target()
    if request.endpoint in LABELMAKER_ATTACK_SURFACE_ENDPOINTS:
        indicators = _request_exploit_indicators()
        if indicators:
            techniques = sorted(
                {
                    technique
                    for indicator in indicators
                    for technique in indicator["techniques"]
                }
            )
            log_targeted_attack(
                "labelmaker_exploit_payload",
                attack_techniques=techniques,
                indicators=indicators,
            )
    if getattr(g, "security_event_logged", False):
        return None

    target_techniques = _exploit_techniques(target)
    if COMMON_SCANNER_TARGET_RE.search(target) or target_techniques:
        # Exploit syntax on an unknown or non-input route is commodity scanner
        # noise.  The same syntax in a consumed labelmaker field is classified
        # above as a targeted attack.
        log_internet_scanner(
            "generic_exploit_probe",
            request_target=target,
            attack_techniques=target_techniques,
        )
    elif request.endpoint is None and not request.path.startswith("/labels"):
        log_internet_scanner("non_application_route_probe", request_target=target)

    return None


@app.after_request
def _add_security_headers(response):
    response.headers.setdefault("X-Request-ID", getattr(g, "request_id", ""))
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Framing is deliberately left open so the app can be embedded in other
    # sites; no X-Frame-Options or frame-ancestors restriction is set.
    if (
        response.status_code >= 400
        and not getattr(g, "security_event_logged", False)
        and not getattr(g, "user_problem_event_logged", False)
    ):
        started_at = getattr(g, "request_started_at", None)
        duration_ms = (
            round((time.monotonic() - started_at) * 1000, 1)
            if started_at is not None
            else None
        )
        log_user_problem(
            "http_error_response",
            status=response.status_code,
            duration_ms=duration_ms,
            content_length=request.content_length,
        )
    return response


@app.errorhandler(413)
def _request_entity_too_large(_error):
    limit_mb = MAX_REQUEST_BYTES / (1024 * 1024)
    # Pasting too many observations at once is the common cause here, not an
    # attack; the size cap itself is what stops an abusive body.
    log_user_problem(
        "request_body_too_large",
        content_length=request.content_length,
        limit_bytes=MAX_REQUEST_BYTES,
    )
    return (
        jsonify(
            {
                "error": (
                    f"Request body too large (limit {limit_mb:.1f} MB). Split the "
                    "observations into smaller batches."
                )
            }
        ),
        413,
    )


def error_reference(log_context, exc=None, logger=None):
    """Log the full failure detail and return a short reference for the client.

    Exception text can carry upstream URLs, query strings, usernames and
    filesystem paths, so it belongs in the log rather than in a response.  The
    reference ties a user's report back to the log line without exposing any of
    it.
    """
    reference = uuid4().hex[:8]
    target_logger = logger or app.logger
    detail = f"[ref {reference}] {log_context}"
    if exc is not None:
        target_logger.warning("%s: %r", detail, exc, exc_info=True)
    else:
        target_logger.warning(detail)
    return reference


def client_error(message, status=400, exc=None, logger=None, log_context=None):
    """Return a specific client-facing error without leaking internals."""
    reference = error_reference(log_context or message, exc=exc, logger=logger)
    return (
        jsonify({"error": f"{message} (reference {reference})", "reference": reference}),
        status,
    )


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


class ConversionBudgetExceeded(ValueError):
    """Raised when one request asks for more MO -> iNat conversions than allowed."""


# Function to convert MO number to iNaturalist ID or return iNaturalist ID
def get_inat_id(obs_input, budget=None):
    """Resolve one observation input to a generator-ready ID.

    ``budget`` is an optional single-element list used as a per-request counter
    for MO -> iNat conversions; each conversion spawns a subprocess and makes an
    upstream request, so callers handling batches pass one in to bound the work.
    """
    obs_type, obs_id = extract_obs_id(obs_input)

    # Direct MO observation - return with MO prefix (uppercase) for generator compatibility
    if obs_type == "mo_direct":
        return f"MO{obs_id}"

    if obs_type == "bg":
        return f"BG{obs_id}"

    # Convert MO to iNat (motoinat)
    if obs_type == "mo":
        if budget is not None:
            if budget[0] <= 0:
                raise ConversionBudgetExceeded(
                    f"Too many Mushroom Observer conversions in one request "
                    f"(max {MAX_MO_CONVERSIONS_PER_REQUEST}). Submit MO #{obs_id} in a "
                    f"smaller batch, or enter it as MO{obs_id} to skip the conversion."
                )
            budget[0] -= 1
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(app.root_path, "motoinat.py"),
                    "-q",
                    obs_id,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=MOTOINAT_TIMEOUT_SECONDS,
            )
            inat_id = result.stdout.strip()
            if inat_id.isdigit():
                return inat_id
            else:
                raise ValueError(f"No iNaturalist observation found for MO #{obs_id}")
        except subprocess.TimeoutExpired:
            app.logger.warning(
                "motoinat timed out after %ss for MO #%s",
                MOTOINAT_TIMEOUT_SECONDS,
                obs_id,
            )
            raise ValueError(
                f"Timed out converting MO #{obs_id} to iNaturalist "
                f"(after {MOTOINAT_TIMEOUT_SECONDS}s). Mushroom Observer may be slow "
                f"right now."
            )
        except subprocess.CalledProcessError as e:
            # The subprocess stderr can carry upstream URLs and query strings;
            # keep it in the log rather than in the response.
            app.logger.warning(
                "motoinat failed for MO #%s (exit %s): %s",
                obs_id,
                e.returncode,
                (e.stderr or "").strip(),
            )
            raise ValueError(
                f"Error converting MO #{obs_id} to iNaturalist. "
                f"The observation may not be linked to an iNaturalist record."
            )

    # iNat ID
    return obs_id


@app.template_global()
def static_asset_url(filename):
    """Version a static URL by mtime.

    nginx serves /labels/static/ without an expires header, so a browser may
    heuristically cache it. index.html calls into addobs_helpers.js, and the two
    have to stay in lockstep - a stale helper file would break the page.
    """
    try:
        stamp = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=stamp)


@app.route("/")
@app.route("/labels")
@app.route("/labels/")
def labels():
    return render_template("index.html", default_label_fields=DEFAULT_LABEL_FIELDS)


def _client_event_text(value):
    """Coerce a browser-supplied field to a bounded string.

    window.onerror hands the browser script strings here, but the request
    body is attacker-controlled: a container would be walked by
    _bounded_diagnostic_value rather than truncated, turning one request into
    a log record far larger than any single value bound allows.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return f"[{type(value).__name__} omitted]"
    return str(value)[:MAX_DIAGNOSTIC_VALUE_LENGTH]


def _client_event_number(value):
    """Line and column numbers are only meaningful as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


@app.post("/labels/client_event")
def client_event():
    """Receive bounded diagnostics for failures that happen only in a browser."""
    try:
        payload = request.get_json(silent=True)
    except RecursionError:
        # get_json(silent=True) swallows ValueError, but json.loads raises
        # RecursionError on deeply nested input, which would surface as a 500.
        payload = None
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON event"}), 400

    event_type = str(payload.get("type") or "").strip()
    allowed_types = {
        "javascript_error",
        "unhandled_rejection",
        "resource_load_error",
    }
    if event_type not in allowed_types:
        return jsonify({"error": "Unsupported client event type"}), 400

    log_user_problem(
        "browser_error",
        browser_event_type=event_type,
        message=_client_event_text(payload.get("message")),
        source=_client_event_text(payload.get("source")),
        line=_client_event_number(payload.get("line")),
        column=_client_event_number(payload.get("column")),
        stack=_client_event_text(payload.get("stack")),
        page_url=_client_event_text(payload.get("page_url")),
    )
    return "", 204


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
    conversion_budget = [MAX_MO_CONVERSIONS_PER_REQUEST]

    # Resolve inputs to either iNat IDs or MO IDs
    for idx, obs_input in enumerate(obs_inputs):
        try:
            resolved = get_inat_id(obs_input, budget=conversion_budget)
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
                    "ofvs": [
                        {
                            "name": "BugGuide URL",
                            "value": f"https://bugguide.net/node/view/{bg_num}",
                        }
                    ],
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
                reference = error_reference(
                    f"iNaturalist batch fetch failed for IDs {chunk}",
                    exc=e,
                    logger=api_error_logger,
                )
                msg = (
                    "Could not fetch this observation from iNaturalist "
                    f"(reference {reference})"
                )
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
                    ] = f"Mushroom Observer observation #{mo_num} does not exist"
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
            reference = error_reference(
                f"Mushroom Observer lookup failed for MO #{mo_num}",
                exc=e,
                logger=api_error_logger,
            )
            for idx in mo_map_indices.get(mo_num, []):
                results[idx]["error"] = (
                    f"Error processing Mushroom Observer #{mo_num} "
                    f"(reference {reference})"
                )
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


_inat_label_module = None
_inat_label_module_lock = threading.Lock()
_inat_label_module_failed = False


def inat_label_script_path():
    """Return the generator path in either supported repository layout."""
    local_path = os.path.join(app.root_path, "inat.label.py")
    parent_path = os.path.join(os.path.dirname(app.root_path), "inat.label.py")
    if os.path.isfile(local_path):
        return local_path
    if os.path.isfile(parent_path):
        return parent_path
    # Keep the usual deployment path in any eventual error message.
    return local_path


def inat_label_module():
    """Load ``inat.label.py`` as a module so other code can reuse its helpers.

    The generator is normally run as a subprocess, but the CSV export needs the
    same date parsing and sort comparisons the labels use, and reimplementing
    them here would let the two drift apart.  The filename is not importable, so
    it is loaded by path, once per process.  A load failure is remembered and
    returns ``None`` rather than raising, so a broken import degrades the CSV to
    unsorted output instead of failing the download.
    """
    global _inat_label_module, _inat_label_module_failed
    if _inat_label_module is not None or _inat_label_module_failed:
        return _inat_label_module
    with _inat_label_module_lock:
        if _inat_label_module is not None or _inat_label_module_failed:
            return _inat_label_module
        try:
            script_path = inat_label_script_path()
            spec = importlib.util.spec_from_file_location(
                "inat_label_helpers", script_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(
                    f"Could not load module specification from {script_path}"
                )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _inat_label_module = module
        except Exception as e:
            _inat_label_module_failed = True
            app.logger.warning(f"Could not load inat.label.py helpers: {e!s}")
        return _inat_label_module


def read_sort_request(form):
    """Validate the shared Sort controls from a submitted form.

    Returns ``(sort_mode, sort_field, error)``.  *error* is a message string when
    the request should be rejected; both callers reject on the same rules so the
    CSV and the labels always agree about what a given Sort selection means.
    """
    sort_mode = (form.get("sort") or "").strip().lower()
    sort_field = (form.get("sort_field") or "").strip()
    if sort_mode and sort_mode not in SORT_MODES:
        if not _looks_like_injection(sort_mode):
            # A crafted value is already recorded by _scan_request_for_attacks,
            # which classifies every field on this endpoint before the view
            # runs.
            log_user_problem("invalid_sort_mode", submitted_value=sort_mode)
        return "", "", "Invalid sort order"
    if sort_mode == "custom" and not sort_field:
        app.logger.warning("Custom sort requested without a field name")
        return "", "", "Sorting by field requires a field name"
    if sort_field and not SORT_FIELD_RE.match(sort_field):
        if not _looks_like_injection(sort_field):
            # A crafted value is already recorded by _scan_request_for_attacks,
            # which classifies every field on this endpoint before the view
            # runs.
            log_user_problem("invalid_sort_field", submitted_value=sort_field)
        return "", "", "Invalid sort field"
    if sort_mode != "custom":
        sort_field = ""
    return sort_mode, sort_field, None


def read_custom_fields_request(form):
    """Validate and return the label field additions/removals in *form*."""
    if not form.get("use_custom"):
        return [], None

    custom_fields = [
        field.strip() for field in form.getlist("custom_args[]") if field.strip()
    ]
    if len(custom_fields) > MAX_CUSTOM_FIELDS:
        log_user_problem(
            "custom_field_limit_exceeded",
            submitted_count=len(custom_fields),
            limit=MAX_CUSTOM_FIELDS,
        )
        return [], f"Too many custom label fields (max {MAX_CUSTOM_FIELDS})"

    invalid_fields = [f for f in custom_fields if not CUSTOM_FIELD_RE.match(f)]
    if invalid_fields:
        if not any(_looks_like_injection(field) for field in invalid_fields):
            # A crafted value is already recorded by _scan_request_for_attacks,
            # which classifies every field on this endpoint before the view
            # runs.
            log_user_problem(
                "invalid_custom_fields", submitted_values=invalid_fields
            )
        return [], f"Invalid custom label field name: {invalid_fields[0][:64]!r}"

    return custom_fields, None


def _split_custom_fields(custom_fields):
    """Return generator-style ``(additions, removals)`` field-name lists."""
    additions = []
    removals = []
    for field in custom_fields or []:
        if field.startswith("+"):
            additions.append(field[1:].strip())
        elif field.startswith("-"):
            removals.append(field[1:].strip())
    return additions, removals


def _ofv_value(ofvs, *field_names):
    """Return the first nonblank observation-field value matching *field_names*."""
    if not isinstance(ofvs, list):
        return ""
    wanted = [name.lower() for name in field_names]
    for name in wanted:
        for field in ofvs:
            if not isinstance(field, dict):
                continue
            if str(field.get("name") or "").strip().lower() != name:
                continue
            value = field.get("value")
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _voucher_value(ofvs):
    """Return the voucher number under either of the field names in use."""
    return _ofv_value(ofvs, "Voucher Number", "Voucher Number(s)")


def sort_csv_rows(rows, sort_mode, sort_field):
    """Order CSV rows the same way ``inat.label.py`` orders the printed labels.

    Each row carries the metadata the comparisons need, including the rendered
    label fields after custom additions/removals.  That prevents raw API fields
    hidden from the labels from changing only the spreadsheet order.  Rows are
    returned in the new order; the caller renumbers the ID column afterwards.
    """
    if sort_mode == "none":
        return sorted(rows, key=lambda row: row["index"])

    module = inat_label_module()

    if sort_mode in ("voucher", "custom"):
        if module is None:
            return list(rows)

        def raw_value(row):
            label_fields = row.get("label_fields") or []
            if sort_mode == "voucher":
                return module.get_voucher_value(label_fields)
            return module.label_get(label_fields, sort_field)

        def compare(row_a, row_b):
            result = module.cmp_alpha_then_trailing_num(
                raw_value(row_a) or None, raw_value(row_b) or None
            )
            if result != 0:
                return result
            return row_a["index"] - row_b["index"]

        return sorted(rows, key=cmp_to_key(compare))

    if sort_mode in ("date", "date-desc"):

        def date_key(row):
            observed = row["observed"]
            if observed is None:
                # Undated rows sort last in both directions, so the leading flag
                # stays ascending and only the index orders them.
                return (1, 0.0, 0.0, row["index"])
            # The local calendar date leads so rows stay in the order their
            # printed dates suggest even when time zones differ; the instant only
            # breaks ties within one displayed date.
            ordinal = float(observed.date().toordinal())
            instant = observed.timestamp()
            if sort_mode == "date-desc":
                return (0, -ordinal, -instant, row["index"])
            return (0, ordinal, instant, row["index"])

        return sorted(rows, key=date_key)

    # Default: observation number, matching the generator's numeric sort.
    def number_key(row):
        if module is not None:
            numeric = module.parse_key_default(row["obs_number"])
        else:
            match = re.search(r"(\d+)\s*$", str(row["obs_number"]).strip())
            numeric = int(match.group(1)) if match else 0
        return (numeric, row["index"])

    return sorted(rows, key=number_key)


# Columns of the CSV export.  "ID" is the row's position in the exported order,
# so it matches the order the labels print in for the same Sort selection.
CSV_COLUMNS = [
    "ID",
    "Observation Number",
    "Scientific Name",
    "Common Name",
    "Observer",
    "Observer Name",
    "Date Observed",
    "Time Observed",
    "Location",
    "Latitude",
    "Longitude",
    "Coordinate Accuracy",
    "Herbarium Catalog Number",
    "Voucher Number",
    "URL",
]

_CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
# Plain numbers are data, not formulas, so they are exempt from the leading
# apostrophe below; without this every negative longitude would export as text.
_PLAIN_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")


def safe_csv_field(val):
    """Return *val* as CSV text, defused if a spreadsheet would read it as a formula."""
    try:
        s = str(val)
    except Exception:
        s = ""
    if s and s[0] in ("=", "+", "-", "@") and not _PLAIN_NUMBER_RE.fullmatch(s):
        return "'" + s
    return s


def _observed_date_and_time(observation, observed):
    """Split a resolved observation datetime into date and clock-time columns.

    *observed* comes from ``observation_sort_datetime``, which falls back to
    date-only fields and returns midnight when there is no time of day.  The
    clock column is filled only when the source actually carried a time, so an
    undated-hour observation reads as blank instead of a spurious 00:00:00.
    """
    if observed is None:
        return "", ""
    date_text = observed.date().isoformat()
    if str(observation.get("time_observed_at") or "").strip():
        return date_text, observed.strftime("%H:%M:%S")
    # Records without the API timestamp only have a time if their free-text date
    # string carries one and no calendar-date field preempted it -- that is the
    # Mushroom Observer case.
    if not observation.get("observed_on") and _CLOCK_TIME_RE.search(
        str(observation.get("observed_on_string") or "")
    ):
        return date_text, observed.strftime("%H:%M:%S")
    return date_text, ""


def _coordinate_columns(observation):
    """Return ``(latitude, longitude, accuracy)`` as the labels would print them."""
    module = inat_label_module()
    if module is None:
        return "", "", ""
    try:
        coords, accuracy = module.get_coordinates(observation)
    except Exception as e:
        app.logger.warning(f"Could not read coordinates for CSV export: {e!s}")
        return "", "", ""
    # "private" and "Not available" are statuses, not coordinates.
    if not coords or "," not in coords:
        return "", "", ""
    latitude, _, longitude = coords.partition(",")
    return latitude.strip(), longitude.strip(), accuracy or ""


def _mo_observation_shape(mo_result, mo_number):
    """Shape a Mushroom Observer record like the dicts inat.label.py expects.

    Reusing that shape lets the MO rows get their date and coordinates from the
    same helpers as the iNaturalist rows, so both sort together correctly.
    """
    observation = {
        "id": f"MO{mo_number}",
        "observed_on_string": mo_result.get("date") or "",
        "description": mo_result.get("notes") or "",
        "ofvs": [
            {
                "name": "Mushroom Observer URL",
                "value": f"https://mushroomobserver.org/obs/{mo_number}",
            }
        ],
    }
    consensus = mo_result.get("consensus") or {}
    observation["taxon"] = {
        "name": consensus.get("name") or mo_result.get("name") or "Not available",
        "preferred_common_name": "",
    }
    owner = mo_result.get("owner") or {}
    observation["user"] = {
        "name": owner.get("legal_name") or "",
        "login": owner.get("login_name") or mo_result.get("login_name") or "",
    }
    if "herbarium_name" in mo_result:
        observation["ofvs"].append(
            {
                "name": "Herbarium Name",
                "value": mo_result.get("herbarium_name") or "",
            }
        )
    if "herbarium_id" in mo_result:
        observation["ofvs"].append(
            {
                "name": "Herbarium Catalog Number",
                "value": mo_result.get("herbarium_id") or "",
            }
        )
    location = mo_result.get("location")
    if isinstance(location, dict):
        observation["place_guess"] = location.get("name") or ""
        try:
            longitude = (
                float(location["longitude_east"]) + float(location["longitude_west"])
            ) / 2
            latitude = (
                float(location["latitude_north"]) + float(location["latitude_south"])
            ) / 2
            observation["geojson"] = {"coordinates": [longitude, latitude]}
        except (KeyError, TypeError, ValueError):
            pass
    return observation


def _rendered_label_fields(observation, custom_fields):
    """Build the post-filtered fields that printed-label sorting can inspect."""
    module = inat_label_module()
    if module is None or not observation:
        return []
    additions, removals = _split_custom_fields(custom_fields)
    taxon = observation.get("taxon") or {}
    iconic_taxon = taxon.get("iconic_taxon_name") or (
        "Fungi" if str(observation.get("id") or "").startswith("MO") else "Life"
    )
    try:
        rendered = module.create_inaturalist_label(
            observation,
            iconic_taxon,
            custom_add=additions,
            custom_remove=removals,
        )
    except Exception as e:
        app.logger.warning(f"Could not build label fields for CSV sorting: {e!s}")
        return []
    return rendered[0] if rendered else []


def _padded_row_values(obs_number, scientific_name="", observer="", url=""):
    """Return a row for a record with no usable detail, blank past what is known."""
    values = [""] * (len(CSV_COLUMNS) - 1)
    values[0] = obs_number
    values[1] = scientific_name
    values[3] = observer
    values[-1] = url
    return values


def _observed_datetime(observation):
    """Resolve an observation's datetime with the generator's own parser."""
    module = inat_label_module()
    if module is None or not observation:
        return None
    try:
        return module.observation_sort_datetime(observation)
    except Exception as e:
        app.logger.warning(f"Could not read observation date for CSV export: {e!s}")
        return None


def _inat_csv_row(
    index, obs_number, observation, custom_fields=None, include_sort_fields=False
):
    """Build one CSV row from an iNaturalist observation record."""
    taxon = observation.get("taxon") or {}
    user = observation.get("user") or {}
    ofvs = observation.get("ofvs") or []
    observed = _observed_datetime(observation)
    date_text, time_text = _observed_date_and_time(observation, observed)
    latitude, longitude, accuracy = _coordinate_columns(observation)
    return {
        "index": index,
        "obs_number": obs_number,
        "observed": observed,
        "ofvs": ofvs,
        "label_fields": (
            _rendered_label_fields(observation, custom_fields)
            if include_sort_fields
            else []
        ),
        "values": [
            obs_number,
            taxon.get("name") or "Unknown",
            taxon.get("preferred_common_name") or "",
            user.get("login") or "Unknown",
            user.get("name") or "",
            date_text,
            time_text,
            _concise_locality(observation) or "",
            latitude,
            longitude,
            accuracy,
            _ofv_value(ofvs, "Herbarium Catalog Number"),
            _voucher_value(ofvs),
            f"https://www.inaturalist.org/observations/{obs_number}",
        ],
    }


def _mo_csv_row(
    index, obs_number, mo_result, custom_fields=None, include_sort_fields=False
):
    """Build one CSV row from a Mushroom Observer record, or a placeholder row.

    *mo_result* is ``None`` when the lookup failed; the row still goes out so the
    observation is not silently missing from the export.
    """
    mo_number = obs_number[2:]
    url = f"https://mushroomobserver.org/obs/{mo_number}"
    if not isinstance(mo_result, dict):
        return {
            "index": index,
            "obs_number": obs_number,
            "observed": None,
            "ofvs": [],
            "label_fields": [],
            "values": _padded_row_values(
                obs_number, scientific_name="Unknown", observer="Unknown", url=url
            ),
        }

    consensus = mo_result.get("consensus") or {}
    owner = mo_result.get("owner") or {}
    observation = _mo_observation_shape(mo_result, mo_number)
    observed = _observed_datetime(observation)
    date_text, time_text = _observed_date_and_time(observation, observed)
    latitude, longitude, accuracy = _coordinate_columns(observation)
    ofvs = observation["ofvs"]
    herbarium_catalog_number = mo_result.get("herbarium_id") or ""
    return {
        "index": index,
        "obs_number": obs_number,
        "observed": observed,
        "ofvs": ofvs,
        "label_fields": (
            _rendered_label_fields(observation, custom_fields)
            if include_sort_fields
            else []
        ),
        "values": [
            obs_number,
            consensus.get("name") or mo_result.get("name") or "Unknown",
            "",
            owner.get("login_name") or mo_result.get("login_name") or "Unknown",
            owner.get("legal_name") or "",
            date_text,
            time_text,
            observation.get("place_guess") or "",
            latitude,
            longitude,
            accuracy,
            str(herbarium_catalog_number),
            "",
            url,
        ],
    }


def _bugguide_csv_row(index, obs_number):
    """Build one CSV row for a BugGuide entry, which has no API lookup here."""
    bg_number = obs_number[2:]
    return {
        "index": index,
        "obs_number": obs_number,
        "observed": None,
        "ofvs": [],
        "label_fields": [],
        "values": _padded_row_values(
            obs_number,
            scientific_name="BugGuide",
            url=f"https://bugguide.net/node/view/{bg_number}",
        ),
    }


def _skipped_ids_header(skipped):
    """Render dropped observation inputs as a single safe header value.

    The entries are raw user input, so they are reduced to an identifier-shaped
    subset before going back out in a header: no separators, no control
    characters, nothing that could split the header.
    """
    parts = []
    for value in skipped[:MAX_SKIPPED_IDS_REPORTED]:
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value))[:32]
        if cleaned:
            parts.append(cleaned)
    return ", ".join(parts)


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

        # Same Sort selection the labels use.  An empty value means the
        # generator's default, which is by observation number.
        sort_mode, sort_field, sort_error = read_sort_request(request.form)
        if sort_error:
            return sort_error, 400
        custom_fields, custom_fields_error = read_custom_fields_request(request.form)
        if custom_fields_error:
            return custom_fields_error, 400

        # Inputs that never reach the file: unparseable entries here, and iNat
        # IDs the API does not return below (deleted, private, or mistyped).  A
        # silently short CSV is indistinguishable from a broken export, so the
        # skipped entries travel back in response headers.
        skipped = []

        # Resolve inputs into either iNat IDs or MO IDs
        resolved = []
        conversion_budget = [MAX_MO_CONVERSIONS_PER_REQUEST]
        for obs in raw_observations:
            try:
                rid = get_inat_id(obs, budget=conversion_budget)
                resolved.append(rid)
            except ConversionBudgetExceeded as e:
                app.logger.warning(str(e))
                return str(e), 429
            except ValueError as e:
                app.logger.warning(str(e))
                # Skip invalid entries
                skipped.append(obs)
                continue

        # Partition into iNat and MO.  BugGuide entries are neither, and putting
        # a "BG..." value in the iNat id list would malform the whole chunk's
        # query, so they are excluded here and rendered from the input alone.
        inat_ids = [
            str(x)
            for x in resolved
            if not (isinstance(x, str) and x.upper().startswith(("MO", "BG")))
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

        rows = []
        include_sort_fields = sort_mode in ("voucher", "custom")
        for index, rid in enumerate(resolved):
            rid_str = str(rid)
            # MO observations (fetch per-ID with detail fallback)
            if rid_str.upper().startswith("MO"):
                mo_number = rid_str[2:]
                mo_result = None
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
                        mo_result = mo_data["results"][0]
                except Exception as e:
                    app.logger.warning(f"Error fetching MO data: {e!s}")
                rows.append(
                    _mo_csv_row(
                        index,
                        rid_str,
                        mo_result,
                        custom_fields,
                        include_sort_fields,
                    )
                )
                continue
            if rid_str.upper().startswith("BG"):
                rows.append(_bugguide_csv_row(index, rid_str))
                continue
            # iNaturalist observation from batch map
            r = id_to_inat.get(rid_str)
            if r:
                rows.append(
                    _inat_csv_row(
                        index,
                        rid_str,
                        r,
                        custom_fields,
                        include_sort_fields,
                    )
                )
            else:
                # No row; the caller is told how many were dropped.
                skipped.append(rid_str)

        # The Sort dropdown drives the export the same way it drives the labels,
        # so a run's CSV and its printed labels come out in the same order.
        rows = sort_csv_rows(rows, sort_mode, sort_field)

        csv_data = [list(CSV_COLUMNS)]
        for position, row in enumerate(rows, start=1):
            csv_data.append([position] + [safe_csv_field(v) for v in row["values"]])

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(csv_data)
        csv_content = output.getvalue()

        headers = {
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename=observations.csv",
        }
        if skipped:
            app.logger.warning(
                "CSV export skipped %d of %d requested observations: %s",
                len(skipped),
                len(raw_observations),
                ", ".join(str(s) for s in skipped[:MAX_SKIPPED_IDS_REPORTED]),
            )
            headers["X-Skipped-Observations"] = str(len(skipped))
            headers["X-Skipped-Ids"] = _skipped_ids_header(skipped)

        return csv_content, 200, headers
    except Exception as e:
        app.logger.exception(e)
        return "An internal error occurred while generating the CSV file.", 500


# Streaming printing support
@app.route("/labels/print_start", methods=["POST"])
def print_start():
    fmt = (request.form.get("format") or "rtf").lower()
    if fmt not in ("rtf", "pdf"):
        if not _looks_like_injection(fmt):
            # A crafted value is already recorded by _scan_request_for_attacks,
            # which classifies every field on this endpoint before the view
            # runs.
            log_user_problem("invalid_output_format", submitted_value=fmt)
        return jsonify({"error": "Invalid format"}), 400

    omit_qr_codes = request.form.get("omit_qr_codes")
    print_duplicate_labels = bool(request.form.get("print_duplicate_labels"))

    # Label sort order.  An empty value keeps inat.label.py's default
    # observation-number sort, so no --sort flag is passed in that case.
    sort_mode, sort_field, sort_error = read_sort_request(request.form)
    if sort_error:
        return jsonify({"error": sort_error}), 400

    # Custom label fields are forwarded to inat.label.py as a command argument,
    # so validate them here rather than relying on how argparse happens to treat
    # option-shaped values.
    custom_fields, custom_fields_error = read_custom_fields_request(request.form)
    if custom_fields_error:
        return jsonify({"error": custom_fields_error}), 400

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
    conversion_budget = [MAX_MO_CONVERSIONS_PER_REQUEST]

    for obs in raw_observations:
        try:
            inat_id = str(get_inat_id(obs, budget=conversion_budget))
            if inat_id.upper().startswith("BG"):
                if not is_minilabel:
                    bg_omitted = True
                    continue
            inat_ids.append(inat_id)
            if print_duplicate_labels:
                inat_ids.append(inat_id)
        except ConversionBudgetExceeded as e:
            app.logger.warning(str(e))
            return jsonify({"error": str(e)}), 429
        except ValueError as e:
            app.logger.warning(str(e))
            continue

    if not inat_ids:
        app.logger.warning(
            "print_start: No valid observations found after processing raw input."
        )
        if bg_omitted:
            return (
                jsonify(
                    {
                        "error": "No labels were generated because all provided observations were BugGuide entries, which are only supported when minilabels are enabled."
                    }
                ),
                400,
            )
        return jsonify({"error": "No valid observations provided"}), 400

    script_path = inat_label_script_path()
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
    if request.form.get("number_labels"):
        command.append("--number-labels")
    if sort_mode:
        command.extend(["--sort", sort_mode])
        if sort_mode == "custom":
            command.extend(["--sort-field", sort_field])
    if custom_fields:
        command.append("--custom")
        # Join all custom fields with commas as inat.label.py expects a comma-separated list
        command.append(", ".join(custom_fields))
    # Everything after "--" is a positional observation ID, so no validated
    # value can be re-read as an option by the generator's argument parser.
    command.append("--")
    command.extend(inat_ids)
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

    return jsonify(
        {
            "job_id": job_id,
            "warning": (
                "BugGuide observations were omitted because minilabels are not enabled."
                if bg_omitted
                else None
            ),
        }
    )


@app.route("/labels/print_stream")
def print_stream():
    job_id = request.args.get("job_id")

    if job_id and not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        job_id,
        re.IGNORECASE,
    ):
        if not getattr(g, "security_event_logged", False):
            log_user_problem("malformed_print_job_id", submitted_value=job_id)

    with _jobs_lock:
        if not job_id or job_id not in _jobs:
            if (
                not getattr(g, "security_event_logged", False)
                and not getattr(g, "user_problem_event_logged", False)
            ):
                log_user_problem("print_job_not_found", submitted_value=job_id)
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

    # Each open stream pins a worker thread until its job ends, so refuse new
    # ones past the cap instead of letting them exhaust the thread pool.  The
    # client falls back to polling for the finished file.
    with _stream_count_lock:
        if _open_streams[0] >= MAX_CONCURRENT_STREAMS:
            app.logger.warning(
                "print_stream: refusing stream, %s already open (max %s)",
                _open_streams[0],
                MAX_CONCURRENT_STREAMS,
            )
            return (
                jsonify(
                    {
                        "error": (
                            f"Too many log streams open ({_open_streams[0]}, max "
                            f"{MAX_CONCURRENT_STREAMS}). The label job is still running; "
                            f"the download will appear when it finishes."
                        )
                    }
                ),
                429,
            )
        _open_streams[0] += 1

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
                if exit_code != 0:
                    log_user_problem(
                        "label_generator_nonzero_exit",
                        job_id=job_id,
                        exit_code=exit_code,
                        output_path=output_path,
                    )
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
                log_user_problem(
                    "label_generator_output_missing",
                    job_id=job_id,
                    exit_code=exit_code,
                    output_path=output_path,
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

            with _stream_count_lock:
                _open_streams[0] = max(0, _open_streams[0] - 1)

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

    mappings = (
        [results]
        if any(isinstance(value, int) for value in results.values())
        else [value for value in results.values() if isinstance(value, dict)]
    )
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

_observation_field_cache = OrderedDict()
_observation_field_cache_lock = threading.Lock()


def _normalize_observation_field_result(field):
    """Return only the autocomplete metadata used by the browser."""
    if not isinstance(field, dict):
        return None
    field_id = field.get("id")
    name = field.get("name")
    if isinstance(field_id, bool) or not isinstance(field_id, int) or field_id < 1:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    values_count = field.get("values_count", field.get("observations_count"))
    if isinstance(values_count, bool) or not isinstance(values_count, int):
        values_count = 0
    datatype = field.get("datatype")
    return {
        "id": field_id,
        "name": name.strip(),
        "datatype": datatype.strip() if isinstance(datatype, str) else "",
        "values_count": max(values_count, 0),
    }


def _cached_observation_field_search(query):
    """Fetch a small, briefly cached set of iNaturalist field suggestions."""
    cache_key = query.casefold()
    now = time.time()
    with _observation_field_cache_lock:
        entry = _observation_field_cache.get(cache_key)
        if entry is not None:
            expires_at, cached_results = entry
            if expires_at > now:
                _observation_field_cache.move_to_end(cache_key)
                return cached_results
            del _observation_field_cache[cache_key]

    response = inat_api_get(
        "https://www.inaturalist.org/observation_fields.json",
        params={"q": query},
        timeout=15,
    )
    payload = response.json()
    if isinstance(payload, list):
        fields = payload
    elif isinstance(payload, dict):
        fields = payload.get("results") or []
    else:
        fields = []
    normalized = [
        item
        for item in (_normalize_observation_field_result(field) for field in fields)
        if item is not None
    ]
    normalized.sort(
        key=lambda item: (-item["values_count"], item["name"].casefold(), item["id"])
    )
    normalized = normalized[:INAT_OBSERVATION_FIELD_RESULT_LIMIT]

    with _observation_field_cache_lock:
        _observation_field_cache[cache_key] = (
            time.time() + INAT_OBSERVATION_FIELD_CACHE_TTL,
            normalized,
        )
        _observation_field_cache.move_to_end(cache_key)
        while len(_observation_field_cache) > INAT_OBSERVATION_FIELD_CACHE_MAX_ENTRIES:
            _observation_field_cache.popitem(last=False)
    return normalized


def _inat_observation_field_filter(field_name):
    """Build iNaturalist's field-presence parameter for a validated name."""
    normalized_name = str(field_name or "").strip()
    if not normalized_name:
        return {}
    return {f"field:{normalized_name}": ""}


def _observation_has_required_field(observation, field_id=None, field_name=""):
    """Confirm that an observation has a nonblank value for the requested field."""
    normalized_name = str(field_name or "").strip().casefold()
    fields = observation.get("ofvs") if isinstance(observation, dict) else None
    if not isinstance(fields, list):
        return False

    def populated(field):
        value = field.get("value") if isinstance(field, dict) else None
        return value is not None and str(value).strip() != ""

    if field_id is not None:
        for field in fields:
            if not isinstance(field, dict):
                continue
            candidate_id = field.get("field_id")
            if candidate_id is None:
                observation_field = field.get("observation_field") or {}
                if not isinstance(observation_field, dict):
                    observation_field = {}
                candidate_id = observation_field.get("id")
            if str(candidate_id) == str(field_id) and populated(field):
                return True

    if normalized_name:
        for field in fields:
            if not isinstance(field, dict):
                continue
            observation_field = field.get("observation_field") or {}
            if not isinstance(observation_field, dict):
                observation_field = {}
            candidate_name = observation_field.get("name", field.get("name", ""))
            if str(candidate_name).strip().casefold() == normalized_name and populated(
                field
            ):
                return True
    return False


@app.get("/labels/observation_fields")
def observation_fields():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"results": []})
    if len(query) > INAT_OBSERVATION_FIELD_QUERY_MAX_LENGTH:
        return jsonify({"error": "Observation field query is too long"}), 400
    try:
        return jsonify({"results": _cached_observation_field_search(query)})
    except (requests.RequestException, ValueError) as exc:
        api_error_logger.warning(
            "Observation field autocomplete failed: %s", str(exc), exc_info=True
        )
        return jsonify({"error": "Observation field search is unavailable"}), 502


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
    obs_field_name = (request.form.get("obs_field_name") or "").strip()
    obs_field_id_raw = (request.form.get("obs_field_id") or "").strip()
    if source not in ("inat", "mo"):
        return jsonify({"error": "Unsupported source"}), 400

    if len(obs_field_name) > INAT_OBSERVATION_FIELD_QUERY_MAX_LENGTH:
        return jsonify({"error": "Observation field name is too long"}), 400
    if obs_field_id_raw and not obs_field_id_raw.isdigit():
        return jsonify({"error": "Invalid observation field ID"}), 400
    obs_field_id = int(obs_field_id_raw) if obs_field_id_raw else None
    if obs_field_id is not None and obs_field_id < 1:
        return jsonify({"error": "Invalid observation field ID"}), 400
    if source == "mo" and (obs_field_name or obs_field_id is not None):
        return (
            jsonify(
                {
                    "error": "Observation-field filtering is only supported for iNaturalist"
                }
            ),
            400,
        )
    if obs_field_id is not None and not obs_field_name:
        return jsonify({"error": "Observation field name is required"}), 400

    if date_mode not in ("observed", "created"):
        return (
            jsonify({"error": "Unsupported date_mode. Use 'observed' or 'created'."}),
            400,
        )

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
    total_in_scope = None
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
                return client_error(
                    f"Could not reach iNaturalist to look up the taxon "
                    f"{taxon_input!r}. This is usually a temporary upstream "
                    f"problem; try again in a moment.",
                    status=502,
                    exc=e,
                    logger=api_error_logger,
                    log_context=f"Taxon lookup failed for {taxon_input!r}",
                )

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

        if obs_field_name:
            try:
                scope_response = inat_api_get(
                    "https://api.inaturalist.org/v1/observations",
                    params={**inat_search_params, "per_page": 1},
                    timeout=30,
                )
                scope_total = scope_response.json().get("total_results")
                if isinstance(scope_total, int) and scope_total >= 0:
                    total_in_scope = scope_total
            except (requests.RequestException, ValueError) as e:
                api_error_logger.warning(
                    "Unfiltered iNaturalist scope count failed: %s",
                    str(e),
                    exc_info=True,
                )
            inat_search_params.update(_inat_observation_field_filter(obs_field_name))

        last_id = 0
        first_page = True
        exhausted_results = False
        fetched_pages = 0
        try:
            while (
                len(current_batch) < cap
                and fetched_pages < INAT_MAX_SEARCH_PAGES
            ):
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
                fetched_pages += 1
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
                    exhausted_results = True
                    break

                previous_last_id = last_id
                for r in results:
                    if len(current_batch) >= cap:
                        break
                    if not isinstance(r, dict):
                        continue
                    raw_oid = r.get("id")
                    if (
                        isinstance(raw_oid, bool)
                        or not str(raw_oid).isdigit()
                        or int(raw_oid) < 1
                    ):
                        continue
                    oid = int(raw_oid)
                    if oid <= last_id:
                        continue
                    last_id = oid
                    if obs_field_name and not _observation_has_required_field(
                        r, obs_field_id, obs_field_name
                    ):
                        continue
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
                            "ofvs": r.get("ofvs", []),
                            "color": color,
                        }
                    )
                if last_id <= previous_last_id:
                    api_error_logger.warning(
                        "Stopped iNaturalist paging for '%s' because page %s "
                        "had no usable observation IDs",
                        username_inat,
                        fetched_pages,
                    )
                    break
            if (
                fetched_pages >= INAT_MAX_SEARCH_PAGES
                and len(current_batch) < cap
                and not exhausted_results
            ):
                api_error_logger.info(
                    "Stopped iNaturalist paging for '%s' after %s pages "
                    "(budget INAT_MAX_SEARCH_PAGES)",
                    username_inat,
                    fetched_pages,
                )
            found = current_batch
            if obs_field_name and exhausted_results:
                total_count = len(current_batch)
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
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            detail = (
                f"iNaturalist returned HTTP {status_code}."
                if status_code
                else "iNaturalist could not be reached."
            )
            return client_error(
                f"Could not fetch observations for iNaturalist user "
                f"{username_inat!r} between {d1_str} and {d2_str}. {detail} "
                f"Check the username, or try again shortly.",
                status=502,
                exc=e,
                logger=api_error_logger,
                log_context=f"Observation fetch for user {username_inat!r} failed",
            )
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
                    if not isinstance(r, dict):
                        mo_dates_complete = False
                        continue
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
                            "ofvs": r.get("ofvs", []),
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
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            detail = (
                f"Mushroom Observer returned HTTP {status_code}."
                if status_code
                else "Mushroom Observer could not be reached."
            )
            return client_error(
                f"Could not fetch observations for Mushroom Observer user "
                f"{username_mo!r} between {d1_str} and {d2_str}. {detail} "
                f"Check the username, or try again shortly.",
                status=502,
                exc=e,
                logger=api_error_logger,
                log_context=(
                    f"Mushroom Observer fetch for user {username_mo!r} failed"
                ),
            )

    found.reverse()
    total_count = max(total_count, len(found))
    response_payload = {
        "count": len(found),
        "total_count": total_count,
        "items": found,
    }
    if obs_field_name:
        response_payload["required_observation_field"] = obs_field_name
        if total_in_scope is not None:
            response_payload["total_in_scope"] = total_in_scope
    if windows:
        response_payload["windows"] = windows
    return jsonify(response_payload), 200


@app.route("/labels/help")
def serve_help():
    return app.send_static_file("help.html")


def _todo_file_path():
    return os.path.join(app.root_path, "static", "todos.txt")


def _read_todos(todo_file):
    if not os.path.exists(todo_file):
        return []
    with open(todo_file, "r") as f:
        return [line.strip() for line in f.readlines()]


@app.route("/labels/todo", methods=["GET", "POST"])
def todo():
    """Public suggestion box.

    Submission stays open to everyone; the guards here are on volume, not
    identity: a per-IP daily cap in the request limiter, per-field length caps,
    and a ceiling on the file itself, since the whole file is read into memory
    and rendered on every page view.
    """
    todo_file = _todo_file_path()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        suggestion = request.form.get("suggestion", "").strip()

        # Sanitize input: allow only alphanumeric, spaces, and some punctuation, including Spanish characters
        name = re.sub(r"[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]", "", name)
        suggestion = re.sub(r"[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]", "", suggestion)
        name = name[:TODO_MAX_NAME_LENGTH].strip()
        suggestion = suggestion[:TODO_MAX_SUGGESTION_LENGTH].strip()

        if name and suggestion:
            try:
                current_size = os.path.getsize(todo_file)
            except OSError:
                current_size = 0

            if current_size >= TODO_MAX_FILE_BYTES:
                app.logger.warning(
                    "todo: suggestion list is full (%s bytes, max %s)",
                    current_size,
                    TODO_MAX_FILE_BYTES,
                )
                return (
                    render_template(
                        "todo.html",
                        todos=_read_todos(todo_file),
                        notice=(
                            "The suggestion list is full right now. Please try "
                            "again later."
                        ),
                    ),
                    507,
                )

            with open(todo_file, "a") as f:
                submitted_on = time.strftime("%Y-%m-%d", time.gmtime())
                f.write(f"[{submitted_on}] {name}: {suggestion}\n")
        return redirect(url_for("todo"))

    return render_template("todo.html", todos=_read_todos(todo_file))


# Start a background thread to reap finished jobs
def _prune_job_output(now=None):
    """Archive and delete job directories past the retention window.

    Counts are folded into the daily usage ledger before deletion so
    make_graph.py keeps its history after the files are gone.
    """
    try:
        result = usage_stats.prune_job_dirs(
            retention_days=usage_stats.JOB_RETENTION_DAYS,
            jobs_dir=os.path.join(app.root_path, "static", "jobs"),
            now=now,
        )
    except Exception:
        app.logger.exception("Job output prune failed.")
        return None

    if result["removed"] or result["failed"]:
        cmd_logger.info(
            "Pruned %s job directories older than %s days "
            "(%.1f MB freed, %s labels archived, %s failed)",
            result["removed"],
            usage_stats.JOB_RETENTION_DAYS,
            result["bytes_freed"] / 1_048_576,
            result["labels"],
            result["failed"],
        )
    return result


def _reaper_thread():
    # Deleting output on import would be a nasty surprise for a test run or a
    # shell session, so the first sweep waits until the process has clearly
    # settled into serving.
    next_prune = time.monotonic() + JOB_PRUNE_STARTUP_DELAY_SECONDS
    prune_enabled = not _env_flag_enabled("LABELS_DISABLE_JOB_PRUNE")
    if prune_enabled:
        # Cheap and idempotent: the graph data has to stay readable to whoever
        # runs make_graph.py, not just to the service user that writes it.
        usage_stats.ensure_ledger_permissions(usage_stats.LEDGER_PATH)

    while True:
        time.sleep(10)
        _reap_finished_jobs()

        if prune_enabled and time.monotonic() >= next_prune:
            next_prune = time.monotonic() + JOB_PRUNE_INTERVAL_SECONDS
            _prune_job_output()


reaper = threading.Thread(target=_reaper_thread, daemon=True)
reaper.start()

if __name__ == "__main__":
    # Only for local dev; never auto-enable from env
    app.run(debug=False)
