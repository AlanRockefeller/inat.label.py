#!/usr/bin/env python3

"""
iNaturalist and Mushroom Observer Herbarium Label Generator

Author: Alan Rockefeller
Date: September 23, 2025
Version: 2.7

This script creates herbarium labels from iNaturalist or Mushroom Observer observation numbers or URLs.
It fetches data from the respective APIs and formats it into printable labels suitable for
herbarium specimens. While it can output the labels to stdout, the RTF output makes more
professional looking labels that include a QR code.

Features:
- Supports multiple observation IDs or URLs as input from both iNaturalist and Mushroom Observer
- Recognizes Mushroom Observer IDs in format "MO" followed by 4-6 digits (e.g., MO2345)
- Supports Mushroom Observer URLs in various formats
- Can output labels to the console or to an RTF file
- Includes various data fields such as scientific name, common name, location,
  GPS coordinates, observation date, observer, and more
- Handles special fields like DNA Barcode ITS (and LSU, TEF1, RPB1, RPB2), GenBank Accession Number,
  Provisional Species Name, Mobile or Traditional Photography?, Microscopy Performed, Herbarium Catalog Number,
  Herbarium Name, Mycoportal ID, Voucher number(s)
- Generates a QR code which links to the observation URL

Usage:
1. Basic usage (output to console - mostly just for testing):
   ./inat_label.py <observation_number_or_url> [<observation_number_or_url> ...]

2. Output to RTF file: (recommended - much better formatting and adds a QR code)
   ./inat_label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>

Examples:
- Generate label for a single iNaturalist observation:
  ./inat_label.py 150291663

- Generate label for a single Mushroom Observer observation:
  ./inat_label.py MO2345

- Generate labels for multiple observations from both platforms:
  ./inat_label.py 150291663 MO2345 https://www.inaturalist.org/observations/105658809 https://mushroomobserver.org/395895

- Generate labels and save to an RTF file:
  ./inat_label.py 150291663 MO2345 --rtf two_labels.rtf

Notes:
- The RTF output is formatted to closely match the style of traditional herbarium labels.
- It is recommended to print herbarium labels on 100% cotton cardstock with an inkjet printer for maximum longevity.

Dependencies:
- requests
- dateutil
- beautifulsoup4
- qrcode
- colorama
- replace-accents

The dependencies can be installed with the following command:

    pip install requests python-dateutil beautifulsoup4 qrcode[pil] colorama replace-accents pillow reportlab

Python version 3.6 or higher is recommended.

"""

import argparse
import colorama
from colorama import Fore, Style
import datetime
import os
import re
import sys
import time
import unicodedata
import random
from io import BytesIO
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from collections import deque
from replace_accents import replace_accents_characters
import binascii
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
import qrcode
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Image as ReportLabImage, KeepTogether, Table, TableStyle, KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import inch
from reportlab.lib.colors import black, blue, green, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

PDF_BASE_FONT = os.environ.get('PDF_BASE_FONT', 'Times-Roman')

# Global session with connection pooling
_session = None

# Simple thread-safe rate limiter: max 60 requests per 60 seconds (configurable via env)
RATE_LIMIT_RPM = int(os.environ.get("INAT_RATE_LIMIT_RPM", "60"))
_rate_lock = threading.Lock()
_request_times = deque()

# Dynamic concurrency control (can be lowered on repeated 429s)
_conc_lock = threading.Lock()
_concurrency_target = int(os.environ.get('INAT_MAX_WORKERS', '5'))
_active_requests = 0
_recent_429 = deque()  # timestamps of recent 429s

# Retry/quiet controls (tunable from CLI or env)
_MAX_WAIT_SECONDS = float(os.environ.get('INAT_MAX_WAIT_SECONDS', '30'))
_QUIET = bool(int(os.environ.get('INAT_QUIET', '0')))

def _rate_limit_wait():
    """Block if we've made RATE_LIMIT_RPM requests in the last 60 seconds, and if active threads exceed target."""
    if RATE_LIMIT_RPM <= 0:
        pass
    window = 60.0
    while True:
        # Concurrency gate
        with _conc_lock:
            if _active_requests < _concurrency_target:
                break
        time.sleep(0.01)
    # Rate window gate
    while True:
        with _rate_lock:
            now = time.time()
            # Drop timestamps outside the window
            while _request_times and now - _request_times[0] > window:
                _request_times.popleft()
            if len(_request_times) < RATE_LIMIT_RPM:
                _request_times.append(now)
                return
            # Need to wait until the oldest timestamp leaves the window
            sleep_time = max(0.01, window - (now - _request_times[0]))
        time.sleep(sleep_time)

def get_session():
    """Get or create a requests session with connection pooling."""
    global _session
    if _session is None:
        _session = requests.Session()
        # Let our own code handle 429s with Retry-After; adapter will only retry on transient 5xx
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
    return _session

def print_error(message):
    """Print error message in red using colorama for cross-platform support."""
    try:
        print(Fore.RED + str(message) + Style.RESET_ALL)
    except Exception:
        # Fallback if colorama not available for some reason
        print(f"\033[91m{message}\033[0m")


def register_fonts():
    global PDF_BASE_FONT
    try:
        pdfmetrics.registerFont(TTFont('Liberation Serif', 'LiberationSerif-Regular.ttf'))
        pdfmetrics.registerFont(TTFont('Liberation Serif-Bold', 'LiberationSerif-Bold.ttf'))
        pdfmetrics.registerFont(TTFont('Liberation Serif-Italic', 'LiberationSerif-Italic.ttf'))
        pdfmetrics.registerFont(TTFont('Liberation Serif-BoldItalic', 'LiberationSerif-BoldItalic.ttf'))
        pdfmetrics.registerFontFamily('Liberation Serif', normal='Liberation Serif', bold='Liberation Serif-Bold', italic='Liberation Serif-Italic', boldItalic='Liberation Serif-BoldItalic')
        PDF_BASE_FONT = 'Liberation Serif'
    except Exception as e:
        print_error("Warning: Liberation Serif font not found. Falling back to default font.")

register_fonts()

RTF_HEADER = r"""{\rtf1\ansi\deff3\adeflang1025
{\fonttbl{\f0\froman\fprq2\fcharset0 Times New Roman;}{\f1\froman\fprq2\fcharset2 Symbol;}{\f2\fswiss\fprq2\fcharset0 Arial;}{\f3\froman\fprq2\fcharset0 Liberation Serif{\*\falt Times New Roman};}{\f4\froman\fprq2\fcharset0 Arial;}{\f5\froman\fprq2\fcharset0 Tahoma;}{\f6\froman\fprq2\fcharset0 Times New Roman;}{\f7\froman\fprq2\fcharset0 Courier New;}{\f8\fnil\fprq2\fcharset0 Times New Roman;}{\f9\fnil\fprq2\fcharset0 Lohit Hindi;}{\f10\fnil\fprq2\fcharset0 DejaVu Sans;}}
{\colortbl;\red0\green0\blue0;\red0\green0\blue255;\red0\green255\blue255;\red0\green255\blue0;\red255\green0\blue255;\red255\green0\blue0;\red255\green255\blue0;\red255\green255\blue255;\red0\green0\blue128;\red0\green128\blue128;\red0\green128\blue0;\red128\green0\blue128;\red128\green0\blue0;\red128\green128\blue0;\red128\green128\blue128;\red192\green192\blue192;}
{\stylesheet{\s0\snext0\ql\keep\nowidCtl\sb0\sa720\ltrpar\hyphpar0\aspalpha\cf0\f6\fs24\lang1033\kerning1 Normal;}
{\*\cs15\snext15 Default Paragraph Font;}
{\s16\sbasedon0\snext17\ql\keep\nowidctl\sb240\sa120\keepn\ltrpar\cf0\f4\fs28\lang1033\kerning1 Heading;}
{\s17\sbasedon0\snext17\ql\keep\nowidctl\sb0\sa120\ltrpar\cf0\f6\fs24\lang1033\kerning1 Text Body;}
{\s18\sbasedon17\snext18\ql\keep\nowidctl\sb0\sa120\ltrpar\cf0\f7\fs24\lang1033\kerning1 List;}
{\s19\sbasedon0\snext19\ai\ql\keep\nowidctl\sb120\sa120\ltrpar\cf0\f6\fs24\lang1033\i\kerning1 Caption;}
{\s20\sbasedon0\snext20\ql\keep\nowidctl\sb0\sa720\ltrpar\cf0\f7\fs24\lang1033\kerning1 Index;}
{\s21\sbasedon0\snext21\ai\ql\keep\nowidctl\sb120\sa120\ltrpar\cf0\f7\fs24\lang1033\i\kerning1 caption;}
{\s22\sbasedon0\snext22\ql\keep\nowidctl\sb0\sa720\ltrpar\cf0\f5\fs16\lang1033\kerning1 Balloon Text;}
{\s23\sbasedon0\snext23\ql\keep\nowidctl\sb0\sa720\ltrpar\cf0\f6\fs24\lang1033\kerning1 Table Contents;}
{\s24\sbasedon23\snext24\ab\qc\keep\nowidctl\sb0\sa720\ltrpar\cf0\f6\fs24\lang1033\b\kerning1 Table Heading;}
}
\formshade\paperh15840\paperw12240\margl360\margr360\margt360\margb360\sectd\sbknone\sectunlocked1\pgndec\pgwsxn12240\pghsxn15840\marglsxn360\margrsxn360\margtsxn360\margbsxn360\cols2\colsx720\ftnbj\ftnstart1\ftnrstcont\ftnnar\aenddoc\aftnrstcont\aftnstart1\aftnnrlc
\pard\plain \s0\ql\tx113
"""



def generate_qr_code(url):
    try:
        qr = qrcode.QRCode(version=1, box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # Resize the QR code here if desired
        scale_factor = 2 # Resize to 2x the original size
        img = img.resize((int(img.size[0] * scale_factor), int(img.size[1] * scale_factor)), Image.LANCZOS)

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()
        img_hex = binascii.hexlify(img_bytes).decode('utf-8')

        # Save the QR code to a PNG file for debugging
        # img.save(filename)
        return img_hex, img.size  # Return the hex string and the size of the image
    except Exception as e:
        print(f"Error generating QR code: {e}")
        return None, None

def escape_rtf(text):
    """Escape special characters for RTF output.  This section may need additional changes as more unusual characters are encountered, usually in the location."""
    rtf_char_map = {
        '\\': '\\\\',
        '{': '\\{',
        '}': '\\}',
        '\n': '\\line ',
        'í': '\\u237\'',
        '\\"': '\\u34\'',           #  Does not work, yet - see https://www.perplexity.ai/search/If-the-RTF-gOdEwtp2TnmQZoPfQGqpsQ
        'µ': '\\u181?',
        '×': '\\u215?',
        '“': '\\ldblquote ',   # left double quotation mark U+201C
        '”': '\\rdblquote ',   # right double quotation mark U+201D
        '‘': '\\lquote ',      # left single quotation mark U+2018
        '’': '\\rquote ',      # right single quotation mark U+2019
        '–': '\\endash ',
        '—': '\\emdash ',
        'é': '\\\'e9',
        'à': '\\u224\'',
        'á': '\\u225\'',
        'ä': '\\\'e4',
        'ö': '\\\'f6',
        'ü': '\\\'fc',
        'ß': '\\\'df',
        '\'': '\\\'27',
    }
    for char, replacement in rtf_char_map.items():
        text = text.replace(char, replacement)
    return text

# Remove formatting tags in stdout
def remove_formatting_tags(text):
    tags_to_remove = ['__BOLD_START__', '__BOLD_END__', '__ITALIC_START__', '__ITALIC_END__']
    for tag in tags_to_remove:
        text = text.replace(tag, '')
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.replace('<br/>', '').strip()
        if not line or re.match(r'^[\d\W]+$', line):
            continue
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)


def parse_html_notes(notes):
    if not notes or '<' not in notes:
        return notes  # Return the original notes if it's empty or doesn't contain HTML tags

    soup = BeautifulSoup(notes, 'html.parser')

    # Replace <p> with line breaks
    for p in soup.find_all('p'):
        p.unwrap()

    # Convert hyperlinks to text URLs
    for a in soup.find_all('a'):
        a.replace_with(f"{a.text} ({a['href']})")

    # Mark bold and italic text for RTF formatting
    for tag in soup.find_all(['strong', 'b']):
        tag.replace_with('__BOLD_START__' + (tag.string or '') + '__BOLD_END__')
    for tag in soup.find_all(['em', 'i']):
        tag.replace_with('__ITALIC_START__' + (tag.string or '') + '__ITALIC_END__')
    for tag in soup.find_all(['ins', 'u']):
        tag.replace_with('' + (tag.string or '') + '')

    processed_text = str(soup).strip()

    # Clean up "Mirrored on iNaturalist at" line by removing the URL in parentheses
    processed_text = re.sub(
        r'(Mirrored on iNaturalist at\s+(https://www\.inaturalist\.org/observations/\d+))\s*\(\2\)?',
        r'\1',
        processed_text,
        flags=re.IGNORECASE
    )

    return processed_text

def normalize_string(s):
    return unicodedata.normalize('NFKD', s.strip().lower())

def extract_observation_id(input_string, debug = False):
    # Check if the input is a Mushroom Observer ID (format MO followed by any number of digits)
    mo_match = re.match(r'^MO(\d+)$', input_string)
    if mo_match:
        # Return the Mushroom Observer ID with the MO prefix
        return input_string
    
    # Check if the input is a Mushroom Observer URL - being tolerant of different ways to write it
    # https://mushroomobserver.org/12345
    # http://mushroomobserver.org/observations/12345
    # https://mushroomobserver.org/obs/585855
    # https://www.mushroomobserver.org/obs/585855?foo=bar
    mo_url_match = re.search( r'(?:https?://)?(?:www\.)?mushroomobserver\.org/(?:observations/|observer/show_observation/|obs/)?/?(\d+)(?=[/?#\s]|$)',input_string)

    if mo_url_match:
        # Return the MO ID with the MO prefix
        return f"MO{mo_url_match.group(1)}"
    
    # Check if the input is an iNaturalist URL
    url_match = re.search(r'observations/(\d+)', input_string)
    if url_match:
        return url_match.group(1)

    # Check if the input is a number
    if input_string.isdigit():
        return input_string

    # If neither, return None
    return None

def fetch_api_data(url, retries=6):
    """Fetch data from a URL with robust retries and friendly errors.
    Uses Retry-After header for 429s and exponential backoff with jitter.
    """
    global _concurrency_target
    def _parse_retry_after(resp):
        ra = resp.headers.get('Retry-After')
        if not ra:
            return None
        try:
            return float(ra)
        except ValueError:
            # Try HTTP-date
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(ra)
                return max(0.0, (dt - datetime.datetime.utcnow()).total_seconds())
            except Exception:
                return None

    attempt = 0
    total_wait = 0.0
    notified_patience = False
    max_total_wait = float(_MAX_WAIT_SECONDS)  # seconds
    patience_notice_threshold = 8.0
    while attempt < retries:
        attempt += 1
        try:
            headers = {'Accept': 'application/json', 'User-Agent': 'inat.label.py (label generator)'}
            _rate_limit_wait()
            with _conc_lock:
                global _active_requests
                _active_requests += 1
            try:
                response = get_session().get(url, headers=headers, timeout=20)
            finally:
                with _conc_lock:
                    _active_requests -= 1

            if response.status_code == 200:
                if not response.text.strip():
                    return None, "Empty response"
                try:
                    return response.json(), None
                except ValueError as e:
                    return None, f"Error parsing JSON: {str(e)}"

            if response.status_code == 404:
                return None, "Not found (404)"

            if response.status_code == 429:
                wait = _parse_retry_after(response)
                if wait is None:
                    wait = min(60, 2 ** attempt)  # exponential backoff
                # Jitter to avoid herd retry
                wait *= random.uniform(0.8, 1.2)
                # Clamp to keep total wait under budget
                remaining = max_total_wait - total_wait
                wait = max(0.5, min(wait, remaining))
                # record 429 and possibly reduce concurrency
                now_ts = time.time()
                with _conc_lock:
                    _recent_429.append(now_ts)
                    # Keep only last 10 seconds
                    while _recent_429 and now_ts - _recent_429[0] > 10:
                        _recent_429.popleft()
                    if len(_recent_429) >= 3 and _concurrency_target > 1:
                        old = _concurrency_target
                        _concurrency_target = max(1, _concurrency_target - 1)
                        print_error(f"Reducing concurrency due to repeated 429s: {old} -> {_concurrency_target}")
                # Inform the user (only phrase in red)
                if not _QUIET:
                    msg = (
                        "iNaturalist returned HTTP 429 (" + Fore.RED + "Too Many Requests" + Style.RESET_ALL + "). "
                        "This means we've sent too many requests in a short period. "
                        f"Waiting {wait:.1f}s and retrying (attempt {attempt}/{retries}). "
                        "Tip: lower concurrency with --workers or INAT_MAX_WORKERS, or reduce INAT_RATE_LIMIT_RPM."
                    )
                    print(msg)
                if not notified_patience and (total_wait + wait) >= patience_notice_threshold:
                    print("Note: experiencing API rate limiting; being patient (up to 30s) to avoid skipping labels.")
                    notified_patience = True
                time.sleep(wait)
                total_wait += wait
                continue

            if 500 <= response.status_code < 600:
                wait = min(30, 1.5 ** attempt)
                wait *= random.uniform(0.8, 1.2)
                remaining = max_total_wait - total_wait
                wait = max(0.5, min(wait, remaining))
                print_error(f"Server error {response.status_code}. Retrying in {wait:.1f}s (attempt {attempt}/{retries})")
                if not notified_patience and (total_wait + wait) >= patience_notice_threshold:
                    print("Note: experiencing server delays; being patient (up to 30s) to avoid skipping labels.")
                    notified_patience = True
                time.sleep(wait)
                total_wait += wait
                continue

            return None, f"HTTP error {response.status_code}"

        except (requests.exceptions.Timeout, requests.exceptions.SSLError):
            wait = min(20, 1.5 ** attempt)
            wait *= random.uniform(0.8, 1.2)
            remaining = max_total_wait - total_wait
            wait = max(0.5, min(wait, remaining))
            print_error(f"Timeout/SSL error. Retrying in {wait:.1f}s (attempt {attempt}/{retries})")
            if not notified_patience and (total_wait + wait) >= patience_notice_threshold:
                print("Note: experiencing network delays; being patient (up to 30s) to avoid skipping labels.")
                notified_patience = True
            time.sleep(wait)
            total_wait += wait
            continue
        except requests.exceptions.RequestException:
            wait = min(20, 1.5 ** attempt)
            wait *= random.uniform(0.8, 1.2)
            remaining = max_total_wait - total_wait
            wait = max(0.5, min(wait, remaining))
            print_error(f"Network error. Retrying in {wait:.1f}s (attempt {attempt}/{retries})")
            if not notified_patience and (total_wait + wait) >= patience_notice_threshold:
                print("Note: experiencing network delays; being patient (up to 30s) to avoid skipping labels.")
                notified_patience = True
            time.sleep(wait)
            total_wait += wait
            continue
        except Exception as e:
            return None, f"Unexpected error: {str(e)}"

    return None, "Exceeded maximum retries due to rate limiting or network errors"

def get_taxon_details(taxon_id, retries=6):
    """Fetch detailed information about a taxon, including its ancestors."""
    url = f"https://api.inaturalist.org/v1/taxa/{taxon_id}"
    data, error = fetch_api_data(url, retries)

    if error:
        print_error(f"Error fetching taxon {taxon_id}: {error}")
        return None

    if data and data.get('results'):
        return data['results'][0]
    else:
        return None

def get_mushroom_observer_data(mo_id, retries=6):
    """Fetch observation data from Mushroom Observer API"""
    mo_number = mo_id.replace("MO", "")
    url = f"https://mushroomobserver.org/api2/observations/{mo_number}?detail=high"
    data, error = fetch_api_data(url, retries)

    if error:
        if "Status code: 404" in error:
            print(f"Error: Mushroom Observer observation {mo_id} does not exist.")
        else:
            print(f"Error fetching Mushroom Observer observation {mo_id}: {error}")
        return None, 'Life'

    if data and 'results' in data and data['results']:
        mo_observation = data['results'][0]
        if isinstance(mo_observation, int):
            print(f"Error: Insufficient data from Mushroom Observer API for observation {mo_id}. Skipping.")
            return None, 'Life'
        
        observation = {
            'id': mo_id,
            'ofvs': [],
        }
        
        if 'location' in mo_observation and isinstance(mo_observation['location'], dict):
            observation['place_guess'] = mo_observation['location'].get('name', 'Not available')
        else:
            observation['place_guess'] = 'Not available'
        
        observation['observed_on_string'] = mo_observation.get('date', 'Not available')
        observation['description'] = mo_observation.get('notes', '')
        
        if 'owner' in mo_observation and isinstance(mo_observation['owner'], dict):
            observation['user'] = {
                'name': mo_observation['owner'].get('legal_name', ''),
                'login': mo_observation['owner'].get('login_name', ''),
            }
        else:
            observation['user'] = {
                'name': '',
                'login': '',
            }
        
        if 'location' in mo_observation and isinstance(mo_observation['location'], dict):
            location = mo_observation['location']
            if 'longitude_east' in location and 'longitude_west' in location and 'latitude_north' in location and 'latitude_south' in location:
                longitude = (float(location.get('longitude_east', 0)) + float(location.get('longitude_west', 0))) / 2
                latitude = (float(location.get('latitude_north', 0)) + float(location.get('latitude_south', 0))) / 2
                
                observation['geojson'] = {
                    'coordinates': [longitude, latitude]
                }
        
        if 'consensus' in mo_observation and isinstance(mo_observation['consensus'], dict):
            observation['taxon'] = {
                'name': mo_observation['consensus'].get('name', 'Not available'),
                'preferred_common_name': ''
            }
        
        mo_url = f"https://mushroomobserver.org/obs/{mo_number}"
        observation['ofvs'].append({
            'name': 'Mushroom Observer URL',
            'value': mo_url
        })
        
        if 'herbarium_name' in mo_observation:
            observation['ofvs'].append({
                'name': 'Herbarium Name',
                'value': mo_observation.get('herbarium_name', '')
            })
        
        if 'herbarium_id' in mo_observation:
            observation['ofvs'].append({
                'name': 'Herbarium Catalog Number',
                'value': mo_observation.get('herbarium_id', '')
            })
        
        if 'sequences' in mo_observation and mo_observation['sequences']:
            for sequence in mo_observation['sequences']:
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
                        observation['ofvs'].append({
                            'name': field_name,
                            'value': f"{bp_count} bp"
                        })
        
        return observation, 'Fungi'
    else:
        print(f"Error: Mushroom Observer observation {mo_id} does not exist or has no results.")
        return None, 'Life'

def get_observation_data(observation_id, retries=6):
    # Check if the observation ID is a Mushroom Observer ID
    if isinstance(observation_id, str) and observation_id.startswith("MO"):
        return get_mushroom_observer_data(observation_id, retries)
    
    # Continue with iNaturalist API for regular observation IDs
    url = f"https://api.inaturalist.org/v1/observations/{observation_id}"
    data, error = fetch_api_data(url, retries)

    if error:
        print_error(f"Error fetching observation {observation_id}: {error}")
        return None, 'Life'

    if data and data.get('results'):
        observation = data['results'][0]
        taxon = observation.get('taxon', {})
        iconic_taxon_name = taxon.get('iconic_taxon_name') if taxon else 'Life'
        
        if taxon and 'id' in taxon:
            taxon_id = taxon['id']
            taxon_details = get_taxon_details(taxon_id)
            if taxon_details:
                observation['taxon_details'] = taxon_details
        
        return observation, iconic_taxon_name
    else:
        print(f"Error: Observation {observation_id} does not exist.")
        return None, 'Life'

def field_exists(observation_data, field_name):
    return any(field['name'].lower() == field_name.lower() for field in observation_data.get('ofvs', []))

def get_field_value(observation_data, field_name):
    for field in observation_data.get('ofvs', []):
        if field['name'].lower() == field_name.lower():
            return field['value']
    return None

def format_mushroom_observer_url(url):
    if url:
        match = re.search(r'https?://(?:www\.)?mushroomobserver\.org/(?:observations/|observer/show_observation/|obs/)?(\d+)(?:\?.*)?', url)
        if match:
            return f"https://mushroomobserver.org/obs/{match.group(1)}"
    return url

def get_coordinates(observation_data):
    if 'geojson' in observation_data and observation_data['geojson']:
        coordinates = observation_data['geojson']['coordinates']
        latitude = f"{coordinates[1]:.5f}"
        longitude = f"{coordinates[0]:.5f}"

        # Try to get geoprivacy information
        geoprivacy = observation_data.get('geoprivacy')

        # Check if the observation is obscured
        is_obscured = observation_data.get('obscured', False)

        if is_obscured or geoprivacy == 'obscured':
            accuracy = 20000  # Set accuracy to 20,000 meters
        else:
            accuracy = observation_data.get('positional_accuracy')

        if accuracy:
            # Format accuracy in kilometers if > 1000 meters
            if accuracy > 1000:
                accuracy_km = accuracy / 1000
                accuracy_str = f"{accuracy_km:.1f}km" if accuracy_km != int(accuracy_km) else f"{int(accuracy_km)}km"
            else:
                accuracy_str = f"{int(accuracy)}m"
            return f"{latitude}, {longitude}", accuracy_str
        else:
            return f"{latitude}, {longitude}", None
    return 'Not available', None

def parse_date(date_string):
    date_formats = [
        '%Y-%m-%d',
        '%Y/%m/%d',
        '%B %d, %Y',
    ]

    # First, try to extract just the date part if there's more information
    if not date_string:
        return None
    date_part = str(date_string).split()[0]

    for format in date_formats:
        try:
            parsed_date = datetime.datetime.strptime(date_part, format)
            if parsed_date:
                return parsed_date.date()  # Return only the date part
        except ValueError:
            continue

    # If the above fails, try parsing the full string but only keep the date
    try:
        parsed_date = dateutil_parser.parse(date_string, fuzzy=True)
        if parsed_date:
            return parsed_date.date()  # Return only the date part
    except (ValueError, TypeError):
        pass

def format_scientific_name(observation_data):
    """Format the scientific name based on taxonomic rank."""
    
    # Define rank abbreviations
    rank_abbreviations = {
        'subgenus': 'subg.',
        'section': 'sect.',
        'subsection': 'subsect.',
        'complex': 'complex',
        'subspecies': 'subsp.',
        'variety': 'var.',
        'form': 'f.'
    }
    
    taxon = observation_data.get('taxon', {})
    if not taxon:
        return 'Not available'
    
    # Get the basic scientific name and rank
    scientific_name = taxon.get('name', 'Not available')
    rank = taxon.get('rank', '').lower()
    
    # If it's not in our special ranks list, use the name as is
    if rank not in rank_abbreviations:
        return scientific_name
    
    # For complex, append 'complex' to the name
    if rank == 'complex':
        return f"{scientific_name} complex"
    
    # Special handling for subspecies, variety, and form which follow species name
    if rank in ['subspecies', 'variety', 'form']:
        taxon_details = observation_data.get('taxon_details', {})
        
        # Check if the name already includes the parent species (e.g., "Amanita muscaria flavivolvata")
        name_parts = scientific_name.split()
        
        # If name has more than 2 parts, it might already include the parent species
        if len(name_parts) > 2:
            # Find the species in the ancestors
            species_name = None
            ancestors = taxon_details.get('ancestors', [])
            
            for ancestor in ancestors:
                if ancestor.get('rank') == 'species':
                    species_name = ancestor.get('name')
                    break
            
            # If we found the species and it's in the name, format properly
            if species_name and species_name in scientific_name:
                # Extract the infraspecific epithet (the part after the species name)
                epithet = scientific_name.replace(species_name, '').strip()
                return f"{species_name} {rank_abbreviations[rank]} {epithet}"
            # If the name has three parts but doesn't match our species ancestor,
            # it might be "Genus species epithet" format
            elif len(name_parts) == 3:
                return f"{name_parts[0]} {name_parts[1]} {rank_abbreviations[rank]} {name_parts[2]}"
        
        # If we get here, we need to find parent species from ancestors
        ancestors = taxon_details.get('ancestors', [])
        species_name = None
        
        for ancestor in ancestors:
            if ancestor.get('rank') == 'species':
                species_name = ancestor.get('name')
                break
        
        if species_name:
            # If the scientific_name is just the infraspecific epithet
            if len(name_parts) == 1:
                return f"{species_name} {rank_abbreviations[rank]} {scientific_name}"
            else:
                # If scientific_name already contains full info, just make sure format is correct
                return f"{species_name} {rank_abbreviations[rank]} {name_parts[-1]}"
        else:
            # Fallback: couldn't find parent species
            return scientific_name
    
    # For other ranks below genus (section, subsection, etc.)
    taxon_details = observation_data.get('taxon_details', {})
    ancestors = taxon_details.get('ancestors', [])
    
    # Find the genus in the ancestors
    genus = None
    section = None
    for ancestor in ancestors:
        if ancestor.get('rank') == 'genus':
            genus = ancestor.get('name')
        elif ancestor.get('rank') == 'section':
            section = ancestor.get('name')
    
    # If we couldn't find the genus, use the name as is
    if not genus:
        return scientific_name
    
    # Construct the full scientific name based on rank
    if rank == 'section':
        return f"{genus} {rank_abbreviations[rank]} {scientific_name}"
    elif rank == 'subsection' and section:
        return f"{genus} sect. {section} {rank_abbreviations[rank]} {scientific_name}"
    else:
        return f"{genus} {rank_abbreviations[rank]} {scientific_name}"

def create_inaturalist_label(observation_data, iconic_taxon_name, rtf_mode=False):
    # Check if observation_data is None
    if observation_data is None:
        print("Error: No observation data available to create label.")
        return None, None
        
    obs_number = observation_data['id']
    # Check if this is a Mushroom Observer observation
    if isinstance(obs_number, str) and obs_number.startswith("MO"):
        # Use the Mushroom Observer URL as the main URL for the label
        mo_number = obs_number.replace("MO", "")
        url = f"https://mushroomobserver.org/obs/{mo_number}"
    else:
        url = f"https://www.inaturalist.org/observations/{obs_number}"

    taxon = observation_data.get('taxon', {})
    # Handle cases where there is no name on the observation
    if taxon is None:
        common_name = ''
        scientific_name = 'Not available'
    else:
        common_name = taxon.get('preferred_common_name', taxon.get('name', 'Not available'))
        # Use the new function to format the scientific name correctly
        scientific_name = format_scientific_name(observation_data)

    location = observation_data.get('place_guess') or 'Not available'

    location = location.replace("United States", "USA")
    # Change location endings from ", US" to ", USA"
    if location.endswith(", US"):
        location = location[:-2] + "USA"
    location = re.sub(r'\b\d{5}\b,?\s*', '', location)

    #If the location is long, remove the first part of the location (usually a street address)
    if len(location) > 40:
        comma_index = location.find(',')
        if comma_index != -1:
            location = location[comma_index + 1:].strip()

    # Remove unusual characters if we are in rtf mode - rtf readers don't handle these well
    if rtf_mode:
        location = replace_accents_characters(location)

    coords, accuracy = get_coordinates(observation_data)
    gps_coords = f"{coords} (±{accuracy})" if accuracy else coords  # accuracy now includes unit (m or km)

    date_observed = parse_date(observation_data['observed_on_string'])

    date_observed_str = str(date_observed) if date_observed else 'Not available'

    user = observation_data['user']
    display_name = user.get('name')
    login_name = user['login']
    observer = f"{display_name} ({login_name})" if display_name else login_name

    # Begin generating label
    label = [
        ("Scientific Name", scientific_name)
    ]

    # Check if common name is contained in any part of the scientific name
    # Include common name only if it's not redundant with any part of the scientific name
    scientific_name_parts = scientific_name.lower().split()
    common_name_normalized = normalize_string(common_name) if common_name else ''
    
    # Check if common name matches any part of the scientific name
    is_redundant = False
    if common_name:
        # First check if it matches the full scientific name
        if common_name_normalized == normalize_string(scientific_name):
            is_redundant = True
        else:
            # Check if it matches any part of the scientific name
            for part in scientific_name_parts:
                # Skip rank abbreviations (sect., subsp., etc.)
                if part.endswith('.') or part == 'complex':
                    continue
                    
                if normalize_string(part) == common_name_normalized:
                    is_redundant = True
                    break
    
    # Only add common name if it's not redundant
    if common_name and not is_redundant:
        label.append(("Common Name", common_name))

    # Add these fields to all labels
    if isinstance(obs_number, str) and obs_number.startswith("MO"):
        # For Mushroom Observer data
        mo_number = obs_number.replace("MO", "")
        label.extend([
            ("Mushroom Observer Number", mo_number),
            ("Mushroom Observer URL", url),
            ("Location", location),
            ("GPS Coordinates", gps_coords),
            ("Date Observed", date_observed_str),
            ("Observer", observer)
        ])
    else:
        # For iNaturalist data
        label.extend([
            ("iNaturalist Observation Number", str(obs_number)),
            ("iNaturalist URL", url),
            ("Location", location),
            ("GPS Coordinates", gps_coords),
            ("Date Observed", date_observed_str),
            ("Observer", observer)
        ])

    # Handle DNA Barcode fields consistently for both platforms
    dna_fields = [
        'DNA Barcode ITS',
        'DNA Barcode LSU',
        'DNA Barcode RPB1',
        'DNA Barcode RPB2',
        'DNA Barcode TEF1'
    ]
    for field_name in dna_fields:
        dna_value = get_field_value(observation_data, field_name)
        if dna_value:
            if isinstance(observation_data['id'], str) and observation_data['id'].startswith("MO"):
                # For Mushroom Observer, dna_value is already formatted (e.g., "603 bp")
                label.append((field_name, dna_value))
            else:
                # For iNaturalist, dna_value may contain the full sequence
                cleaned_bases = ''.join(dna_value.split())
                bp_count = len(cleaned_bases)
                if bp_count > 0:  # Only add if there are actual bases
                    label.append((field_name, f"{bp_count} bp"))

    # Include these fields only if they are populated
    genbank_accession = get_field_value(observation_data, 'GenBank Accession Number')
    if not genbank_accession:
        genbank_accession = get_field_value(observation_data, 'GenBank Accession')
    if genbank_accession:
        label.append(("GenBank Accession Number", genbank_accession))

    provisional_name = get_field_value(observation_data, 'Provisional Species Name')
    if provisional_name:
        label.append(("Provisional Species Name", provisional_name))

    species_name_override = get_field_value(observation_data, 'Species Name Override')
    if species_name_override:
        label.append(("Species Name Override", species_name_override))

        # If there is a scientific name override, actually override the scientific name
        label[0] = ("Scientific Name", species_name_override)

    microscopy = get_field_value(observation_data, 'Microscopy Performed')
    if microscopy:
        label.append(("Microscopy Performed", microscopy))

    photography_type = get_field_value(observation_data, 'Mobile or Traditional Photography?')
    if photography_type:
        label.append(("Mobile or Traditional Photography", photography_type))

    herbarium_catalog_number = get_field_value(observation_data, 'Herbarium Catalog Number')
    if herbarium_catalog_number:
        label.append(("Herbarium Catalog Number", herbarium_catalog_number))

    herbarium_secondary_catalog_number = get_field_value(observation_data, 'Herbarium Secondary Catalog Number')
    if herbarium_secondary_catalog_number:
        label.append(("Herbarium Secondary Catalog Number", herbarium_secondary_catalog_number))

    herbarium_name = get_field_value(observation_data, 'Herbarium Name')
    if herbarium_name:
        label.append(("Herbarium Name", herbarium_name))

    mycoportal_id = get_field_value(observation_data, 'Mycoportal ID')
    if mycoportal_id:
        label.append(("Mycoportal ID", mycoportal_id))

    voucher_numbers = get_field_value(observation_data, 'Voucher Number(s)')
    if voucher_numbers:
        label.append(("Voucher Number(s)", voucher_numbers))

    mushroom_observer_url = get_field_value(observation_data, 'Mushroom Observer URL')
    # Avoid duplicating the MO URL if this is a Mushroom Observer observation
    if mushroom_observer_url and not (isinstance(obs_number, str) and obs_number.startswith("MO")):
        # Format Mushroom Observer URL in the best possible way
        formatted_url = format_mushroom_observer_url(mushroom_observer_url)
        label.append(("Mushroom Observer URL", formatted_url))

    notes = observation_data.get('description') or ''
    # Convert HTML in notes field to text
    notes_parsed = parse_html_notes(notes)
    label.append(("Notes", notes_parsed))

    return label, iconic_taxon_name

def create_pdf_content(labels, filename):
    doc = BaseDocTemplate(filename, pagesize=letter, leftMargin=0.25*inch, rightMargin=0.25*inch, topMargin=0.25*inch, bottomMargin=0.25*inch)

    # Two columns
    column_gap = 0.25 * inch
    frame_width = (doc.width - column_gap) / 2
    frame_height = doc.height

    doc.addPageTemplates([
        PageTemplate(id='TwoCol',
                     frames=[
                         Frame(doc.leftMargin, doc.bottomMargin, frame_width, frame_height, id='col1', topPadding=0, bottomPadding=0, leftPadding=0, rightPadding=0),
                         Frame(doc.leftMargin + frame_width + column_gap, doc.bottomMargin, frame_width, frame_height, id='col2', topPadding=0, bottomPadding=0, leftPadding=0, rightPadding=0),
                     ])
    ])

    styles = getSampleStyleSheet()
    custom_normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontName=PDF_BASE_FONT,
        fontSize=12,
        leading=14
    )
    story = []

    for label, iconic_taxon_name in labels:
        pre_notes_content = []
        notes_value = ""
        qr_url = next(
            (value for field, value in label
             if field in ("iNaturalist URL", "Mushroom Observer URL")),
            None)

        scientific_name = ""
        for field, value in label:
            if field == "Scientific Name":
                scientific_name = value
                break
        
        if iconic_taxon_name == "Fungi":
            print(Fore.BLUE + f"Added label for {iconic_taxon_name}" + Style.RESET_ALL + f" {scientific_name}")
        elif iconic_taxon_name == "Plantae":
            print(Fore.GREEN + f"Added label for {iconic_taxon_name}" + Style.RESET_ALL + f" {scientific_name}")
        else:
            print(f"Added label for {iconic_taxon_name} {scientific_name}")

        for field, value in label:
            if field == "Notes":
                notes_value = value
                continue
            if field == "Scientific Name":
                p = Paragraph(f"<b>{field}:</b> <i>{value}</i>", custom_normal_style)
                pre_notes_content.append(p)
            elif field == "iNaturalist URL":
                p = Paragraph(f"{value}", custom_normal_style)
                pre_notes_content.append(p)
            else:
                p = Paragraph(f"<b>{field}:</b> {value}", custom_normal_style)
                pre_notes_content.append(p)

        notes_paragraph = None
        if notes_value:
            notes_text = notes_value.replace('__BOLD_START__', '<b>').replace('__BOLD_END__', '</b>')
            notes_text = notes_text.replace('__ITALIC_START__', '<i>').replace('__ITALIC_END__', '</i>')
            notes_text = notes_text.replace('\n', '<br/>')
            notes_paragraph = Paragraph(f"<b>Notes:</b> {notes_text}", custom_normal_style)

        qr_image = None
        if qr_url:
            qr_hex, _ = generate_qr_code(qr_url)
            if qr_hex:
                qr_img_data = BytesIO(binascii.unhexlify(qr_hex))
                qr_image = ReportLabImage(qr_img_data, width=0.75*inch, height=0.75*inch)

        label_content = pre_notes_content

        # If notes are long, put QR code below, otherwise to the right
        if len(notes_value) > 200 and notes_paragraph:
            label_content.append(notes_paragraph)
            if qr_image:
                qr_image.hAlign = 'RIGHT'
                label_content.append(qr_image)
        elif notes_paragraph:
            if qr_image:
                label_content.append(Spacer(1, 0.1*inch))
                # Set QR image alignment to RIGHT before adding to table
                qr_image.hAlign = 'RIGHT'
                table_data = [[notes_paragraph, qr_image]]
                # Using 1.05*inch instead of 0.85*inch moves QR code 0.2 inches to the left
                # This positions the QR code to align with the rightmost text on the label
                table = Table(table_data, colWidths=['*', 1.05*inch])
                table.setStyle(TableStyle([
                                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                            ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                                            ('TOPPADDING', (0, 0), (-1, -1), 0),
                                            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                                        ]))
                label_content.append(table)
            else:
                label_content.append(notes_paragraph)
        elif qr_image:
            label_content.append(Spacer(1, 0.1*inch))
            qr_image.hAlign = 'RIGHT'
            # Create a table with a single cell to position the QR code
            empty_paragraph = Paragraph("", styles['Normal'])
            table_data = [[empty_paragraph, qr_image]]
            # Using 1.05*inch instead of 0.85*inch moves QR code 0.2 inches to the left
            # This positions the QR code to align with the rightmost text on the label
            table = Table(table_data, colWidths=['*', 1.05*inch])
            table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]))
            label_content.append(table)

        label_content.append(Spacer(1, 0.25*inch))
        
        # Estimate height to prevent layout errors with oversized labels
        height_estimate = len(pre_notes_content) * 14
        if notes_paragraph:
            height_estimate += (notes_value.count('\n') + 1) * 14

        if height_estimate > frame_height:
            story.append(KeepInFrame(frame_width, frame_height, label_content, mode='shrink'))
        else:
            story.append(KeepTogether(label_content))

    doc.build(story)

def create_rtf_content(labels):
    rtf_header = RTF_HEADER
    rtf_footer = r"}"

    rtf_content = rtf_header

    try:
        for label, iconic_taxon_name in labels:
            rtf_content += r"{\keep\pard\ql\keepn\sa0 " # Start of label with keep, default alignment, keepn, and space after 0
            # Get the URL for the QR code - could be either iNaturalist or Mushroom Observer
            qr_url = next(
                (value for field, value in label
                 if field in ("iNaturalist URL", "Mushroom Observer URL")),
                None)
            
            # Track notes length to determine spacing after QR code
            notes_length = 0
            notes_value = next((value for field, value in label if field == "Notes"), "")
            if notes_value:
                notes_length = len(str(notes_value))

            for field, value in label:
                if field == "iNaturalist URL":
                    rtf_content += str(value) + r"\line "
                elif field == "Mushroom Observer URL":
                    # Show raw URL without heading (requested behavior)
                    rtf_content += str(value) + r"\line "
                elif field.startswith("iNat") or field.startswith("iNaturalist") or field.startswith("Mushroom Observer"):
                    # Special formatting for observation headers (except MO URL which is handled above)
                    if field.startswith("Mushroom Observer"):
                        first_chars, rest = field[:2], field[2:]
                        rtf_content += r"{\ul\b " + first_chars + r"}{\scaps\ul\b " + rest + r":} " + str(value) + r"\line "
                    else:
                        first_char, rest = field[0], field[1:]
                        rtf_content += r"{\ul\b " + first_char + r"}{\scaps\ul\b " + rest + r":} " + str(value) + r"\line "
                elif field == "Scientific Name":
                    rtf_content += r"{\scaps\ul\b " + field + r":} {\b\i " + str(value) + r"}\line "
                    # Tell the user which species is being added to the label on stdout.   Fungi in blue, plants in green, everything else in white.
                    if iconic_taxon_name == "Fungi":
                        print(Fore.BLUE + f"Added label for {iconic_taxon_name}" + Style.RESET_ALL + f" {value}")
                    elif iconic_taxon_name == "Plantae":
                        print(Fore.GREEN + f"Added label for {iconic_taxon_name}" + Style.RESET_ALL + f" {value}")
                    else:
                        print(f"Added label for {iconic_taxon_name} {value}")
                elif field == "GPS Coordinates":
                    # Replace the ± symbol with the RTF escape code
                    value_rtf = value.replace("±", r"\'b1")
                    rtf_content += r"{\scaps\ul\b " + field + r":} " + value_rtf + r"\line "
                elif field == "Notes":
                    if value:
                        # Remove blank lines (lines with only whitespace) from notes
                        lines = str(value).split('\n')
                        non_blank_lines = [line for line in lines if line.strip()]
                        value = '\n'.join(non_blank_lines)
                        
                        rtf_content += r"{\scaps\ul\b " + field + r":} "
                        value = escape_rtf(value)
                        value_rtf = str(value)
                        # Replace newlines with RTF line breaks
                        value_rtf = value_rtf.replace('\n', r'\line ')
                        # Handle bold and italics text properly
                        value_rtf = value_rtf.replace('__BOLD_START__', r'{\b ').replace('__BOLD_END__', r'}')
                        value_rtf = value_rtf.replace('__ITALIC_START__', r'{\i ').replace('__ITALIC_END__', r'}')
                        # Remove the line about the MO to iNat import, as this isn't important on a label since we already include the MO URL
                        value_rtf = re.sub(r'\\line Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value_rtf)
                        # Remove the line about the inat to MO import, as this isn't important on a label since we already include the MO URL (added by MO on import)
                        value_rtf = re.sub(r'((\\line)\s+\2+\s+\2 Imported|Imported) by Mushroom Observer \d{4}-\d{2}-\d{2}', '', value_rtf)
                        rtf_content += value_rtf  # No trailing \line after notes
                else:
                    rtf_content += r"{\scaps\ul\b " + field + r":} " + str(value) + r"\line "

            def split_hex_string(s, n):
                # Split hex string into lines of n characters
                return '\n'.join([s[i:i+n] for i in range(0, len(s), n)])

            # Add the QR code to the label
            qr_hex, qr_size = generate_qr_code(qr_url) if qr_url else (None, None)

            if qr_hex:
                # If there are no notes, remove a trailing \line to avoid an extra blank line before the QR code
                if notes_length == 0 and rtf_content.endswith(r"\line "):
                    rtf_content = rtf_content[:-6]
                rtf_content += r"\par\pard\qr\ri360\sb57\sa0 " # Close paragraph, start new right-aligned one with minimal spacing (~1mm)
                # Convert pixel dimensions to twips (1 pixel = 15 twips)
                qr_width_twips = qr_size[0] * 15
                qr_height_twips = qr_size[1] * 15

                # Embed the base64-encoded QR code image in RTF
                rtf_content += r'{\pict\pngblip\picw'
                rtf_content += str(qr_width_twips)
                rtf_content += r'\pich'
                rtf_content += str(qr_height_twips)
                rtf_content += r'\picwgoal'
                rtf_content += str(qr_width_twips)
                rtf_content += r'\pichgoal'
                rtf_content += str(qr_height_twips)
                rtf_content += r' '

                # Split the base64 string into chunks of 76 characters (standard for RTF)
                hex_chunks = split_hex_string(qr_hex, 76)
                rtf_content += hex_chunks
                rtf_content += r'}'
                # Add extra carriage return only if notes are 200 characters or less
                if notes_length <= 200:
                    rtf_content += r"\par\par" # End QR code paragraph with extra carriage return
                else:
                    rtf_content += r"\par" # End QR code paragraph with single carriage return for long notes
            else:
                print("Failed to generate QR code.")

            rtf_content += r"}" # Close the label group started at line 973
            rtf_content += r"\par " # Additional vertical space between labels

        rtf_content += rtf_footer
    except Exception as e:
        print(f"Error in create_rtf_content: {e}")
        return rtf_header + r"Error generating content" + rtf_footer

    return rtf_content

# Check to see if the observation is in California
def is_within_california(latitude, longitude):
    # Approximate bounding box for California
    CA_NORTH = 42.0
    CA_SOUTH = 32.5
    CA_WEST = -124.4
    CA_EAST = -114.1

    return (CA_SOUTH <= latitude <= CA_NORTH) and (CA_WEST <= longitude <= CA_EAST)

def main():
    parser = argparse.ArgumentParser(description="Create herbarium labels from iNaturalist observation numbers or URLs")
    parser.add_argument("observation_ids", nargs="*", help="Observation number(s) or URL(s)")
    parser.add_argument("--file", metavar="filename", help="File containing observation numbers or URLs (separated by spaces, commas, or newlines)")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--rtf", metavar="filename.rtf", help="Output to RTF file (filename must end with .rtf)")
    output_group.add_argument("--pdf", metavar="filename.pdf", help="Output to PDF file (filename must end with .pdf)")
    parser.add_argument("--find-ca", action="store_true", help="Find observations within California")
    parser.add_argument("--workers", type=int, default=None, help="Max parallel API requests (default 5, or INAT_MAX_WORKERS env)")
    parser.add_argument("--max-wait-seconds", type=float, default=None, help="Max total wait per API call when retrying (default 30s, or INAT_MAX_WAIT_SECONDS env)")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed retry messages (e.g., 429 lines); still shows patience notes and summary")
    parser.add_argument('--debug', action='store_true', help='Print debug output')

    args = parser.parse_args()

    # If no arguments are provided, show help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    # Define rtf_mode and pdf_mode based on whether --rtf or --pdf argument is provided
    rtf_mode = bool(args.rtf)
    pdf_mode = bool(args.pdf)

    # Apply global controls from CLI
    global _MAX_WAIT_SECONDS, _QUIET
    if args.max_wait_seconds is not None:
        _MAX_WAIT_SECONDS = float(args.max_wait_seconds)
    if args.quiet:
        _QUIET = True

    if rtf_mode and not args.rtf.lower().endswith('.rtf'):
        parser.error("argument --rtf: filename must end with .rtf")

    if pdf_mode and not args.pdf.lower().endswith('.pdf'):
        parser.error("argument --pdf: filename must end with .pdf")

    observation_ids = args.observation_ids or []

    # Read observation IDs from file if --file is provided
    if args.file:
        try:
            with open(args.file, 'r') as file:
                file_contents = file.read()
                # Split file contents by whitespace, commas, or newlines
                file_observation_ids = re.split(r'[,\s]+', file_contents.strip())
                observation_ids.extend(file_observation_ids)
        except Exception as e:
            print(f"Error reading file {args.file}: {e}")
            sys.exit(1)

    # Remove empty entries
    observation_ids = [obs for obs in observation_ids if obs]

    labels = []
    failed = []
    total_requested = len(observation_ids)
    start_time = time.time()

    def process_one(input_value):
        try:
            observation_id = extract_observation_id(input_value, debug=args.debug)
            if observation_id is None:
                return ('err', f"Invalid input '{input_value}'")
            result = get_observation_data(observation_id)
            if result is None:
                return ('err', f"Failed to fetch observation {observation_id}")
            observation_data, iconic_taxon_name = result
            if args.find_ca:
                geo = observation_data.get('geojson')
                if geo and geo.get('coordinates'):
                    coordinates = geo['coordinates']
                    latitude, longitude = coordinates[1], coordinates[0]
                    if is_within_california(latitude, longitude):
                        print(f"https://www.inaturalist.org/observations/{observation_id}")
                return ('skip', None)
            else:
                label, updated_iconic_taxon = create_inaturalist_label(
                    observation_data, iconic_taxon_name, rtf_mode=rtf_mode
                )
                if label is not None:
                    return ('ok', (label, updated_iconic_taxon))
                return ('err', f"Could not create label for {observation_id}")
        except Exception as e:
            return ('err', f"Unexpected error for {input_value}: {str(e)}")

    # Respect API guidelines by limiting concurrency to a small number (<=5)
    max_workers = args.workers if args.workers else int(os.environ.get('INAT_MAX_WORKERS', '5'))
    # Initialize dynamic concurrency target to selected workers
    global _concurrency_target
    _concurrency_target = max_workers
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_one, input_value) for input_value in observation_ids]
        for fut in as_completed(futures):
            status, payload = fut.result()
            if status == 'ok' and payload:
                labels.append(payload)
            elif status == 'err' and payload:
                failed.append(payload)

    if not args.find_ca:
        if labels:
            if rtf_mode:
                rtf_content = create_rtf_content(labels)
                with open(args.rtf, 'w') as rtf_file:
                    rtf_file.write(rtf_content)
                try:
                    size_bytes = os.path.getsize(args.rtf)
                    size_kb = (size_bytes + 1023) // 1024
                except Exception:
                    size_kb = None
                basename = os.path.basename(args.rtf)
                if size_kb is not None:
                    print(f"RTF file created: {basename} ({size_kb} kb)")
                else:
                    print(f"RTF file created: {basename}")
            elif pdf_mode:
                create_pdf_content(labels, args.pdf)
                try:
                    size_bytes = os.path.getsize(args.pdf)
                    size_kb = (size_bytes + 1023) // 1024
                except Exception:
                    size_kb = None
                basename = os.path.basename(args.pdf)
                if size_kb is not None:
                    print(f"PDF file created: {basename} ({size_kb} kb)")
                else:
                    print(f"PDF file created: {basename}")
            else:
                # Print labels to stdout
                for label, _ in labels:
                    for field, value in label:
                        if field == "Notes":
                            value = remove_formatting_tags(value)
                            value = re.sub(r'Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value)
                            value = re.sub(r'Imported by Mushroom Observer \d{4}-\d{2}-\d{2}', '', value)
                            print(f"{field}: {value}")
                        elif field == "iNaturalist URL":
                            print(value)
                        elif field == "Mushroom Observer URL":
                            print(value)
                        else:
                            print(f"{field}: {value}")
                    print("\n")  # Blank line between labels
        else:
            print("No valid observations found.")

        # Print summary last so it appears at the very end
        elapsed = time.time() - start_time
        failed_count_text = (Fore.RED + str(len(failed)) + Style.RESET_ALL) if failed else str(len(failed))
        generated_word = "generated"
        if total_requested != len(labels):
            generated_word = Fore.RED + "generated" + Style.RESET_ALL
        print(f"Summary: requested {total_requested}, {generated_word} {len(labels)}, failed {failed_count_text}, time {elapsed:.2f}s")
        if failed:
            for msg in failed:
                print_error(f" - {msg}")

if __name__ == "__main__":
    colorama.init()
    main()
