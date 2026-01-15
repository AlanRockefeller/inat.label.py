#!/usr/bin/env python3

"""
iNaturalist and Mushroom Observer Herbarium Label Generator

Author: Alan Rockefeller
Date: January 14, 2026
Version: 3.9

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
  coordinates, observation date, observer, and more
- Handles special fields like DNA Barcode ITS (and LSU, TEF1, RPB1, RPB2), GenBank Accession Number,
  Provisional Species Name, Mobile or Traditional Photography?, Microscopy Performed, Herbarium Catalog Number,
  Herbarium Name, Mycoportal ID, Voucher number(s)
- Generates a QR code which links to the observation URL
- Can create fungus fair labels from a CSV with --fungusfair

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

- Generate fungus fair labels from a CSV file:
  ./inat_label.py --fungusfair labels.csv --pdf out.pdf

- Generate labels with custom fields - in this case without Coordinates but with Fungusworld number
  ./inat.label.py 183905751 147249599 --custom "+Fungusworld, -Coordinates" --pdf out.pdf

Notes:
- The RTF output is formatted to closely match the style of traditional herbarium labels.
- It is recommended to print herbarium labels on 100% cotton cardstock with an inkjet printer for maximum longevity.
- In fungus fair mode (--fungusfair), CSV files provided as arguments will be parsed for batch label generation.

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
import html
import os
import re
import sys
import time
import shutil
import csv
import unicodedata
import random
from io import BytesIO
import subprocess
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
import threading
from collections import deque
import binascii
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Image as ReportLabImage, KeepTogether, Table, TableStyle, KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError



PDF_BASE_FONT = os.environ.get('PDF_BASE_FONT', 'Times-Roman')
_fonts_registered = False
_fonts_lock = threading.Lock()

# Global session with connection pooling
_thread_local = threading.local()

# Simple thread-safe rate limiter: max 60 requests per 60 seconds (configurable via env)
RATE_LIMIT_RPM = int(os.environ.get("INAT_RATE_LIMIT_RPM", "60"))
_rate_lock = threading.Lock()
_request_times = deque()

# Even spacing control derived from RATE_LIMIT_RPM
_MIN_INTERVAL = 0.0
if RATE_LIMIT_RPM > 0:
    _MIN_INTERVAL = 60.0 / RATE_LIMIT_RPM
_next_allowed_time = 0.0
# Begin smoothing only after this many recent requests in the window (burst allowance for small jobs)
_SMOOTH_THRESHOLD = int(os.environ.get('INAT_SMOOTH_THRESHOLD', str(max(1, RATE_LIMIT_RPM // 4))))

# Concurrency semaphore
_request_semaphore = threading.BoundedSemaphore(int(os.environ.get('INAT_MAX_WORKERS', '5')))

# Retry/quiet controls (tunable from CLI or env)
_MAX_WAIT_SECONDS = float(os.environ.get('INAT_MAX_WAIT_SECONDS', '30'))
_QUIET = bool(int(os.environ.get('INAT_QUIET', '0')))

def _rate_limit_wait():
    """Respect RPM window with smoothing only after a small burst threshold.

    - Allows a small initial burst (< _SMOOTH_THRESHOLD in the last 60s) with no artificial spacing
    - Applies even spacing once activity is high enough to approach the RPM limit
    - Always enforces the absolute window cap (RATE_LIMIT_RPM per 60 seconds)
    """
    global _next_allowed_time
    if RATE_LIMIT_RPM <= 0:
        return
    
    window = 60.0
    min_interval = _MIN_INTERVAL
    
    with _rate_lock:
        now = time.monotonic()
        
        # 1. Clean up old requests from the sliding window
        while _request_times and now - _request_times[0] > window:
            _request_times.popleft()
            
        # 2. Check Hard Cap
        if len(_request_times) >= RATE_LIMIT_RPM:
            # We hit the hard limit. Must wait until the oldest request expires.
            wait_for_cap = (_request_times[0] + window) - now
        else:
            wait_for_cap = 0.0
            
        # 3. Check Smoothing / Burst
        # Ensure schedule catches up to now if we were idle
        if _next_allowed_time < now:
            _next_allowed_time = now
            
        if len(_request_times) < _SMOOTH_THRESHOLD:
            # Burst mode: minimal wait (just the hard cap wait, if any)
            smoothing_target = now
            # Do not increment _next_allowed_time to create debt; just keep it current.
            # This allows the *next* request to also burst or start smoothing from now.
        else:
            # Smoothing mode: respect the schedule
            smoothing_target = _next_allowed_time
        
        # 4. Calculate final wait
        final_time = max(now + wait_for_cap, smoothing_target)
        wait = final_time - now
        
        # 5. Update State
        # If we are bursting, we don't space out the *next* allowed time (it stays at final_time).
        # If we are smoothing, we push the *next* allowed time out by min_interval.
        if len(_request_times) < _SMOOTH_THRESHOLD:
             _next_allowed_time = final_time
        else:
             _next_allowed_time = final_time + min_interval
        
        # Record the execution time for the Hard Cap window
        _request_times.append(final_time)
        
    # 6. Sleep if needed
    if wait > 0:
        time.sleep(wait)

def get_session():
    """Get or create a requests session with connection pooling."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
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
        _thread_local.session.mount("http://", adapter)
        _thread_local.session.mount("https://", adapter)
    return _thread_local.session

def print_error(message):
    """Print an error message in red (cross-platform) to stderr.

    Uses colorama for coloring when available; falls back to ANSI escape codes.
    """
    try:
        print(Fore.RED + str(message) + Style.RESET_ALL, file=sys.stderr)
    except Exception:
        # Fallback if colorama not available for some reason
        print(f"\033[91m{message}\033[0m", file=sys.stderr)


def register_fonts():
    """Register both a preferred font and a system Unicode font to be used conditionally."""
    global PDF_BASE_FONT, _fonts_registered

    with _fonts_lock:
        if _fonts_registered:
            return

        # Set the default preferred font.
        PDF_BASE_FONT = 'Liberation Serif'

        # Attempt to register the preferred font family.
        try:
            pdfmetrics.registerFont(TTFont('Liberation Serif', 'LiberationSerif-Regular.ttf'))
            pdfmetrics.registerFont(TTFont('Liberation Serif-Bold', 'LiberationSerif-Bold.ttf'))
            pdfmetrics.registerFont(TTFont('Liberation Serif-Italic', 'LiberationSerif-Italic.ttf'))
            pdfmetrics.registerFont(TTFont('Liberation Serif-BoldItalic', 'LiberationSerif-BoldItalic.ttf'))
            pdfmetrics.registerFontFamily('Liberation Serif', normal='Liberation Serif', bold='Liberation Serif-Bold', italic='Liberation Serif-Italic', boldItalic='Liberation Serif-BoldItalic')
        except (OSError, ValueError, TTFError) as e:
            print_error(f"Warning: Preferred font 'Liberation Serif' not found or invalid: {e}. PDF output may use a fallback.")
            PDF_BASE_FONT = 'Times-Roman' # A core PDF font

        # Attempt to find and register a system Unicode font for special characters.
        if shutil.which('fc-match'):
            try:
                styles = {
                    'normal': 'sans-serif:lang=vi', 'bold': 'sans-serif:lang=vi:weight=bold',
                    'italic': 'sans-serif:lang=vi:slant=italic', 'boldItalic': 'sans-serif:lang=vi:weight=bold:slant=italic'
                }
                font_paths = {}
                all_found = True
                for style, query in styles.items():
                    command = ['fc-match', query, '-f', '%{file}']
                    process = subprocess.run(command, capture_output=True, text=True, check=True)
                    path = process.stdout.strip()
                    if path:
                        font_paths[style] = path
                    else:
                        print_error(f"Warning: fc-match found no path for font style '{style}' with query '{query}'.")
                        all_found = False
                        break
                
                if all_found:
                    family_name = 'SystemUnicodeFont'
                    pdfmetrics.registerFont(TTFont(family_name, font_paths['normal']))
                    pdfmetrics.registerFont(TTFont(f"{family_name}-Bold", font_paths['bold']))
                    pdfmetrics.registerFont(TTFont(f"{family_name}-Italic", font_paths['italic']))
                    pdfmetrics.registerFont(TTFont(f"{family_name}-BoldItalic", font_paths['boldItalic']))
                    pdfmetrics.registerFontFamily(family_name, normal=family_name, bold=f"{family_name}-Bold", italic=f"{family_name}-Italic", boldItalic=f"{family_name}-BoldItalic")
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                print_error(f"Warning: Could not find or register a system Unicode font: {e}. Special characters in PDF may not render correctly.")
        
        _fonts_registered = True

MINILABEL_RTF_HEADER = r"""{\rtf1\ansi\uc1\deff3\adeflang1025
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
\formshade\paperh15840\paperw12240\margl360\margr360\margt360\margb360\sectd\sbknone\sectunlocked1\pgndec\pgwsxn12240\pghsxn15840\marglsxn360\margrsxn360\margtsxn360\margbsxn360\ftnbj\ftnstart1\ftnrstcont\ftnnar\aenddoc\aftnrstcont\aftnstart1\aftnnrlc
\pard\plain \s0\ql\tx113
"""

RTF_HEADER = r"""{\rtf1\ansi\uc1\deff3\adeflang1025
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



def generate_qr_code(url, minilabel_mode=False):
    """Generate a small PNG QR code for the given URL and return (hex_string, size_tuple)."""
    try:
        if minilabel_mode:
            qr = qrcode.QRCode(version=1, box_size=1, border=1)
        else:
            qr = qrcode.QRCode(version=1, box_size=2, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()
        img_hex = binascii.hexlify(img_bytes).decode('utf-8')
        return img_hex, img.size  # Return the hex string and the size of the image
    except Exception as e:
        print(f"Error generating QR code: {e}")
        return None, None

def escape_rtf(text):
    """Escape special characters for RTF output. This function handles standard RTF control
    characters and encodes any non-ASCII characters using RTF's \\uXXXX notation."""
    if not text:
        return ""
    text = str(text)

    # First, escape RTF control characters and handle newlines.
    text = text.replace('\\', '\\\\')
    text = text.replace('{', '\\{')
    text = text.replace('}', '\\}')
    text = text.replace('\n', '\\line ')

    # Create a new string with non-ASCII characters properly escaped.
    res = []
    for char in text:
        codepoint = ord(char)
        if codepoint < 128:
            res.append(char)
        else:
            # For non-ASCII characters, use the \uXXXX escape sequence.
            # RTF uses a signed 16-bit integer for the Unicode value.
            # A '?' is appended as a fallback for older RTF readers.
            if codepoint > 32767:
                codepoint -= 65536
            res.append(f'\\u{codepoint}?')
    return "".join(res)

# Remove formatting tags in stdout
def remove_formatting_tags(text):
    """Strip internal formatting markers and prune empty/garbage lines for plaintext output."""
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


def rl_safe(text):
    """Escape text for ReportLab Paragraphs and restore internal formatting markers."""
    if not text:
        return ""
    return html.escape(str(text)).replace('__ITALIC_START__','<i>').replace('__ITALIC_END__','</i>').replace('__BOLD_START__','<b>').replace('__BOLD_END__','</b>')


def parse_html_notes(notes):
    """Convert HTML notes into simplified text with inline formatting markers.

    - Unwraps paragraph tags
    - Converts <a> to "text (url)"
    - Replaces bold/italic with placeholder tokens understood by downstream RTF/Paragraph rendering
    - Cleans specific duplicated cross-posting lines
    """
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
        tag.replace_with('__BOLD_START__' + tag.get_text() + '__BOLD_END__')
    for tag in soup.find_all(['em', 'i']):
        tag.replace_with('__ITALIC_START__' + tag.get_text() + '__ITALIC_END__')
    for tag in soup.find_all(['ins', 'u']):
        tag.replace_with(tag.get_text())

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
    """Lowercase, strip whitespace, and apply NFKD Unicode normalization for comparisons."""
    return unicodedata.normalize('NFKD', s.strip().lower())

def extract_observation_id(input_string, debug = False):
    """Normalize a user-supplied input into an observation identifier.

    Accepts iNaturalist numeric IDs or URLs, and Mushroom Observer IDs like "MO12345" or MO URLs.
    Returns a string ID (possibly with "MO" prefix) or None if unrecognized.
    """
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
    def _parse_retry_after(resp):
        """Parse HTTP Retry-After header as seconds, supporting both delta and HTTP-date."""
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
            with _request_semaphore:
                response = get_session().get(url, headers=headers, timeout=20)

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
                
                # Inform the user (only phrase in red)
                if not _QUIET:
                    msg = (
                        "iNaturalist returned HTTP 429 (" + Fore.RED + "Too Many Requests" + Style.RESET_ALL + "). "
                        "This means we've sent too many requests in a short period. "
                        f"Waiting {wait:.1f}s and retrying (attempt {attempt}/{retries}). "
                        "Tip: lower concurrency with --workers or INAT_MAX_WORKERS, or reduce INAT_RATE_LIMIT_RPM."
                    )
                    print(msg, flush=True)
                if not notified_patience and (total_wait + wait) >= patience_notice_threshold:
                    print("Note: experiencing API rate limiting; being patient (up to 30s) to avoid skipping labels.", flush=True)
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
                    print("Note: experiencing server delays; being patient (up to 30s) to avoid skipping labels.", flush=True)
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
                print("Note: experiencing network delays; being patient (up to 30s) to avoid skipping labels.", flush=True)
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
                print("Note: experiencing network delays; being patient (up to 30s) to avoid skipping labels.", flush=True)
            time.sleep(wait)
            total_wait += wait
            continue
        except Exception as e:
            return None, f"Unexpected error: {str(e)}"

    return None, "Exceeded maximum retries due to rate limiting or network errors"

# Batched taxon-details cache and fetcher
_taxon_cache = {}
_taxon_cache_lock = threading.Lock()
_taxon_pending = {}
_taxon_pending_lock = threading.Lock()
_taxon_batch_queue = deque()
_taxon_batcher_thread = None
_taxon_batcher_stop = threading.Event()
_TAXON_BATCH_MAX = int(os.environ.get('INAT_TAXON_BATCH_MAX', '50'))
_TAXON_BATCH_WINDOW = float(os.environ.get('INAT_TAXON_BATCH_WINDOW', '0.1'))


def _start_taxon_batcher():
    """Start the background batcher thread for /taxa lookups if not already running."""
    global _taxon_batcher_thread
    if _taxon_batcher_thread and _taxon_batcher_thread.is_alive():
        return

    def _loop():
        """Background loop that batches pending taxon IDs and fetches them via a single /taxa call."""
        while not _taxon_batcher_stop.is_set():
            ids = []
            start_wait = time.time()
            # Collect a batch up to max size or window
            while len(ids) < _TAXON_BATCH_MAX:
                with _taxon_pending_lock:
                    if _taxon_batch_queue:
                        tid = _taxon_batch_queue.popleft()
                        if tid not in ids:
                            ids.append(tid)
                    else:
                        # No work pending right now
                        pass
                if ids:
                    if time.time() - start_wait >= _TAXON_BATCH_WINDOW:
                        break
                    # Small sleep to allow more IDs to accumulate
                    time.sleep(0.01)
                else:
                    # Avoid tight loop when idle
                    time.sleep(0.01)
                    if not _taxon_batch_queue:
                        # still nothing; continue outer while
                        continue
            if not ids:
                # nothing to do this round
                continue
            # Perform a single batched request
            url = "https://api.inaturalist.org/v1/taxa?id=" + ",".join(str(i) for i in ids)
            data, error = fetch_api_data(url)
            results_by_id = {}
            if not error and data and data.get('results') is not None:
                for item in data['results']:
                    tid = item.get('id')
                    if tid is not None:
                        results_by_id[tid] = item
            # Fulfill waiters and populate cache
            with _taxon_pending_lock:
                # Also lock the cache when updating
                with _taxon_cache_lock:
                    for tid in ids:
                        _taxon_cache[tid] = results_by_id.get(tid)
                        evt = _taxon_pending.get(tid)
                        if evt:
                            evt.set()
                            _taxon_pending.pop(tid, None)

    _taxon_batcher_thread = threading.Thread(target=_loop, name="inat-taxa-batcher", daemon=True)
    _taxon_batcher_thread.start()


def get_taxon_details(taxon_id, retries=6):
    """Fetch taxon details (including ancestors) with caching.

    Uses the /v1/taxa/{id} endpoint to ensure 'ancestors' are present. Caches results in-memory.
    """
    try:
        tid = int(taxon_id)
    except Exception:
        return None

    # Cache fast-path; ensure we have ancestors
    with _taxon_cache_lock:
        cached = _taxon_cache.get(tid)
    if cached and cached.get('ancestors') is not None:
        return cached

    # Fetch directly (ensures ancestors are included)
    url = f"https://api.inaturalist.org/v1/taxa/{tid}"
    data, error = fetch_api_data(url, retries)
    if error:
        print_error(f"Error fetching taxon {tid}: {error}")
        return cached  # Return whatever we had (may be None or partial)

    if data and data.get('results'):
        item = data['results'][0]
        with _taxon_cache_lock:
            _taxon_cache[tid] = item
        return item
    return cached

def get_mushroom_observer_data(mo_id, retries=6):
    """Fetch observation data from Mushroom Observer API."""
    mo_number = mo_id.replace("MO", "")
    url = f"https://mushroomobserver.org/api2/observations/{mo_number}?detail=high"
    data, error = fetch_api_data(url, retries)

    if error:
        if "Status code: 404" in error:
            print(f"Error: Mushroom Observer observation {mo_id} does not exist.", flush=True)
        else:
            print(f"Error fetching Mushroom Observer observation {mo_id}: {error}", flush=True)
        return None, 'Life'

    if data and 'results' in data and data['results']:
        mo_observation = data['results'][0]
        if isinstance(mo_observation, int):
            print(f"Error: Insufficient data from Mushroom Observer API for observation {mo_id}. Skipping.", flush=True)
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
    """Fetch observation data from iNaturalist or Mushroom Observer.

    Accepts either an iNaturalist observation ID (int/str) or an MO ID like "MO12345".
    Returns a tuple (observation_dict, iconic_taxon_name) or (None, 'Life') on error.
    May augment iNat observations with taxon_details (ancestors) when needed for formatting.
    """
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
            rank = str(taxon.get('rank', '')).lower()
            # Only fetch ancestors when formatting needs them
            if rank in {'subspecies', 'variety', 'form', 'section', 'subsection', 'subgenus'}:
                taxon_details = get_taxon_details(taxon_id)
                if taxon_details:
                    observation['taxon_details'] = taxon_details
        
        return observation, iconic_taxon_name
    else:
        print(f"Error: Observation {observation_id} does not exist.", flush=True)
        return None, 'Life'

def field_exists(observation_data, field_name):
    """Return True if an observation has a custom field with the given name."""
    return any(field['name'].lower() == field_name.lower() for field in observation_data.get('ofvs', []))

def get_field_value(observation_data, field_name):
    """Return the value for a custom field by name, or None if absent."""
    for field in observation_data.get('ofvs', []):
        if field['name'].lower() == field_name.lower():
            return field['value']
    return None

def format_mushroom_observer_url(url):
    """Normalize Mushroom Observer URLs to https://mushroomobserver.org/obs/<id> when possible."""
    if url:
        match = re.search(r'https?://(?:www\.)?mushroomobserver\.org/(?:observations/|observer/show_observation/|obs/)?(\d+)(?:\?.*)?', url)
        if match:
            return f"https://mushroomobserver.org/obs/{match.group(1)}"
    return url

def get_coordinates(observation_data):
    """Return ("lat, lon", accuracy_str) from observation geojson, if present.

    - Formats lat/lon to 5 decimals. Accuracy is "Xm" or "Ykm" when available.
    - For obscured observations, defaults accuracy to 20000m.
    - Returns ("Not available", None) when no coordinates are present.
    """
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
    """Parse a variety of date strings and return a datetime.date or None.

    Only the calendar date is kept; any time-of-day or timezone (e.g. PST)
    is ignored before parsing.
    """
    if not date_string:
        return None

    s = str(date_string).strip()

    # Try to extract just the date portion in common patterns, dropping time/TZ.
    # Examples we handle explicitly:
    #   2025-11-14 03:25 PM PST   -> 2025-11-14
    #   2025/11/14 03:25 PM PST   -> 2025/11/14
    #   November 14, 2025 3:25 PM PST -> November 14, 2025
    iso_match = re.search(r'\d{4}-\d{2}-\d{2}', s)
    slash_match = re.search(r'\d{4}/\d{1,2}/\d{1,2}', s)
    long_match = re.search(r'[A-Za-z]+ \d{1,2}, \d{4}', s)

    if iso_match:
        s_clean = iso_match.group(0)
    elif slash_match:
        s_clean = slash_match.group(0)
    elif long_match:
        s_clean = long_match.group(0)
    else:
        # Fall back to the original string if we can't detect a date substring
        s_clean = s

    date_formats = [
        '%Y-%m-%d',
        '%Y/%m/%d',
        '%B %d, %Y',
    ]

    # First, try our explicit formats on the cleaned date-only string
    for fmt in date_formats:
        try:
            parsed_date = datetime.datetime.strptime(s_clean, fmt)
            return parsed_date.date()
        except ValueError:
            continue

    # If that fails, let dateutil try, but on the cleaned string that no longer
    # contains time-of-day or timezone tokens like "PST".
    try:
        parsed_date = dateutil_parser.parse(s_clean, fuzzy=True)
        if parsed_date:
            return parsed_date.date()
    except (ValueError, TypeError):
        return None

def format_scientific_name(observation_data):
    """Format the scientific name based on taxonomic rank.

    - Species-level and above: return the taxon's canonical name.
    - Infraspecific (subspecies/variety/form): "<Genus species> <rank> <epithet>".
    - Infrageneric (subgenus/section/subsection): "<Genus> <rank> <name>" using full rank words.
    """
    
    # Define rank label map; abbreviate infrageneric ranks; rank labels themselves are not italicized.
    rank_label = {
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
    rank = str(taxon.get('rank', '')).lower()
    
    # If it's not in our special ranks list, use the name as is
    if rank not in rank_label:
        # Entire binomial or uninomial italicized in display contexts; mark for styling.
        return f"__ITALIC_START__{scientific_name}__ITALIC_END__"
    
    # For complex, append 'complex' to the name
    if rank == 'complex':
        # Complex label not italicized; italicize the uninomial name only
        return f"__ITALIC_START__{scientific_name}__ITALIC_END__ complex"
    
    # Special handling for subspecies, variety, and form which follow species name
    if rank in ['subspecies', 'variety', 'form']:
        taxon_details = observation_data.get('taxon_details')
        # Lazy fetch if needed
        if not taxon_details and taxon.get('id'):
            fetched = get_taxon_details(taxon['id'])
            if fetched:
                taxon_details = fetched
                observation_data['taxon_details'] = fetched
        
        # Check if the name already includes the parent species (e.g., "Amanita muscaria flavivolvata")
        name_parts = scientific_name.split()
        
        # If name has more than 2 parts, it might already include the parent species
        if len(name_parts) > 2 and taxon_details:
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
                return f"__ITALIC_START__{species_name}__ITALIC_END__ {rank_label[rank]} __ITALIC_START__{epithet}__ITALIC_END__"
            # If the name has three parts but doesn't match our species ancestor,
            # it might be "Genus species epithet" format
            elif len(name_parts) == 3:
                return f"__ITALIC_START__{name_parts[0]} {name_parts[1]}__ITALIC_END__ {rank_label[rank]} __ITALIC_START__{name_parts[2]}__ITALIC_END__"
        
        # If we get here, we need to find parent species from ancestors
        species_name = None
        ancestors = (taxon_details or {}).get('ancestors', [])
        for ancestor in ancestors:
            if ancestor.get('rank') == 'species':
                species_name = ancestor.get('name')
                break
        
        if species_name:
            # If the scientific_name is just the infraspecific epithet
            if len(name_parts) == 1:
                return f"__ITALIC_START__{species_name}__ITALIC_END__ {rank_label[rank]} __ITALIC_START__{scientific_name}__ITALIC_END__"
            else:
                # If scientific_name already contains full info, just make sure format is correct
                return f"__ITALIC_START__{species_name}__ITALIC_END__ {rank_label[rank]} __ITALIC_START__{name_parts[-1]}__ITALIC_END__"
        else:
            # Fallback: couldn't find parent species
            return scientific_name
    
    # Infrageneric ranks (below genus): subgenus, section, subsection
    # We need the genus; fetch ancestors if not present
    taxon_details = observation_data.get('taxon_details')
    if not taxon_details and taxon.get('id'):
        fetched = get_taxon_details(taxon['id'])
        if fetched:
            taxon_details = fetched
            observation_data['taxon_details'] = fetched
    ancestors = (taxon_details or {}).get('ancestors', [])
    
    # Find the genus in the ancestors
    genus = None
    for ancestor in ancestors:
        if ancestor.get('rank') == 'genus':
            genus = ancestor.get('name')
            break
    
    # If we couldn't find the genus, use the name as is
    if not genus:
        # Fallback; italicize full name
        return f"__ITALIC_START__{scientific_name}__ITALIC_END__"
    
    # Construct: italicize genus and epithet, not the rank label
    return f"__ITALIC_START__{genus}__ITALIC_END__ {rank_label[rank]} __ITALIC_START__{scientific_name}__ITALIC_END__"

def create_inaturalist_label(observation_data, iconic_taxon_name, show_common_names=False, omit_notes=False, debug=False, custom_add=None, custom_remove=None):
    """Build a label record from observation data.

    Returns (label_fields, iconic_taxon_name) where label_fields is a list of (field, value) tuples
    suitable for either RTF/PDF rendering or plaintext output. If observation_data is None, returns (None, None).
    When omit_notes is True, the Notes field is omitted entirely. When debug is True, custom
    observation fields (OFVs) are logged to stderr to aid troubleshooting.
    """
    # If no data, return quietly; upstream will report a single concise error.
    if observation_data is None:
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
        # Only use the preferred common name; do not fall back to scientific name
        common_name = taxon.get('preferred_common_name') or ''
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
    scientific_name_plain = scientific_name.replace('__ITALIC_START__','').replace('__ITALIC_END__','')
    scientific_name_parts = scientific_name_plain.lower().split()
    common_name_normalized = normalize_string(common_name) if common_name else ''
    
    # Check if common name matches any part of the scientific name
    is_redundant = False
    if common_name:
        # First check if it matches the full scientific name (plain)
        if common_name_normalized == normalize_string(scientific_name_plain):
            is_redundant = True
        else:
            # Check if it matches any part of the scientific name
            for part in scientific_name_parts:
                # Skip rank abbreviations (sect., subsp., subsect., subg., etc.) and keywords
                if part.endswith('.') or part in {'complex'}:
                    continue
                if normalize_string(part) == common_name_normalized:
                    is_redundant = True
                    break
    
    # Only add common name if it's not redundant
    if show_common_names and common_name and not is_redundant:
        label.append(("Common Name", common_name))

    # Add these fields to all labels
    if isinstance(obs_number, str) and obs_number.startswith("MO"):
        # For Mushroom Observer data
        mo_number = obs_number.replace("MO", "")
        label.extend([
            ("Mushroom Observer Number", mo_number),
            ("Mushroom Observer URL", url),
            ("Location", location),
            ("Coordinates", gps_coords),
            ("Date Observed", date_observed_str),
            ("Observer", observer)
        ])
    else:
        # For iNaturalist data
        label.extend([
            ("iNaturalist Observation Number", str(obs_number)),
            ("iNaturalist URL", url),
            ("Location", location),
            ("Coordinates", gps_coords),
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

    # If we are in debug mode, print the fields that are found
    if debug:
        for f in observation_data.get("ofvs", []):
            print_error("OFV FIELD: " + repr(f.get("name")) + " → " + repr(f.get("value")))

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
        # I guess we don't really need the species name override field on the label - just go ahead and make it actually override the name
        # label.append(("Species Name Override", species_name_override))

        # If there is a scientific name override, actually override the scientific name
        label[0] = ("Scientific Name", f"__ITALIC_START__{species_name_override}__ITALIC_END__")

    microscopy = get_field_value(observation_data, 'Microscopy Performed')
    if microscopy:
        label.append(("Microscopy Performed", microscopy))

    fungal_microscopy = get_field_value(observation_data, 'Fungal Microscopy')
    if fungal_microscopy:
        label.append(("Fungal Microscopy", fungal_microscopy))

    photography_type = get_field_value(observation_data, 'Mobile or Traditional Photography?')
    if photography_type:
        label.append(("Mobile or Traditional Photography", photography_type))

    collectors_name = get_field_value(observation_data, 'Collector\'s name')
    if collectors_name:
        label.append(("Collector's name", collectors_name))

    herbarium_catalog_number = get_field_value(observation_data, 'Herbarium Catalog Number')
    if herbarium_catalog_number:
        label.append(("Herbarium Catalog Number", herbarium_catalog_number))

    fungarium_catalog_number = get_field_value(observation_data, 'Fungarium Catalog Number')
    if fungarium_catalog_number:
        label.append(("Fungarium Catalog Number", fungarium_catalog_number))

    herbarium_secondary_catalog_number = get_field_value(observation_data, 'Herbarium Secondary Catalog Number')
    if herbarium_secondary_catalog_number:
        label.append(("Herbarium Secondary Catalog Number", herbarium_secondary_catalog_number))

    habitat = get_field_value(observation_data, 'Habitat')
    if habitat:
        label.append(("Habitat", habitat))

    microhabitat = get_field_value(observation_data, 'Microhabitat')
    if microhabitat:
        label.append(("Microhabitat", microhabitat))

    collection_number = get_field_value(observation_data, 'Collection Number')
    if collection_number:
        label.append(("Collection Number", collection_number))

    associated_species = get_field_value(observation_data, 'Associated Species')
    if associated_species:
        label.append(("Associated Species", associated_species))

    herbarium_name = get_field_value(observation_data, 'Herbarium Name')
    if herbarium_name:
        label.append(("Herbarium Name", herbarium_name))

    mycoportal_id = get_field_value(observation_data, 'Mycoportal ID')
    if mycoportal_id:
        label.append(("Mycoportal ID", mycoportal_id))

    voucher_number = get_field_value(observation_data, 'Voucher Number')
    if voucher_number:
        label.append(("Voucher Number", voucher_number))

    voucher_numbers = get_field_value(observation_data, 'Voucher Number(s)')
    if voucher_numbers:
        label.append(("Voucher Number(s)", voucher_numbers))

    accession_number = get_field_value(observation_data, 'Accession Number')
    if accession_number:
        label.append(("Accession Number", accession_number))

    mushroom_observer_url = get_field_value(observation_data, 'Mushroom Observer URL')
    # Avoid duplicating the MO URL if this is a Mushroom Observer observation
    if mushroom_observer_url and not (isinstance(obs_number, str) and obs_number.startswith("MO")):
        # Format Mushroom Observer URL in the best possible way
        formatted_url = format_mushroom_observer_url(mushroom_observer_url)
        label.append(("Mushroom Observer URL", formatted_url))

    if not omit_notes:
        notes = observation_data.get('description') or ''
        # Convert HTML in notes field to text
        notes_parsed = parse_html_notes(notes)
        label.append(("Notes", notes_parsed))

    if custom_add:
        for field_name in custom_add:
            value = get_field_value(observation_data, field_name)
            if value:
                label.append((field_name, value))

    if custom_remove:
        remove_set = {n.lower() for n in custom_remove}
        label = [item for item in label if item[0].lower() not in remove_set]

    return label, iconic_taxon_name

def create_fungus_fair_label(observation_data, iconic_taxon_name, show_common_names=False, debug=False):
    """Build a minimal label record for fungus fair usage (Sci Name, Common Name, Habitat, Spore Print, Edibility)."""
    if observation_data is None:
        return None, None
    
    taxon = observation_data.get('taxon', {})
    # Only use the preferred common name; do not fall back to scientific name
    common_name = taxon.get('preferred_common_name') or ''
    
    # Use the name directly from observation data to avoid API calls in format_scientific_name
    if not taxon:
        scientific_name = 'Not available'
        raw_scientific_name = 'Not available'
    else:
        raw_scientific_name = taxon.get('name', 'Not available')
        scientific_name = f"__ITALIC_START__{raw_scientific_name}__ITALIC_END__"
        # Simple formatting for common ranks without API lookups
        for rank_marker in [' var. ', ' subsp. ', ' f. ', ' sect. ', ' subg. ']:
            if rank_marker in scientific_name:
                scientific_name = scientific_name.replace(rank_marker, f"__ITALIC_END__{rank_marker}__ITALIC_START__")

    label = [
        ("Scientific Name", scientific_name)
    ]

    scientific_name_plain = raw_scientific_name
    scientific_name_parts = scientific_name_plain.lower().split()
    common_name_normalized = normalize_string(common_name) if common_name else ''
    
    is_redundant = False
    if common_name:
        if common_name_normalized == normalize_string(scientific_name_plain):
            is_redundant = True
        else:
            for part in scientific_name_parts:
                if part.endswith('.') or part in {'complex'}:
                    continue
                if normalize_string(part) == common_name_normalized:
                    is_redundant = True
                    break
    
    if show_common_names and common_name and not is_redundant:
        label.append(("Common Name", common_name))

    # Add custom fields if they exist
    habitat = get_field_value(observation_data, 'Habitat')
    if habitat:
        label.append(("Habitat", habitat))

    spore_print = get_field_value(observation_data, 'Spore Print')
    if not spore_print:
        spore_print = get_field_value(observation_data, 'Spore Print Color')
    if spore_print:
        label.append(("Spore Print", spore_print))
        
    edibility = get_field_value(observation_data, 'Edibility')
    norm = normalize_edibility(edibility) if edibility else None
    if norm:
        label.append(("Edibility", norm))
    else:
        label.append(("Edibility", "unknown"))

    return label, iconic_taxon_name

def find_non_ascii_chars(labels):
    """Find all non-ASCII characters in the label data, ignoring certain common symbols."""
    non_ascii_chars = set()
    # The default font handles '±' (U+00B1) correctly
    ignore_chars = {'±'}

    for label, _ in labels:
        for _, value in label:
            if isinstance(value, str):
                for char in value:
                    if ord(char) >= 128 and char not in ignore_chars:
                        non_ascii_chars.add(char)
    return non_ascii_chars

def create_pdf_content(labels, filename, no_qr=False, title_field=None, fungus_fair_mode=False):
    """Render labels into a two-column PDF at the given filename.

    Expects labels as an iterable of (label_fields, iconic_taxon_name). Adds a QR code when a URL is present.
    """
    register_fonts()
    if fungus_fair_mode:
        try:
            from PIL import Image as PILImage
        except ImportError:
            print_error("Error: PIL (Pillow) is required for fungus fair mode image handling.")
            sys.exit(1)

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
    
    # Conditionally select the font based on label content
    base_font = PDF_BASE_FONT
    non_ascii_found = find_non_ascii_chars(labels)
    font_size_multiplier = 1.0
    if non_ascii_found:
        try:
            # Verify that the system font was successfully registered before using it
            pdfmetrics.getFont('SystemUnicodeFont')
            base_font = 'SystemUnicodeFont'
            # System fonts often have larger glyphs; reduce font size to compensate.
            font_size_multiplier = 0.85
            char_report = ", ".join(f"'{c}'" for c in sorted(list(non_ascii_found)))
            print_error(f"Info: Non-ASCII characters detected: {char_report}. Switching to system Unicode font for PDF and reducing text size.")
        except KeyError:
            print_error("Warning: Non-ASCII characters detected, but the system Unicode font is not available. Characters may not render correctly.")

    custom_normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontName=base_font,
        fontSize=12 * font_size_multiplier,
        leading=14 * font_size_multiplier
    )
    title_normal_style = ParagraphStyle(
        'TitleNormal',
        parent=styles['Title'],
        fontName=base_font,
        fontSize=16 * font_size_multiplier,
        leading=18 * font_size_multiplier,
        alignment=1  # Centered
    )   
    story = []

    for label, iconic_taxon_name in labels:
        label_content = []
        notes_value = "" # Default if not found

        if fungus_fair_mode:
            sci_name = next((v for f, v in label if f == "Scientific Name"), "")
            common_name = next((v for f, v in label if f == "Common Name"), "")
            habitat = next((v for f, v in label if f == "Habitat"), "")
            spore_print = next((v for f, v in label if f == "Spore Print"), "")
            edibility = next((v for f, v in label if f == "Edibility"), "")

            ff_center_style = ParagraphStyle(
                'FFCenter',
                parent=styles['Normal'],
                fontName=base_font,
                fontSize=18 * font_size_multiplier,
                leading=22 * font_size_multiplier,
                alignment=1 # Center
            )

            ff_sci_style = ParagraphStyle(
                'FFSci',
                parent=ff_center_style,
                fontSize=22 * font_size_multiplier,
                leading=26 * font_size_multiplier
            )

            # Scientific Name
            label_content.append(Paragraph(f"<b>{rl_safe(sci_name)}</b>", ff_sci_style))

            if common_name:
                label_content.append(Paragraph(f"<b>{rl_safe(common_name)}</b>", ff_center_style))
            
            # Removed Spacer to move everything up (part of "quarter inch higher" request)
            # label_content.append(Spacer(1, 0.2*inch))

            # Details
            details_text = []
            if habitat:
                details_text.append(f"<b>Habitat:</b> {rl_safe(habitat)}")
            if spore_print:
                details_text.append(f"<b>Spore Print:</b> {rl_safe(spore_print)}")
            if edibility:
                details_text.append(f"<b>Edibility:</b> {rl_safe(get_pretty_edibility(edibility))}")
            
            details_p = None
            if details_text:
                details_p = Paragraph("<br/>".join(details_text), custom_normal_style)

            # Image
            img_obj = None
            if edibility:
                script_dir = os.path.dirname(os.path.realpath(__file__))
                img_name = os.path.join(script_dir, "images", f"{edibility.lower()}.jpg")
                if os.path.exists(img_name):
                    try:
                        with PILImage.open(img_name) as pil_img:
                            iw, ih = pil_img.size
                            aspect = iw / ih
                            h = 1.0 * inch
                            w = h * aspect
                            max_w = frame_width * 0.4
                            if w > max_w:
                                w = max_w
                                h = w / aspect
                            img_obj = ReportLabImage(img_name, width=w, height=h)
                    except Exception as e:
                        print_error(f"Error loading image {img_name}: {e}")
                else:
                    pass

            if img_obj:
                # Add 0.5 inch to width to accommodate the shift left
                col2_width = img_obj.drawWidth + 0.6*inch 
                col1_width = frame_width - col2_width
                if col1_width < 1*inch:
                    col1_width = frame_width / 2
                    col2_width = frame_width / 2
                
                # If details_p is None, use an empty Paragraph
                table_details = details_p if details_p else Paragraph("", custom_normal_style)
                table = Table([[table_details, img_obj]], colWidths=[col1_width, col2_width])
                table.setStyle(TableStyle([
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('ALIGN', (1,0), (1,0), 'RIGHT'),
                    ('LEFTPADDING', (0,0), (-1,-1), 0),
                    ('RIGHTPADDING', (0,0), (0,0), 0), # No padding on text cell
                    ('RIGHTPADDING', (1,0), (1,0), 0.5*inch), # 0.5 inch padding on image cell to move it left
                    ('TOPPADDING', (0,0), (0,0), 0.25*inch), # Push text down 0.25 inch
                    ('TOPPADDING', (1,0), (1,0), 0), # Keep image at top
                    ('BOTTOMPADDING', (0,0), (-1,-1), 0),
                ]))
                label_content.append(table)
            elif details_p:
                label_content.append(details_p)

            label_content.append(Spacer(1, 0.75*inch))
            
            # Simple height estimate for fungus fair mode
            # 3 lines of header (22pt+), 4 lines of details (14pt), Image (1 inch), Spacer (0.75 inch)
            height_estimate = (3 * 26 + 4 * 14) * font_size_multiplier + 1.0 * 72 + 0.75 * 72 
            
        else:
            pre_notes_content = []
            qr_url = next(
                (value for field, value in label
                 if field in ("iNaturalist URL", "Mushroom Observer URL")),
                None)
            
            if title_field:
                for field, value in label:
                    if field == title_field:
                        p = Paragraph(f"<b>{rl_safe(value)}</b>", title_normal_style)
                        pre_notes_content.append(p)
                        pre_notes_content.append(Spacer(1, 0.1*inch))
                        break

            for field, value in label:
                if field == "Notes":
                    notes_value = value
                    continue
                if title_field and field == title_field:
                    continue
                elif field == "Scientific Name":
                    p = Paragraph(f"<b>{field}:</b> {rl_safe(value)}", custom_normal_style)
                    pre_notes_content.append(p)
                elif field == "iNaturalist URL":
                    p = Paragraph(rl_safe(value), custom_normal_style)
                    pre_notes_content.append(p)
                else:
                    p = Paragraph(f"<b>{field}:</b> {rl_safe(value)}", custom_normal_style)
                    pre_notes_content.append(p)

            notes_paragraph = None
            if notes_value:
                notes_safe = rl_safe(notes_value)
                # Remove the line about the MO to iNat import, as this isn't important on a label since we already include the MO URL
                notes_safe = re.sub(r'Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', notes_safe)
                # Remove line about the inat to MO import, as this isn't important on a label since we already include the MO URL (added by MO on import)
                notes_safe = re.sub(r'Imported by Mushroom Observer \d{4}-\d{2}-\d{2}', '', notes_safe)
                notes_safe = notes_safe.replace('\n', '<br/>')
                notes_paragraph = Paragraph(f"<b>Notes:</b> {notes_safe}", custom_normal_style)

            qr_image = None
            if qr_url and not no_qr:
                qr_hex, _ = generate_qr_code(qr_url, minilabel_mode=False)
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
            height_estimate = len(pre_notes_content) * 14 * font_size_multiplier
            if notes_paragraph:
                height_estimate += (notes_value.count('\n') + 1) * 14 * font_size_multiplier

        if height_estimate > frame_height:
            story.append(KeepInFrame(frame_width, frame_height, label_content, mode='shrink'))
        else:
            story.append(KeepTogether(label_content))

    doc.build(story)


def create_minilabel_pdf_content(labels, filename):
    """Render minilabels into an 8-column PDF, with QR on left and 'iNat' + number top-aligned on the right."""
    register_fonts()
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Image as ReportLabImage, Table, TableStyle
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    import binascii
    from io import BytesIO

    # page + margins
    doc = BaseDocTemplate(
        filename,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    num_columns = 8
    usable_width = doc.width   # inside margins
    col_width = usable_width / num_columns

    # make 8 skinny frames
    frames = []
    for i in range(num_columns):
        x = doc.leftMargin + i * col_width
        frames.append(
            Frame(
                x,
                doc.bottomMargin,
                col_width,
                doc.height,
                id=f"mini-col-{i}",
                leftPadding=0,
                rightPadding=0,
                topPadding=0,
                bottomPadding=0,
            )
        )

    doc.addPageTemplates([PageTemplate(id="MiniLabels", frames=frames)])

    styles = getSampleStyleSheet()

    # Conditionally select the font based on label content
    base_font = PDF_BASE_FONT
    non_ascii_found = find_non_ascii_chars(labels)
    font_size_multiplier = 1.0
    if non_ascii_found:
        try:
            # Verify that the system font was successfully registered before using it
            pdfmetrics.getFont('SystemUnicodeFont')
            base_font = 'SystemUnicodeFont'
            # System fonts often have larger glyphs; reduce font size to compensate.
            font_size_multiplier = 0.85
            char_report = ", ".join(f"'{c}'" for c in sorted(list(non_ascii_found)))
            print_error(f"Info: Non-ASCII characters detected: {char_report}. Switching to system Unicode font for PDF and reducing text size.")
        except KeyError:
            print_error("Warning: Non-ASCII characters detected, but the system Unicode font is not available. Characters may not render correctly.")

    text_style = ParagraphStyle(
        "MiniLabelText",
        parent=styles["Normal"],
        fontName=base_font,
        fontSize=7.5 * font_size_multiplier,
        leading=8.5 * font_size_multiplier,
    )

    story = []

    for label, _ in labels:
        # get the two things we actually need
        obs_number = next((v for f, v in label if "Observation Number" in f), None)
        qr_url = next((v for f, v in label if "URL" in f), None)

        if not obs_number or not qr_url:
            story.append(Spacer(1, 0.04 * inch))
            continue

        # make small QR
        qr_hex, _ = generate_qr_code(qr_url, minilabel_mode=True)
        if not qr_hex:
            story.append(Spacer(1, 0.04 * inch))
            continue

        qr_img_data = BytesIO(binascii.unhexlify(qr_hex))
        qr_size = 0.33 * inch  # small but scannable
        qr_image = ReportLabImage(qr_img_data, width=qr_size, height=qr_size)

        # right-hand stacked text
        p_title = Paragraph("iNaturalist", text_style)
        p_id = Paragraph(rl_safe(obs_number), text_style)

        text_width = col_width - qr_size
        if text_width < 0.28 * inch:
            text_width = col_width * 0.55

        # text table: 2 rows, top-aligned
        text_table = Table(
            [[p_title],
             [p_id]],
            colWidths=[text_width],
        )
        text_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

        # outer table: [ QR | text_table ]
        outer = Table(
            [[qr_image, text_table]],
            colWidths=[qr_size, text_width],
        )
        outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

        story.append(outer)
        story.append(Spacer(1, 0.04 * inch))

    doc.build(story)


def create_rtf_content(labels, no_qr=False, fungus_fair_mode=False):
    """Generate RTF content for the given labels and return it as a string.

    - keeps QR code right-justified
    - avoids blank space at the top of the right column in LibreOffice
    - avoids a trailing blank paragraph after the last label
    """
    if fungus_fair_mode:
        try:
            from PIL import Image as PILImage
        except ImportError:
            print_error("Error: PIL (Pillow) is required for fungus fair mode image handling.")
            sys.exit(1)

    rtf_header = RTF_HEADER
    rtf_footer = r"}"

    rtf_content = rtf_header

    def split_hex_string(s, n):
        return '\n'.join([s[i:i+n] for i in range(0, len(s), n)])

    def _format_rtf_text(text):
        text = escape_rtf(text)
        text = text.replace('__BOLD_START__', r'{\b ').replace('__BOLD_END__', r'}')
        text = text.replace('__ITALIC_START__', r'{\i ').replace('__ITALIC_END__', r'}')
        return text

    try:
        total = len(labels)
        for idx, (label, _iconic_taxon_name) in enumerate(labels):
            # Calculate space before to handle spacing between labels without ghost space at top of columns
            sb_twips = 0
            if idx > 0:
                sb_twips = 1080 if fungus_fair_mode else 560
            space_before_cmd = f"\\sb{sb_twips} "

            # start one label, force zero space-after here
            rtf_content += r"{\keep\pard\ql\keepn\sa0 " + space_before_cmd

            if fungus_fair_mode:
                sci_name = next((v for f, v in label if f == "Scientific Name"), "")
                common_name = next((v for f, v in label if f == "Common Name"), "")
                habitat = next((v for f, v in label if f == "Habitat"), "")
                spore_print = next((v for f, v in label if f == "Spore Print"), "")
                edibility = next((v for f, v in label if f == "Edibility"), "")

                # Scientific Name - Center, Size 44 (22pt)
                sci_name_rtf = _format_rtf_text(sci_name)
                # Re-apply space_before_cmd because \pard resets paragraph properties
                rtf_content += r"\pard" + space_before_cmd + r"\keep\keepn\qc\sa120 {\fs44\b " + sci_name_rtf + r"}\par "

                # Common Name - Center, Size 36 (18pt)
                if common_name:
                    rtf_content += r"\pard\keep\keepn\qc\sa240 {\fs36\b " + escape_rtf(common_name) + r"}\par "
                else:
                    rtf_content += r"\pard\keep\keepn\sa240 \par "
                
                # Reset alignment to left for details
                rtf_content += r"\pard\keep\ql\sa0 "

                # Details text
                details_rtf = ""
                if habitat:
                    details_rtf += r"{\b Habitat:} " + escape_rtf(habitat) + r"\line "
                if spore_print:
                    details_rtf += r"{\b Spore Print:} " + escape_rtf(spore_print) + r"\line "
                if edibility:
                    details_rtf += r"{\b Edibility:} " + escape_rtf(get_pretty_edibility(edibility))
                
                # Image handling
                img_hex = None
                img_dims = None
                if edibility:
                    script_dir = os.path.dirname(os.path.realpath(__file__))
                    img_name = os.path.join(script_dir, "images", f"{edibility.lower()}.jpg")
                    if os.path.exists(img_name):
                        try:
                            with PILImage.open(img_name) as pil_img:
                                # Convert to RGB if necessary (e.g. for PNGs with transparency)
                                if pil_img.mode in ('RGBA', 'P'):
                                    pil_img = pil_img.convert('RGB')
                                
                                iw, ih = pil_img.size
                                aspect = iw / ih
                                
                                # Convert to JPEG in memory
                                buffer = BytesIO()
                                pil_img.save(buffer, format="JPEG")
                                img_bytes = buffer.getvalue()
                                img_hex = binascii.hexlify(img_bytes).decode('utf-8')
                                
                                # Layout math (similar to PDF logic)
                                # Target height 1 inch = 1440 twips
                                # Max width 40% of col (~2160 twips)
                                h_twips = 1440
                                w_twips = int(h_twips * aspect)
                                max_w_twips = 2160 # Approx 1.5 inch
                                
                                if w_twips > max_w_twips:
                                    w_twips = max_w_twips
                                    h_twips = int(w_twips / aspect)
                                
                                img_dims = (w_twips, h_twips)

                        except Exception as e:
                            print_error(f"Error processing image {img_name}: {e}")

                # If image exists, use a table or absolute positioning?
                # RTF tables are simpler. 2 columns: Text | Image
                if img_hex and img_dims:
                    # Table def
                    rtf_content += r"\trowd\trkeep\trgaph108\trleft0" # 108 twips gap
                    
                    # Col 1 width (rest of space)
                    # Col 2 width (image width + padding)
                    # Assume col width ~5400 twips. 
                    col2_w = img_dims[0] + 720 # image width + 0.5 inch padding
                    col1_w = 5400 - col2_w
                    
                    rtf_content += r"\cellx" + str(col1_w)
                    rtf_content += r"\cellx" + str(col1_w + col2_w)
                    
                    # Cell 1: Details
                    rtf_content += r"\pard\intbl " + details_rtf + r"\cell "
                    
                    # Cell 2: Image (Right aligned)
                    rtf_content += r"\pard\intbl\qr "
                    rtf_content += (
                        r'{\pict\jpegblip\picw' + str(iw) +
                        r'\pich' + str(ih) +
                        r'\picwgoal' + str(img_dims[0]) +
                        r'\pichgoal' + str(img_dims[1]) + r' '
                    )
                    rtf_content += split_hex_string(img_hex, 76)
                    rtf_content += r'}\cell '
                    rtf_content += r"\row "
                    
                else:
                    # No image, just dump text
                    rtf_content += details_rtf + r"\par "

            else:
                # Standard Label Logic
                # find url and notes length first
                qr_url = next(
                    (value for field, value in label
                     if field in ("iNaturalist URL", "Mushroom Observer URL")),
                    None
                )
                notes_value = next((value for field, value in label if field == "Notes"), "")
                notes_length = len(str(notes_value)) if notes_value else 0

                # body fields
                for field, value in label:
                    if field == "iNaturalist URL":
                        rtf_content += escape_rtf(str(value)) + r" \line "
                    elif field == "Mushroom Observer URL":
                        rtf_content += escape_rtf(str(value)) + r"\line "
                    elif field.startswith("iNat") or field.startswith("iNaturalist") or field.startswith("Mushroom Observer"):
                        if field.startswith("Mushroom Observer"):
                            first_chars, rest = field[:2], field[2:]
                            rtf_content += (
                                r"{\ul\b " + first_chars + r"}{\scaps\ul\b " + rest + r":} "
                                + escape_rtf(str(value)) + r"\line "
                            )
                        else:
                            first_char, rest = field[0], field[1:]
                            rtf_content += (
                                r"{\ul\b " + first_char + r"}{\scaps\ul\b " + rest + r":} "
                                + escape_rtf(str(value)) + r"\line "
                            )
                    elif field == "Scientific Name":
                        value_rtf = _format_rtf_text(str(value))
                        rtf_content += r"{\scaps\ul\b " + escape_rtf(field) + r":} " + value_rtf + r"\line "
                    elif field == "Coordinates":
                        value_rtf = escape_rtf(value)
                        rtf_content += r"{\scaps\ul\b " + escape_rtf(field) + r":} " + value_rtf + r"\line "
                    elif field == "Notes":
                        if value:
                            # strip blank lines out of Notes
                            lines = str(value).split('\n')
                            non_blank_lines = [line for line in lines if line.strip()]
                            value = '\n'.join(non_blank_lines)

                            rtf_content += r"{\scaps\ul\b " + escape_rtf(field) + r":} "
                            value_rtf = _format_rtf_text(value)
                            # MO import cleanup
                            value_rtf = re.sub(
                                r'\\line Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.',
                                '',
                                value_rtf
                            )
                            value_rtf = re.sub(
                                r'((\\line)\s+\2+\s+\2 Imported|Imported) by Mushroom Observer \d{4}-\d{2}-\d{2}',
                                '',
                                value_rtf
                            )
                            rtf_content += value_rtf
                    else:
                        rtf_content += r"{\scaps\ul\b " + escape_rtf(field) + r":} " + escape_rtf(str(value)) + r"\line "

                # QR code (right aligned)
                if not no_qr:
                    qr_hex, qr_size = generate_qr_code(qr_url, minilabel_mode=False) if qr_url else (None, None)
                    if qr_hex:
                        # if we had no notes and we ended with "\line ", drop it so QR sits right under text
                        if notes_length == 0 and rtf_content.endswith(r"\line "):
                            rtf_content = rtf_content[:-6]

                        rtf_content += r"\par\pard\qr\ri360\sb57\sa0 "
                        qr_width_twips = qr_size[0] * 15
                        qr_height_twips = qr_size[1] * 15
                        rtf_content += (
                            r'{\pict\pngblip\picw' + str(qr_width_twips) +
                            r'\pich' + str(qr_height_twips) +
                            r'\picwgoal' + str(qr_width_twips) +
                            r'\pichgoal' + str(qr_height_twips) + r' '
                        )
                        rtf_content += split_hex_string(qr_hex, 76)
                        rtf_content += r'}'
                        # always just one paragraph after QR so we don't create tall gaps
                        rtf_content += r"\par"
                    else:
                        # no QR - just end paragraph cleanly
                        rtf_content += r"\par"
                else:
                    # no QR – just end paragraph cleanly
                    rtf_content += r"\par"

            # close label group
            rtf_content += r"}"

        rtf_content += rtf_footer
    except Exception as e:
        return rtf_header + "Error generating content: " + escape_rtf(str(e)) + rtf_footer

    return rtf_content



def create_minilabel_rtf_content(labels):
    """Generate RTF content for minilabels and return it as a string."""
    rtf_header = MINILABEL_RTF_HEADER
    rtf_footer = r"}"
    rtf_content = rtf_header

    num_columns = 8
    page_width_twips = 12240
    margins = 720  # 0.5 inch
    usable_width = page_width_twips - (2 * margins)
    col_width = usable_width // num_columns

    # Build the table rows
    table_rows = []
    for i in range(0, len(labels), num_columns):
        row_labels = labels[i:i + num_columns]
        row = []
        for label, _ in row_labels:
            obs_number = next((value for field, value in label if 'Observation Number' in field), None)
            qr_url = next((value for field, value in label if 'URL' in field), None)

            if not obs_number or not qr_url:
                row.append("")
                continue

            qr_hex, qr_size = generate_qr_code(qr_url, minilabel_mode=True)
            if not qr_hex:
                row.append("")
                continue

            qr_pixel_width = qr_size[0]
            qr_pixel_height = qr_size[1]
            desired_twips = 500  # QR display size (500 twips ≈ 0.35 inch)

            # Build cell content
            cell_content = "{"
            cell_content += (
                r'{\pict\pngblip'
                r'\picw' + str(qr_pixel_width) +
                r'\pich' + str(qr_pixel_height) +
                r'\picwgoal' + str(desired_twips) +
                r'\pichgoal' + str(desired_twips) +
                r' ' + qr_hex + r'}'
            )
            # Observation number, slightly spaced but no redundant paragraph breaks
            cell_content += r'\pard\fs16 ' + str(obs_number)
            cell_content += "}"
            row.append(cell_content)

        # Pad the row to full width
        while len(row) < num_columns:
            row.append("")
        table_rows.append(row)

    # Emit the RTF table
    rtf_content += r'\pard\par'
    for row in table_rows:
        # Define the row, no gap, rely on cell padding for vertical spacing
        rtf_content += r'\trowd\trgaph0'
        for i in range(num_columns):
            # No borders, add small vertical padding
            rtf_content += (
                r'\clbrdrt\brdrnil'
                r'\clbrdrl\brdrnil'
                r'\clbrdrb\brdrnil'
                r'\clbrdrr\brdrnil'
                r'\clpadl0'
                r'\clpadt80'   # top padding: 80 twips (~0.055")
                r'\clpadr0'
                r'\clpadb80'   # bottom padding: 80 twips
                r'\clpadfl3\clpadft3\clpadfr3\clpadfb3'
                f'\\cellx{(i + 1) * col_width}'
            )

        # Add the cell content
        rtf_content += r'\pard\intbl'
        for cell in row:
            rtf_content += cell + r'\cell'
        rtf_content += r'\row'

    rtf_content += rtf_footer
    return rtf_content


def normalize_edibility(s):
    """Normalize edibility string to a canonical set of values."""
    if not s:
        return None
    # Remove non-alpha characters and convert to lowercase
    t = re.sub(r'[^a-z]', '', s.strip().lower())
    return {
        'edible': 'edible',
        'nonedible': 'nonedible',
        'inedible': 'nonedible',
        'poisonous': 'poisonous',
        'toxic': 'poisonous',
        'unknown': 'unknown',
    }.get(t)

def get_pretty_edibility(edibility_value):
    """Map normalized edibility values to human-friendly display strings."""
    if not edibility_value:
        return edibility_value
    mapping = {
        'edible': 'Edible',
        'nonedible': 'Not edible',
        'poisonous': 'Poisonous',
        'unknown': 'Unknown'
    }
    return mapping.get(edibility_value.lower(), edibility_value)

# Check to see if the observation is in California
def is_within_california(latitude, longitude):
    """Return True if the point lies within an approximate California bounding box."""
    # Approximate bounding box for California
    CA_NORTH = 42.0
    CA_SOUTH = 32.5
    CA_WEST = -124.4
    CA_EAST = -114.1

    return (CA_SOUTH <= latitude <= CA_NORTH) and (CA_WEST <= longitude <= CA_EAST)

def main():
    """
    Command-line entry point that builds herbarium labels from iNaturalist or Mushroom Observer observation identifiers and writes them to stdout, an RTF file, or a PDF file.
    
    Parses command-line arguments to accept observation numbers or URLs (or a file of them), fetches observation data in parallel, and generates formatted labels. Supported behaviors include:
    - Writing labels to an RTF file (--rtf) or a PDF file (--pdf), or printing human-readable labels to stdout when no output file is specified. When writing files, prints the created filename and its size in kilobytes when available.
    - A discovery mode (--find-ca) that prints iNaturalist observation URLs for observations located within California instead of generating labels.
    - Reading observation identifiers from a file via --file; accepts space-, comma-, or newline-separated entries.
    - Concurrency tuning via --workers (or INAT_MAX_WORKERS env var) and global retry timeout adjustment via --max-wait-seconds (or INAT_MAX_WAIT_SECONDS env var).
    - Minimal verbosity control (--quiet) and a debug flag (--debug).
    
    Updates module-global controls used by API calls (e.g., max wait time and quiet mode), enforces filename extensions for RTF/PDF outputs, and respects API rate/concurrency constraints while fetching data. Prints a final summary of requested, generated, and failed counts with elapsed time; prints per-failure messages to stderr. Exits with an error if no CLI arguments are supplied or if the provided input file cannot be read.
    """
    parser = argparse.ArgumentParser(
        description="Create herbarium labels or fungus fair signage from iNaturalist/Mushroom Observer data",
        epilog="Examples:\n  inat.label.py --fungusfair labels.csv --pdf out.pdf\n  inat.label.py --fungusfair 12345 67890 --pdf out.pdf",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("observation_ids", nargs="*", help="Observation number(s), URL(s), or CSV file(s) (for fungus fair mode)")
    parser.add_argument("--file", metavar="filename", help="File containing observation numbers or URLs (separated by spaces, commas, or newlines)")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--rtf", metavar="filename.rtf", help="Output to RTF file (filename must end with .rtf)")
    output_group.add_argument("--pdf", metavar="filename.pdf", help="Output to PDF file (filename must end with .pdf)")
    parser.add_argument("--find-ca", action="store_true", help="Find observations within California")
    parser.add_argument("--workers", type=int, default=None, help="Max parallel API requests (default 5, or INAT_MAX_WORKERS env)")
    parser.add_argument("--max-wait-seconds", type=float, default=None, help="Max total wait per API call when retrying (default 30s, or INAT_MAX_WAIT_SECONDS env)")
    parser.add_argument("--common-names", action="store_true", help="Include common names in labels (off by default)")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed retry messages (e.g., 429 lines); still shows patience notes and summary")
    parser.add_argument('--debug', action='store_true', help='Print debug output')
    parser.add_argument("--no-qr", action="store_true", help="Omit QR code from PDF and RTF labels")
    parser.add_argument("--minilabel", action="store_true", help="Generate minilabels with only observation number and QR code")
    parser.add_argument("--omit-notes", action="store_true", help="Omit the Notes field from all labels")
    parser.add_argument("--title", type=str, default=None, help="Field to use as title (only for PDF output)")
    parser.add_argument(
        '--custom',
        help='Add or remove fields from the default label format. Use "+" to add and "-" to remove. For example: --custom "+My Field, -Observer"',
    )
    parser.add_argument("--fungusfair", action="store_true", help="Generate labels for fungus fairs")
    parser.add_argument("--edibility", choices=['edible', 'nonedible', 'poisonous', 'unknown'], help="Edibility status (fungus fair mode)")
    parser.add_argument("--scientificname", type=str, help="Scientific name (fungus fair mode)")
    parser.add_argument("--commonname", type=str, help="Common name (fungus fair mode)")
    parser.add_argument("--habitat", type=str, help="Habitat (fungus fair mode)")
    parser.add_argument("--sporeprint", type=str, help="Spore print color (fungus fair mode)")
    

    args = parser.parse_args()

    fields_to_add = []
    fields_to_remove = []
    if args.custom:
        custom_items = [item.strip() for item in args.custom.split(',')]
        for arg in custom_items:
            if not arg:
                continue
            if arg.startswith('+') or arg.startswith('-'):
                mod = arg[0]
                field_name = arg[1:].strip()
                if not field_name:
                    continue

                if mod == '+':
                    fields_to_add.append(field_name)
                elif mod == '-':
                    fields_to_remove.append(field_name)
            else:
                print_error(f"Error: Invalid format for --custom. Field options must start with '+' or '-'. Found: '{arg}'")
                sys.exit(2)


    # If no arguments are provided, show help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    # User can not use 'Notes" as title field
    if args.title == "Notes":
        parser.error("argument --title: 'Notes' field can not be used as title")

    # Define rtf_mode and pdf_mode based on whether --rtf or --pdf argument is provided
    rtf_mode = bool(args.rtf)
    pdf_mode = bool(args.pdf)

    # Reset rate limiter state to ensure monotonic time consistency
    global _next_allowed_time
    with _rate_lock:
        _request_times.clear()
        _next_allowed_time = 0.0

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

    inputs = args.observation_ids or []
    
    # Initialize labels list
    labels = []
    failed = []

    if args.fungusfair:
        # Fungus fair mode: positional args must be CSV files only (or none for manual mode)
        csv_files = [x for x in inputs if x.lower().endswith('.csv')]
        non_csv = [x for x in inputs if not x.lower().endswith('.csv')]

        if non_csv:
            parser.error(
                "--fungusfair expects CSV input only. "
                f"Remove these non-CSV arguments: {', '.join(non_csv)}"
            )

        # Optional: disallow --file in fungusfair mode (since --file is for obs IDs)
        if args.file:
            parser.error("--file is not supported with --fungusfair. Pass CSV file(s) as positional args instead.")

        # From here on, do NOT treat anything as observation IDs
        observation_ids = []
    else:
        csv_files = []
        observation_ids = inputs

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

    # Process input for Fungus Fair mode (CSV files)
    if args.fungusfair:
        for csv_file in csv_files:
            if not os.path.exists(csv_file):
                print_error(f"Error: CSV file '{csv_file}' not found.")
                continue
                
            try:
                with open(csv_file, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    
                    def norm_key(s):
                        if not s:
                            return ""
                        return s.strip().lower().replace(' ', '').replace('_', '')

                    header_map = {}
                    if reader.fieldnames:
                        for field in reader.fieldnames:
                            header_map[norm_key(field)] = field
                    
                    def get_val(row, keys):
                        for key in keys:
                            real_key = header_map.get(norm_key(key))
                            if real_key and row.get(real_key):
                                return row[real_key].strip()
                        return None

                    for line_num, row in enumerate(reader, start=2):
                        # Skip empty rows (where all values are whitespace or None)
                        if not any(v.strip() for v in row.values() if v):
                            continue

                        manual_label = []
                        sci_name = get_val(row, ['scientificname', 'scientific_name', 'name'])
                        common_name = get_val(row, ['commonname', 'common_name'])
                        habitat = get_val(row, ['habitat'])
                        spore_print = get_val(row, ['sporeprint', 'spore_print'])
                        edibility = get_val(row, ['edibility'])
                        
                        if sci_name:
                             manual_label.append(("Scientific Name", f"__ITALIC_START__{sci_name}__ITALIC_END__"))
                        else:
                            print_error(f"Error on line {line_num}: Missing required value for 'Scientific Name'.")
                            continue

                        if common_name:
                            manual_label.append(("Common Name", common_name))
                        if habitat:
                            manual_label.append(("Habitat", habitat))
                        if spore_print:
                            manual_label.append(("Spore Print", spore_print))
                        
                        normalized_edibility = normalize_edibility(edibility) if edibility else None
                        if normalized_edibility:
                            manual_label.append(("Edibility", normalized_edibility))
                        else:
                            if edibility:
                                print_error(f"Warning on line {line_num}: Invalid edibility value '{edibility}'. Defaulting to 'unknown'")
                            manual_label.append(("Edibility", "unknown"))
                        
                        labels.append((manual_label, "Fungus"))
            except Exception as e:
                print_error(f"Error reading CSV file {csv_file}: {e}")
                
        if not csv_files and not args.scientificname:
             parser.error("--fungusfair requires at least one CSV file or --scientificname for a manual label.")

    # Logic for manual label (no IDs, but fungus fair args)
    if not observation_ids and args.fungusfair and args.scientificname:
        # Create a manual label
        manual_label = []
        # Mark scientific name for italics if it's manual
        manual_label.append(("Scientific Name", f"__ITALIC_START__{args.scientificname}__ITALIC_END__"))
        if args.commonname:
            manual_label.append(("Common Name", args.commonname))
        if args.habitat:
            manual_label.append(("Habitat", args.habitat))
        if args.sporeprint:
            manual_label.append(("Spore Print", args.sporeprint))
        
        manual_label.append(("Edibility", args.edibility or "unknown"))
        
        # Add to labels list
        labels.append((manual_label, "Fungus"))
        
    elif not observation_ids and not labels:
        # Standard behavior: show help if no IDs, no manual label, and no CSV labels
        parser.print_help()
        sys.exit(1)

    # Remove empty entries
    observation_ids = [obs for obs in observation_ids if obs]

    # labels list is already initialized above

    total_requested = len(observation_ids) + len(labels) # Count pre-generated labels too

    if total_requested > 25:
        # The rate limiter smooths requests to one per second when busy.
        # Add 5% for the API call itself and other small delays.
        estimated_time = total_requested * 1.05

        hours = int(estimated_time // 3600)
        minutes = int((estimated_time % 3600) // 60)
        seconds = int(estimated_time % 60)

        time_parts = []
        if hours > 0:
            time_parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            time_parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if seconds > 0 or (hours == 0 and minutes == 0 and total_requested > 0): # Include seconds if it's less than a minute, but only if there are labels to generate
            time_parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

        if not time_parts: # Handle case where estimated_time is 0
            time_str_human_readable = "no time"
        else:
            time_str_human_readable = ", ".join(time_parts)
            if len(time_parts) > 1:
                # Replace the last comma with " and " for better readability
                time_str_human_readable = time_str_human_readable.rsplit(', ', 1)[0] + ' and ' + time_str_human_readable.rsplit(', ', 1)[1]

        print(f'Generating {total_requested} labels, this will take about {time_str_human_readable}', flush=True)

    start_time = time.time()

    def process_one(input_value):
        """Process one input: normalize ID, fetch data, optionally find-CA, and build label."""
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
                if args.fungusfair:
                    label, updated_iconic_taxon = create_fungus_fair_label(
                        observation_data, iconic_taxon_name, show_common_names=args.common_names, debug=args.debug
                    )
                else:
                    label, updated_iconic_taxon = create_inaturalist_label(
                        observation_data, iconic_taxon_name, show_common_names=args.common_names, omit_notes=args.omit_notes,debug=args.debug, custom_add=fields_to_add, custom_remove=fields_to_remove
                    )

                if label is not None:
                    # Print as soon as the label is created
                    scientific_name = next((v for f, v in label if f == "Scientific Name"), "")
                    scientific_name_plain = scientific_name.replace('__ITALIC_START__','').replace('__ITALIC_END__','')
                    if updated_iconic_taxon == "Fungi":
                        print(Fore.BLUE + f"Added label for {updated_iconic_taxon}" + Style.RESET_ALL + f" {scientific_name_plain}", flush=True)
                    elif updated_iconic_taxon == "Plantae":
                        print(Fore.GREEN + f"Added label for {updated_iconic_taxon}" + Style.RESET_ALL + f" {scientific_name_plain}", flush=True)
                    else:
                        print(f"Added label for {updated_iconic_taxon} {scientific_name_plain}", flush=True)
                    return ('ok', (label, updated_iconic_taxon))
                return ('err', f"Could not create label for {observation_id}")
        except Exception as e:
            return ('err', f"Unexpected error for {input_value}: {str(e)}")

    # Respect API guidelines by limiting concurrency to a small number (<=5)
    max_workers = args.workers if args.workers else int(os.environ.get('INAT_MAX_WORKERS', '5'))
    
    # Update semaphore to match the selected worker count
    global _request_semaphore
    _request_semaphore = threading.BoundedSemaphore(max_workers)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_one, input_value) for input_value in observation_ids]
        for fut in futures:
            status, payload = fut.result()
            if status == 'ok' and payload:
                labels.append(payload)
            elif status == 'err' and payload:
                failed.append(payload)

    if not args.find_ca:
        if labels:
            if args.minilabel:
                if not (rtf_mode or pdf_mode):
                    print_error("Error: --minilabel requires either --rtf or --pdf output")
                    sys.exit(1)
                if rtf_mode:
                    rtf_content = create_minilabel_rtf_content(labels)
                    with open(args.rtf, 'w') as rtf_file:
                        rtf_file.write(rtf_content)
                    print(f"RTF file created: {os.path.basename(args.rtf)}", flush=True)
                elif pdf_mode:
                    create_minilabel_pdf_content(labels, args.pdf)
                    print(f"PDF file created: {os.path.basename(args.pdf)}", flush=True)
            elif rtf_mode:
                rtf_content = create_rtf_content(labels, no_qr=args.no_qr, fungus_fair_mode=args.fungusfair)
                with open(args.rtf, 'w') as rtf_file:
                    rtf_file.write(rtf_content)
                try:
                    size_bytes = os.path.getsize(args.rtf)
                    size_kb = (size_bytes + 1023) // 1024
                except Exception:
                    size_kb = None
                basename = os.path.basename(args.rtf)
                if size_kb is not None:
                    print(f"RTF file created: {basename} ({size_kb} kb)", flush=True)
                else:
                    print(f"RTF file created: {basename}", flush=True)
            elif pdf_mode:
                create_pdf_content(labels, args.pdf, no_qr=args.no_qr, title_field=args.title, fungus_fair_mode=args.fungusfair)
                try:
                    size_bytes = os.path.getsize(args.pdf)
                    size_kb = (size_bytes + 1023) // 1024
                except Exception:
                    size_kb = None
                basename = os.path.basename(args.pdf)
                if size_kb is not None:
                    print(f"PDF file created: {basename} ({size_kb} kb)", flush=True)
                else:
                    print(f"PDF file created: {basename}", flush=True)
            else:
                # Print labels to stdout
                for label, _ in labels:
                    for field, value in label:
                        if field == "Notes":
                            value = remove_formatting_tags(value)
                            value = re.sub(r'Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value)
                            value = re.sub(r'Imported by Mushroom Observer \d{4}-\d{2}-\d{2}', '', value)
                            print(f"{field}: {value}", flush=True)
                        elif field == "iNaturalist URL":
                            print(value, flush=True)
                        elif field == "Mushroom Observer URL":
                            print(value, flush=True)
                        else:
                            if field == "Scientific Name":
                                value = value.replace('__ITALIC_START__','').replace('__ITALIC_END__','')
                            if field == "Edibility":
                                value = get_pretty_edibility(value)
                            print(f"{field}: {value}", flush=True)
                    print("\n", flush=True)  # Blank line between labels
        else:
            print("No valid observations found.", flush=True)

        # Print summary last so it appears at the very end
        elapsed = time.time() - start_time
        failed_count_text = (Fore.RED + str(len(failed)) + Style.RESET_ALL) if failed else str(len(failed))
        generated_word = "generated"
        if total_requested != len(labels):
            generated_word = Fore.RED + "generated" + Style.RESET_ALL
        print(f"Summary: requested {total_requested}, {generated_word} {len(labels)}, failed {failed_count_text}, time {elapsed:.2f}s", flush=True)
        if failed:
            for msg in failed:
                print_error(f" - {msg}")

if __name__ == "__main__":
    # Force line-buffered output
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    else:
        # Fallback for older Python versions or environments without reconfigure
        sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)

    # Ensure the new sys.stdout is used immediately
    sys.stdout.flush()

    # Do not strip ANSI when piping to the Flask server so colors reach the browser
    colorama.init(strip=False, convert=False)
    main()
