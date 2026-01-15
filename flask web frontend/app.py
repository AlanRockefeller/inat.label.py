from flask import Flask, request, jsonify, send_file, url_for, render_template, send_from_directory, redirect
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
from uuid import uuid4
from functools import partial

import threading
from logging.handlers import RotatingFileHandler

INAT_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'flask-labels (inat.label.py frontend)'
}

# Rate limiting for iNaturalist API
api_lock = threading.Lock()
next_api_call_time = 0.0

# Hardening settings
MAX_OBS_PER_REQUEST = int(os.environ.get('MAX_OBS_PER_REQUEST', '500'))
MAX_CONCURRENT_JOBS = int(os.environ.get('MAX_CONCURRENT_JOBS', '3'))
FINISHED_JOB_TTL = int(os.environ.get('FINISHED_JOB_TTL', '300')) # Time in seconds to keep finished jobs
ENABLE_MO_DEBUG = bool(int(os.environ.get('ENABLE_MO_DEBUG', '0')))
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS')  # comma-separated list of allowed origins

# Observation fields that inat.label.py automatically includes on labels when present.
# These should be checked by default in the Add Fields modal.
# This list is derived from create_inaturalist_label() in inat.label.py.
DEFAULT_LABEL_FIELDS = [
    # DNA Barcode fields
    'DNA Barcode ITS',
    'DNA Barcode LSU',
    'DNA Barcode RPB1',
    'DNA Barcode RPB2',
    'DNA Barcode TEF1',
    # Other optional fields that get added if present
    'GenBank Accession Number',
    'GenBank Accession',
    'Provisional Species Name',
    'Species Name Override',
    'Microscopy Performed',
    'Fungal Microscopy',
    'Mobile or Traditional Photography?',
    "Collector's name",
    'Herbarium Catalog Number',
    'Fungarium Catalog Number',
    'Herbarium Secondary Catalog Number',
    'Habitat',
    'Microhabitat',
    'Collection Number',
    'Associated Species',
    'Herbarium Name',
    'Mycoportal ID',
    'Voucher Number',
    'Voucher Number(s)',
    'Accession Number',
    'Mushroom Observer URL',
]

# In-memory job store for streaming print jobs
_jobs = {}
_jobs_lock = threading.Lock()

def _reap_finished_jobs_locked():
    now = time.time()
    finished_to_reap = []
    for job_id, job in _jobs.items():
        if job.get('finished_time'):
            if now > job['finished_time'] + FINISHED_JOB_TTL:
                finished_to_reap.append(job_id)
        elif job['proc'].poll() is not None:
            # Process finished, but not yet marked. Mark it now.
            job['finished_time'] = now

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
            kwargs.setdefault('headers', INAT_HEADERS)
            kwargs.setdefault('timeout', 20)
            response = requests.get(url, **kwargs)
            next_api_call_time = time.time() + 1.0
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException:
            app.logger.exception("Error during iNaturalist API request.")
            next_api_call_time = time.time() + 1.0
            raise


app = Flask(__name__)

# Configure logging
log_dir = os.path.join(app.root_path, 'logs')
os.makedirs(log_dir, exist_ok=True)
error_log_path = os.path.join(log_dir, 'error.log')
file_handler = RotatingFileHandler(error_log_path, maxBytes=1024*1024, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.WARNING)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.WARNING)

# Command logger
cmd_log_path = os.path.join(log_dir, 'app.log')
cmd_handler = RotatingFileHandler(cmd_log_path, maxBytes=1024*1024, backupCount=5)
cmd_handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s'))
cmd_logger = logging.getLogger('cmd_logger')
cmd_logger.setLevel(logging.INFO)
cmd_logger.addHandler(cmd_handler)

# API Error logger
api_err_log_path = os.path.join(log_dir, 'api_error.log')
api_err_handler = RotatingFileHandler(api_err_log_path, maxBytes=1024*1024, backupCount=5)
api_err_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
api_error_logger = logging.getLogger('api_error_logger')
api_error_logger.setLevel(logging.WARNING)
api_error_logger.addHandler(api_err_handler)

# Only enable CORS when explicitly configured; same-origin requests do not need CORS
if ALLOWED_ORIGINS:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(',') if o.strip()]
    if origins:
        CORS(app, resources={r"/labels/*": {"origins": origins}})

# Helper function to extract observation type and ID from raw input
def extract_obs_id(obs_input):
    # Convert to lowercase for case-insensitive comparison
    input_lower = obs_input.lower() 
    
    # Handles mo:XXXXX format separately for direct access
    if input_lower.startswith('mo:'):
        mo_number = obs_input[3:].strip()
        if mo_number.isdigit():
            return 'mo_direct', mo_number

    # Original logic
    if '://' in obs_input:
        if 'inaturalist.org/observations/' in input_lower:
            match = re.search(r'/observations/(\d+)', obs_input)
            if match:
                return 'inat', match.group(1)
        elif 'mushroomobserver.org' in input_lower:
            match = re.search(r'/(\d+)$', obs_input)
            if match:
                return 'mo_direct', match.group(1)
    else:
        # Handle MOTOINAT/MOINAT/INATMO formats for conversion (MO -> iNat)
        if input_lower.startswith('motoinat') or input_lower.startswith('moinat') or input_lower.startswith('inatmo'):
            mo_number = re.search(r'\d+', obs_input)
            if mo_number:
                return 'mo', mo_number.group(0)
        # Regular MO format - now treated as direct (pass through to generator)
        elif input_lower.startswith('mo'):
            mo_number = re.search(r'\d+', obs_input)
            if mo_number:
                return 'mo_direct', mo_number.group(0)
        elif obs_input.isdigit():
            return 'inat', obs_input
    raise ValueError(f'Invalid observation input: {obs_input}')

# Function to convert MO number to iNaturalist ID or return iNaturalist ID
def get_inat_id(obs_input):
    obs_type, obs_id = extract_obs_id(obs_input)
    
    # Direct MO observation - return with MO prefix (uppercase) for generator compatibility
    if obs_type == 'mo_direct':
        return f'MO{obs_id}'
        
    # Convert MO to iNat (motoinat)
    if obs_type == 'mo':
        try:
            result = subprocess.run(['python', os.path.join(app.root_path, 'motoinat.py'), '-q', obs_id],
                                  capture_output=True, text=True, check=True)
            inat_id = result.stdout.strip()
            if inat_id.isdigit():
                return inat_id
            else:
                raise ValueError(f'No iNaturalist observation found for MO #{obs_id}')
        except subprocess.CalledProcessError as e:
            raise ValueError(f'Error converting MO #{obs_id} to iNaturalist: {e.stderr.strip()}')
    
    # iNat ID
    return obs_id

@app.route('/')
@app.route('/labels')
@app.route('/labels/')
def labels():
    return render_template('index.html', default_label_fields=DEFAULT_LABEL_FIELDS)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

def lookup_batch_internal(obs_inputs):
    # Prepare containers
    results = [{'input': oi} for oi in obs_inputs]
    inat_ids = []
    inat_map_indices = {}
    mo_numbers = []
    mo_map_indices = {}

    # Resolve inputs to either iNat IDs or MO IDs
    for idx, obs_input in enumerate(obs_inputs):
        try:
            resolved = get_inat_id(obs_input)
        except ValueError as e:
            results[idx]['error'] = str(e)
            results[idx]['status'] = 400
            continue
        # Direct MO
        if isinstance(resolved, str) and resolved.upper().startswith('MO'):
            mo_num = resolved[2:]
            mo_numbers.append(mo_num)
            mo_map_indices.setdefault(mo_num, []).append(idx)
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
            chunk = inat_ids[i:i + chunk_size]
            
            try:
                params = {'id': ','.join(chunk)}
                response = inat_api_get('https://api.inaturalist.org/v1/observations', params=params)
                data = response.json()
                for r in data.get('results', []):
                    if 'id' in r:
                        id_to_result[str(r['id'])] = r
            except requests.exceptions.RequestException as e:
                # Apply a generic error to all inat IDs in the failed chunk
                msg = f'Error fetching iNaturalist data: {str(e)}'
                for inat_id in chunk:
                    for idx in inat_map_indices.get(inat_id, []):
                        if 'error' not in results[idx]: # Avoid overwriting previous errors
                            results[idx]['error'] = msg
                            results[idx]['status'] = 500
                continue

        # Fill results from the fetched data
        for inat_id in inat_ids:
            indices = inat_map_indices.get(inat_id, [])
            # Skip if an error was already recorded for this chunk
            if any('error' in results[idx] for idx in indices):
                continue
            r = id_to_result.get(inat_id)
            if not r:
                for idx in indices:
                    results[idx]['error'] = f'iNaturalist Observation #{inat_id} does not exist'
                    results[idx]['status'] = 404
                continue
            taxon = r.get('taxon') or {}
            user = r.get('user') or {}
            scientific_name = taxon.get('name', 'Unknown')
            user_login = user.get('login', 'Unknown')
            iconic = taxon.get('iconic_taxon_name', '')
            color = 'black'
            if iconic == 'Fungi':
                color = 'magenta'
            elif iconic == 'Plantae':
                color = 'green'
            elif iconic == 'Protozoa':
                color = 'purple'
            elif iconic == 'Insecta':
                color = 'red'
            elif iconic in ('Aves', 'Reptilia', 'Mammalia'):
                color = 'blue'
            for idx in indices:
                results[idx].update({
                    'original_input': obs_inputs[idx],
                    'inat_id': inat_id,
                    'scientific_name': scientific_name,
                    'user_id': user_login,
                    'color': color,
                    'ofvs': r.get('ofvs', [])
                })

    # Fetch MO observations (API supports detail=high, ids= may support multiple; use per-ID for safety)
    for mo_num in mo_numbers:
        try:
            api_url = f'https://mushroomobserver.org/api2/observations/{mo_num}.json?detail=high'
            mo_response = requests.get(api_url, timeout=20)
            mo_response.raise_for_status()
            mo_data = mo_response.json()
            result = None
            if mo_data and 'results' in mo_data and mo_data['results']:
                result = mo_data['results'][0]
                if isinstance(result, int):
                    # Fetch full detail via ids=
                    api_url = f'https://mushroomobserver.org/api2/observations?ids={mo_num}&detail=high'
                    mo_response = requests.get(api_url, timeout=20)
                    mo_response.raise_for_status()
                    mo_data = mo_response.json()
                    if mo_data and 'results' in mo_data and mo_data['results']:
                        result = mo_data['results'][0]
                    else:
                        result = None
            if not isinstance(result, dict):
                for idx in mo_map_indices.get(mo_num, []):
                    results[idx]['error'] = f'No data found for Mushroom Observer #{mo_num}'
                    results[idx]['status'] = 404
                continue
            scientific_name = 'Unknown'
            user_id = 'Unknown'

            consensus = result.get('consensus') or {}
            owner = result.get('owner') or {}
            scientific_name = consensus.get('name') or result.get('name', 'Unknown')
            user_id = owner.get('login_name') or result.get('login_name', 'Unknown')
            
            ofvs = []
            mo_url = f"https://mushroomobserver.org/obs/{mo_num}"
            ofvs.append({
                'name': 'Mushroom Observer URL',
                'value': mo_url
            })
            if 'herbarium_name' in result:
                ofvs.append({
                    'name': 'Herbarium Name',
                    'value': result.get('herbarium_name', '')
                })
            if 'herbarium_id' in result:
                ofvs.append({
                    'name': 'Herbarium Catalog Number',
                    'value': result.get('herbarium_id', '')
                })
            if 'sequences' in result and result['sequences']:
                for sequence in result['sequences']:
                    locus = sequence.get('locus', '').upper()
                    bases = sequence.get('bases', '')
                    if locus and bases:
                        locus_mapping = {
                            'ITS': 'DNA Barcode ITS',
                            'LSU': 'DNA Barcode LSU',
                            'TEF1': 'DNA Barcode TEF1',
                            'EF1': 'DNA Barcode TEF1',
                            'RPB1': 'DNA Barcode RPB1',
                            'RPB2': 'DNA Barcode RPB2'
                        }
                        field_name = locus_mapping.get(locus)
                        if field_name:
                            cleaned_bases = ''.join(bases.split())
                            bp_count = len(cleaned_bases)
                            ofvs.append({
                                'name': field_name,
                                'value': f"{bp_count} bp"
                            })
            for idx in mo_map_indices.get(mo_num, []):
                results[idx].update({
                    'original_input': obs_inputs[idx],
                    'inat_id': f'MO{mo_num}',
                    'scientific_name': scientific_name,
                    'user_id': user_id,
                    'color': 'magenta',
                    'ofvs': ofvs
                })
        except Exception as e:
            app.logger.exception(e)
            for idx in mo_map_indices.get(mo_num, []):
                results[idx]['error'] = f'Error processing Mushroom Observer #{mo_num}: {str(e)}'
                results[idx]['status'] = 500

    # Normalize output: ensure items list of dicts with either error or data
    items = []
    for idx, base in enumerate(results):
        if 'error' in base:
            items.append({
                'input': obs_inputs[idx],
                'error': base['error'],
                'status': base.get('status', 400)
            })
        else:
            items.append({
                'original_input': base.get('original_input', obs_inputs[idx]),
                'inat_id': base.get('inat_id', ''),
                'scientific_name': base.get('scientific_name', 'Unknown'),
                'user_id': base.get('user_id', 'Unknown'),
                'color': base.get('color', 'black'),
                'ofvs': base.get('ofvs', [])
            })
    return {'items': items}


@app.route('/labels/lookup_batch', methods=['POST'])
def lookup_batch():
    obs_inputs = request.form.getlist('obs_ids[]')
    if not obs_inputs:
        return jsonify({'error': 'No observation IDs provided'}), 400
    if len(obs_inputs) > MAX_OBS_PER_REQUEST:
        return jsonify({'error': f'Too many observations in one request (max {MAX_OBS_PER_REQUEST})'}), 400
    payload = lookup_batch_internal(obs_inputs)
    return jsonify(payload)

@app.route('/labels/submit', methods=['POST'])
def submit():
    try:
        raw_observations = [obs.strip() for obs in request.form.getlist('observations[]') if obs.strip()]
        if not raw_observations:
            return "No observations provided", 400
        if len(raw_observations) > MAX_OBS_PER_REQUEST:
            return f"Too many observations in one request (max {MAX_OBS_PER_REQUEST})", 400

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
        inat_ids = [str(x) for x in resolved if not (isinstance(x, str) and x.upper().startswith('MO'))]

        # Batch fetch iNat observations in chunks
        id_to_inat = {}
        if inat_ids:
            CHUNK = 50
            for i in range(0, len(inat_ids), CHUNK):
                chunk = inat_ids[i:i+CHUNK]
                try:
                    params = {'id': ','.join(chunk)}
                    resp = inat_api_get('https://api.inaturalist.org/v1/observations', params=params, timeout=30)
                    data = resp.json()
                    for r in data.get('results', []):
                        if 'id' in r:
                            id_to_inat[str(r['id'])] = r
                except requests.exceptions.RequestException as e:
                    app.logger.warning(f"iNat batch fetch failed for chunk starting {chunk[0]}: {e}")
                    continue

        def safe_csv_field(val):
            try:
                s = str(val)
            except Exception:
                s = ''
            if s and s[0] in ('=', '+', '-', '@'):
                return "'" + s
            return s

        csv_data = [['ID', 'Observation Number', 'Scientific Name', 'Observer']]
        valid_counter = 0

        # Build CSV rows in original order
        for rid in resolved:
            rid_str = str(rid)
            # MO observations (fetch per-ID with detail fallback)
            if rid_str.upper().startswith('MO'):
                valid_counter += 1
                mo_number = rid_str[2:]
                try:
                    mo_response = requests.get(f'https://mushroomobserver.org/api2/observations/{mo_number}.json?detail=high', timeout=20)
                    mo_response.raise_for_status()
                    mo_data = mo_response.json()
                    if mo_data and 'results' in mo_data and mo_data['results'] and isinstance(mo_data['results'][0], int):
                        mo_response = requests.get(f'https://mushroomobserver.org/api2/observations?ids={mo_number}&detail=high', timeout=20)
                        mo_response.raise_for_status()
                        mo_data = mo_response.json()
                    if mo_data and 'results' in mo_data and mo_data['results'] and isinstance(mo_data['results'][0], dict):
                        result = mo_data['results'][0]
                        
                        consensus = result.get('consensus') or {}
                        owner = result.get('owner') or {}
                        scientific_name = consensus.get('name') or result.get('name', 'Unknown')
                        user_login = owner.get('login_name') or result.get('login_name', 'Unknown')

                        csv_data.append([valid_counter, safe_csv_field(rid_str), safe_csv_field(scientific_name), safe_csv_field(user_login)])
                    else:
                        csv_data.append([valid_counter, safe_csv_field(rid_str), 'Unknown', 'Unknown'])
                except Exception as e:
                    app.logger.warning(f"Error fetching MO data: {str(e)}")
                    csv_data.append([valid_counter, rid_str, 'Unknown (API Error)', 'Unknown'])
                continue
            # iNaturalist observation from batch map
            r = id_to_inat.get(rid_str)
            if r:
                valid_counter += 1
                taxon = r.get('taxon') or {}
                user = r.get('user') or {}
                scientific_name = taxon.get('name', 'Unknown')
                user_login = user.get('login', 'Unknown')
                csv_data.append([valid_counter, safe_csv_field(rid_str), safe_csv_field(scientific_name), safe_csv_field(user_login)])
            # If r missing, skip adding a row to preserve numbering semantics like previous implementation

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(csv_data)
        csv_content = output.getvalue()

        return csv_content, 200, {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=observations.csv'}
    except Exception as e:
        app.logger.exception(e)
        return "An internal error occurred while generating the CSV file.", 500


# Streaming printing support
@app.route('/labels/print_start', methods=['POST'])
def print_start():
    fmt = (request.form.get('format') or 'rtf').lower()
    if fmt not in ('rtf', 'pdf'):
        app.logger.warning(f"print_start: Invalid format requested: {fmt}")
        return jsonify({'error': 'Invalid format'}), 400

    omit_qr_codes = request.form.get('omit_qr_codes')
    raw_observations = request.form.getlist('observations[]')
    if not raw_observations:
        app.logger.warning("print_start: No observations provided")
        return jsonify({'error': 'No observations provided'}), 400
    if len(raw_observations) > MAX_OBS_PER_REQUEST:
        app.logger.warning(f"print_start: Too many observations requested: {len(raw_observations)}, max is {MAX_OBS_PER_REQUEST}")
        return jsonify({'error': f'Too many observations in one request (max {MAX_OBS_PER_REQUEST})'}), 400
    with _jobs_lock:
        _reap_finished_jobs_locked()  # clean finished/expired entries immediately

        active = 0
        for job in _jobs.values():
            proc = job.get('proc')
            if proc and proc.poll() is None:
                active += 1

        if active >= MAX_CONCURRENT_JOBS:
            error_message = f'Too many concurrent jobs ({active}), max is {MAX_CONCURRENT_JOBS}. Please try again shortly.'
            app.logger.warning(f"print_start: {error_message}")
            return jsonify({'error': error_message}), 429

    inat_ids = []
    for obs in raw_observations:
        try:
            inat_id = get_inat_id(obs)
            inat_ids.append(inat_id)
        except ValueError as e:
            app.logger.warning(str(e))
            continue

    if not inat_ids:
        app.logger.warning("print_start: No valid observations found after processing raw input.")
        return jsonify({'error': 'No valid observations provided'}), 400

    script_path = os.path.join(app.root_path, 'inat.label.py')
    static_dir = os.path.join(app.root_path, 'static')
    job_id = str(uuid4())
    job_dir = os.path.join(static_dir, 'jobs', job_id)
    filename = 'labels.rtf' if fmt == 'rtf' else 'labels.pdf'
    output_path = os.path.join(job_dir, filename)
    os.makedirs(job_dir, exist_ok=True)
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    command = [
        sys.executable,
        '-u',     
        script_path,
        *inat_ids,
        *(["--rtf", output_path] if fmt == 'rtf' else ["--pdf", output_path]),
    ]
    if omit_qr_codes:
        command.append('--no-qr')
    if request.form.get('minilabel'):
        command.append('--minilabel')
    if request.form.get('common_names'):
        command.append('--common-names')
    if request.form.get('omit_notes'):
        command.append('--omit-notes')
    if request.form.get('use_custom'):
        custom_args = request.form.getlist('custom_args[]')
        if custom_args:
            command.append('--custom')
            # Join all custom fields with commas as inat.label.py expects a comma-separated list
            command.append(', '.join(custom_args))
    cmd_logger.info(' '.join(command))
    app.logger.debug(f"Starting streaming command: {' '.join(command)}")

    # Start subprocess for streaming
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,        # line-buffered
        text=True,
        encoding='utf-8',
        errors='replace',
    )

    with _jobs_lock:
        _jobs[job_id] = {
            'proc': proc,
            'output_path': output_path,
            'filename': filename,
        }

    return jsonify({'job_id': job_id})


@app.route('/labels/print_stream')
def print_stream():
    job_id = request.args.get('job_id')

    with _jobs_lock:
        if not job_id or job_id not in _jobs:
            # Job is already gone, possibly reaped.
            # Return an immediate SSE 'done' event with an error.
            def generate_reaped_error():
                error_payload = json.dumps({
                    'success': False,
                    'error': 'Job not found. It may have been completed and cleaned up.',
                    'exit_code': -1,
                })
                yield f"event: done\ndata: {error_payload}\n\n"
            return app.response_class(
                generate_reaped_error(),
                mimetype='text/event-stream',
                headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
            )
        job = _jobs[job_id]
        proc = job['proc']
        output_path = job['output_path']

    rel_path = os.path.relpath(
        output_path,
        os.path.join(app.root_path, 'static')
    ).replace('\\', '/')
    download_url = url_for('static', filename=rel_path)

    def generate():
        try:
            # Stream already-written and future output
            for line in iter(proc.stdout.readline, ''):
                line = line.rstrip('\n')
                # Small debug hook if you want:
                # app.logger.debug(f"SSE log line: {line!r}")

                yield f"event: log\ndata: {json.dumps(line)}\n\n"

            # When the loop ends, the process has finished
            exit_code = proc.wait()

            if os.path.exists(output_path):
                done_payload = json.dumps({
                    'success': True,
                    'download_url': download_url,
                    'exit_code': exit_code,
                })
            else:
                error_message = (
                    f'Output file was not created. '
                    f'Process exited with code {exit_code}.'
                )
                done_payload = json.dumps({
                    'success': False,
                    'error': error_message,
                    'exit_code': exit_code,
                })

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
                    _jobs[job_id]['finished_time'] = time.time()

    return app.response_class(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # nginx hint; harmless even if not used
        }
    )


@app.route('/labels/find_observations', methods=['POST'])
def find_observations():
    """Find iNaturalist observation IDs by date range, username, and taxon (including descendants)."""
    d1_str = (request.form.get('d1') or '').strip()
    d2_str = (request.form.get('d2') or '').strip()
    username = (request.form.get('username') or '').strip().replace(' ', '_')
    taxon_input = (request.form.get('taxon') or '').strip()

    if not d1_str or not d2_str or not username or not taxon_input:
        missing_fields = []
        if not d1_str:
            missing_fields.append('Start Date')
        if not d2_str:
            missing_fields.append('End Date')
        if not username:
            missing_fields.append('Username')
        if not taxon_input:
            missing_fields.append('Taxon')
        return jsonify({'error': f'Missing required fields: {", ".join(missing_fields)}'}), 400

    # Validate dates YYYY-MM-DD
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', d1_str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', d2_str):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    # Resolve taxon_id (accept numeric id or search by name)
    taxon_id = None
    if taxon_input.isdigit():
        taxon_id = int(taxon_input)
    else:
        try:
            resp = inat_api_get(
                'https://api.inaturalist.org/v1/taxa',
                params={'q': taxon_input, 'per_page': 1},
                timeout=15,
            )
            tdata = resp.json()
            if tdata.get('results'):
                taxon_id = tdata['results'][0].get('id')
            else:
                return jsonify({'error': f'Taxon not found: {taxon_input}'}), 404
        except requests.RequestException as e:
            api_error_logger.warning(f"Taxon lookup failed: {str(e)}", exc_info=True)
            return jsonify({'error': f'Error looking up taxon: {str(e)}'}), 500

    # Query observations
    found = []
    cap = MAX_OBS_PER_REQUEST + 1
    last_id = 0
    current_batch = []

    try:
        while len(current_batch) < cap:
            params = {
                'user_login': username,
                'd1': d1_str,
                'd2': d2_str,
                'taxon_id': taxon_id,
                'per_page': 200,
                'order': 'asc',
                'order_by': 'id',
            }
            if last_id > 0:
                params['id_above'] = last_id

            resp = inat_api_get('https://api.inaturalist.org/v1/observations', params=params, timeout=30)
            data = resp.json()
            results = data.get('results', [])
            if not results:
                break
            
            for r in results:
                if len(current_batch) >= cap:
                    break
                oid = r.get('id')
                if oid:
                    last_id = oid
                taxon = r.get('taxon') or {}
                
                iconic = taxon.get('iconic_taxon_name', '')
                color = 'black'
                if iconic == 'Fungi': color = 'magenta'
                elif iconic == 'Plantae': color = 'green'
                elif iconic == 'Protozoa': color = 'purple'
                elif iconic == 'Insecta': color = 'red'
                elif iconic in ('Aves', 'Reptilia', 'Mammalia'): color = 'blue'

                current_batch.append({
                    'id': oid, 'scientific_name': taxon.get('name', ''),
                    'iconic_taxon_name': iconic, 'observed_on': r.get('observed_on'),
                    'color': color
                })
        found = current_batch
    except requests.RequestException as e:
        api_error_logger.warning(f"Observation fetch for user '{username}' failed: {str(e)}", exc_info=True)
        error_message = f'Error fetching observations: {str(e)}'
        try:
            if e.response:
                error_details = e.response.json()
                if 'error' in error_details:
                    error_message = f"Error fetching observations: {error_details['error']}"
        except ValueError:
            pass
        return jsonify({'error': error_message}), 500

    found.reverse()
    return jsonify({'count': len(found), 'items': found}), 200


@app.route('/labels/help')
def serve_help():
    return app.send_static_file('help.html')

@app.route('/labels/todo', methods=['GET', 'POST'])
def todo():
    todo_file = os.path.join(app.root_path, 'static', 'todos.txt')
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        suggestion = request.form.get('suggestion', '').strip()

        # Sanitize input: allow only alphanumeric, spaces, and some punctuation, including Spanish characters
        name = re.sub(r'[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', name)
        suggestion = re.sub(r'[^a-zA-Z0-9 .,!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', suggestion)

        if name and suggestion:
            with open(todo_file, 'a') as f:
                f.write(f'{name}: {suggestion}\n')
        return redirect(url_for('todo'))

    todos = []
    if os.path.exists(todo_file):
        with open(todo_file, 'r') as f:
            todos = [line.strip() for line in f.readlines()]
    return render_template('todo.html', todos=todos)

# Start a background thread to reap finished jobs
def _reaper_thread():
    while True:
        time.sleep(10)
        _reap_finished_jobs()

reaper = threading.Thread(target=_reaper_thread, daemon=True)
reaper.start()

if __name__ == '__main__':
    # Only for local dev; never auto-enable from env
    app.run(debug=False)

