#!/usr/bin/env python3

"""
iNaturalist Herbarium Label Generator

Author: Alan Rockefeller
Date: June 29, 2024
Version: 1.4

This script creates herbarium labels from iNaturalist observation numbers or URLs.
It fetches data from the iNaturalist API and formats it into printable labels suitable for
herbarium specimens.

Features:
- Supports multiple observation IDs or URLs as input
- Can output labels to the console or to an RTF file
- Includes various data fields such as scientific name, common name, location,
  GPS coordinates, observation date, observer, and more
- Handles special fields like DNA Barcode ITS (and LSU, TEF1, RPB1, RPB2), GenBank Accession Number,
  Provisional Species Name, Mobile or Traditional Photography?, Microscopy Performed and Mushroom Observer URL when available

Usage:
1. Basic usage (output to console):
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...]

2. Output to RTF file:
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>

Examples:
- Generate label for a single observation:
  ./inat.label.py 150291663

- Generate labels for multiple observations:
  ./inat.label.py 150291663 62240372 https://www.inaturalist.org/observations/105658809

- Generate labels and save to an RTF file:
  ./inat.label.py 150291663 62240372 --rtf two_labels.rtf

Notes:
- If the scientific name of an observation is a section, for example Amanita sect. Phalloideae, the 
  scientific name will just be Phalloideae - in that case the full scientific name will be in the 
  Common name field.
- The RTF output is formatted to closely match the style of traditional herbarium labels.
- It is recommended to print herbarium labels on 100% cotton paper for maximum longevity.

Dependencies:
- requests
- dateutil
- beautifulsoup4

The dependencies can be installed with the following command:

    pip install requests python-dateutil beautifulsoup4

Python version 3.6 or higher is recommended.

"""

import argparse
import re
import sys
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

def escape_rtf(text):
    """Escape special characters for RTF output."""
    rtf_char_map = {
        '\\': '\\\\',
        '{': '\\{',
        '}': '\\}',
        '\n': '\\line ',
        'í': '\\u237\'',
        '\\"': '\\u34\'',           #  Does not work, yet - see https://www.perplexity.ai/search/If-the-RTF-gOdEwtp2TnmQZoPfQGqpsQ
        'µ': '\\u181?',
        '×': '\\u215?',
        '“': '\\ldblquote ',
        '”': '\\rdblquote ',
        '‘': '\\lquote ',
        '’': '\\rquote ',
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
        tag.replace_with('__BOLD_START__' + tag.string + '__BOLD_END__')
    for tag in soup.find_all(['em', 'i']):
        tag.replace_with('__ITALIC_START__' + tag.string + '__ITALIC_END__')
    
    processed_text = str(soup).strip()
    return processed_text

def normalize_string(s):
    return unicodedata.normalize('NFKD', s.strip().lower())

def extract_observation_id(input_string):
    # Check if the input is a URL
    url_match = re.search(r'observations/(\d+)', input_string)
    if url_match:
        return url_match.group(1)

    # Check if the input is a number
    if input_string.isdigit():
        return input_string

    # If neither, return None
    return None

def get_observation_data(observation_id):
    url = f"https://api.inaturalist.org/v1/observations/{observation_id}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data['results']:
            return data['results'][0]
        else:
            print(f"Error: Observation {observation_id} does not exist.")
            return None
    else:
        print(f"Error: Unable to fetch data for observation {observation_id}")
        return None

def field_exists(observation_data, field_name):
    return any(field['name'].lower() == field_name.lower() for field in observation_data.get('ofvs', []))

def get_field_value(observation_data, field_name):
    for field in observation_data.get('ofvs', []):
        if field['name'].lower() == field_name.lower():
            return field['value']
    return None

def format_mushroom_observer_url(url):
    if url:
        match = re.search(r'mushroomobserver\.org/(?:observer/show_observation/)?(\d+)', url)
        if match:
            return f"https://mushroomobserver.org/{match.group(1)}"
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
            return f"{latitude}, {longitude}", f"{accuracy}"
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
    date_part = date_string.split()[0]
    
    for format in date_formats:
        try:
            parsed_date = datetime.strptime(date_part, format)
            return parsed_date.date()  # Return only the date part
        except ValueError:
            continue

    # If the above fails, try parsing the full string but only keep the date
    try:
        parsed_date = dateutil_parser.parse(date_string, fuzzy=True)
        return parsed_date.date()  # Return only the date part
    except ValueError:
        return None

def create_inaturalist_label(observation_data):
    obs_number = observation_data['id']
    url = f"https://www.inaturalist.org/observations/{obs_number}"

    taxon = observation_data.get('taxon', {})
    common_name = taxon.get('preferred_common_name', taxon.get('name', 'Not available'))
    scientific_name = observation_data.get('taxon', {}).get('name', 'Not available')

    location = observation_data.get('place_guess') or 'Not available'
    if args.rtf:
        location = escape_rtf(str(location))   # Escape characters if we are in rtf mode

    coords, accuracy = get_coordinates(observation_data)
    gps_coords = f"{coords} (±{accuracy}m)" if accuracy else coords

    date_observed = parse_date(observation_data['observed_on_string'])

    date_observed_str = str(date_observed) if date_observed else 'Not available'

    user = observation_data['user']
    display_name = user.get('name')
    login_name = user['login']
    observer = f"{display_name} ({login_name})" if display_name else login_name

    scientific_name_normalized = normalize_string(scientific_name)
    common_name_normalized = normalize_string(common_name) if common_name else ''

    label = [
    ("Scientific Name", scientific_name)
    ]

    if common_name and common_name_normalized != scientific_name_normalized:
        label.append(("Common Name", common_name))

    label.extend([
    ("iNat Observation Number", str(obs_number)),
    ("iNaturalist URL", url),
    ("Location", location),
    ("GPS Coordinates", gps_coords),
    ("Date Observed", date_observed_str),
    ("Observer", observer)
])

    dna_barcode_its = get_field_value(observation_data, 'DNA Barcode ITS')
    if dna_barcode_its:
        bp_count = len(dna_barcode_its)
        label.append(("DNA Barcode ITS", f"{bp_count} bp"))

    dna_barcode_lsu = get_field_value(observation_data, 'DNA Barcode LSU')
    if dna_barcode_lsu:
        bp_count = len(dna_barcode_lsu)
        label.append(("DNA Barcode LSU", f"{bp_count} bp"))

    dna_barcode_rpb1 = get_field_value(observation_data, 'DNA Barcode RPB1')
    if dna_barcode_rpb1:
        bp_count = len(dna_barcode_rpb1)
        label.append(("DNA Barcode RPB1", f"{bp_count} bp"))

    dna_barcode_rpb2 = get_field_value(observation_data, 'DNA Barcode RPB2')
    if dna_barcode_rpb2:
        bp_count = len(dna_barcode_rpb2)
        label.append(("DNA Barcode RPB2", f"{bp_count} bp"))

    dna_barcode_tef1 = get_field_value(observation_data, 'DNA Barcode TEF1')
    if dna_barcode_tef1:
        bp_count = len(dna_barcode_tef1)
        label.append(("DNA Barcode TEF1", f"{bp_count} bp"))


    genbank_accession = get_field_value(observation_data, 'GenBank Accession Number')
    if not genbank_accession:
        genbank_accession = get_field_value(observation_data, 'GenBank Accession')
    if genbank_accession:
        label.append(("GenBank Accession Number", genbank_accession))

    provisional_name = get_field_value(observation_data, 'Provisional Species Name')
    if provisional_name:
        label.append(("Provisional Species Name", provisional_name))

    microscopy = get_field_value(observation_data, 'Microscopy performed')
    if microscopy:
        label.append(("Microscopy performed:", microscopy))
    
    photography_type = get_field_value(observation_data, 'Mobile or Traditional Photography?')
    if photography_type:
        label.append(("Mobile or Traditional Photography", photography_type))

    mushroom_observer_url = get_field_value(observation_data, 'Mushroom Observer URL')
    if mushroom_observer_url:
        formatted_url = format_mushroom_observer_url(mushroom_observer_url)
        label.append(("Mushroom Observer URL", formatted_url))

    notes = observation_data.get('description') or ''
    notes_parsed = parse_html_notes(notes)
    label.append(("Notes", notes_parsed))

    return label

def create_rtf_content(labels):
    rtf_header = r"""{\rtf1\ansi\deff3\adeflang1025
{\fonttbl{\f0\froman\fprq2\fcharset0 Times New Roman;}{\f1\froman\fprq2\fcharset2 Symbol;}{\f2\fswiss\fprq2\fcharset0 Arial;}{\f3\froman\fprq2\fcharset0 Liberation Serif{\*\falt Times New Roman};}{\f4\froman\fprq2\fcharset0 Arial;}{\f5\froman\fprq2\fcharset0 Tahoma;}{\f6\froman\fprq2\fcharset0 Times New Roman;}{\f7\froman\fprq2\fcharset0 Courier New;}{\f8\fnil\fprq2\fcharset0 Times New Roman;}{\f9\fnil\fprq2\fcharset0 Lohit Hindi;}{\f10\fnil\fprq2\fcharset0 DejaVu Sans;}}
{\colortbl;\red0\green0\blue0;\red0\green0\blue255;\red0\green255\blue255;\red0\green255\blue0;\red255\green0\blue255;\red255\green0\blue0;\red255\green255\blue0;\red255\green255\blue255;\red0\green0\blue128;\red0\green128\blue128;\red0\green128\blue0;\red128\green0\blue128;\red128\green0\blue0;\red128\green128\blue0;\red128\green128\blue128;\red192\green192\blue192;}
{\stylesheet{\s0\snext0\dbch\af8\langfe1081\dbch\af8\afs24\alang1081\ql\keep\nowidctlpar\sb0\sa720\ltrpar\hyphpar0\aspalpha\cf0\loch\f6\fs24\lang1033\kerning1 Normal;}
{\*\cs15\snext15\dbch\af10\langfe1033\afs24 Default Paragraph Font;}
{\s16\sbasedon0\snext17\dbch\af10\langfe1081\dbch\af8\afs28\ql\keep\nowidctlpar\sb240\sa120\keepn\ltrpar\cf0\loch\f4\fs28\lang1033\kerning1 Heading;}
{\s17\sbasedon0\snext17\dbch\af8\langfe1081\dbch\af8\afs24\ql\keep\nowidctlpar\sb0\sa120\ltrpar\cf0\loch\f6\fs24\lang1033\kerning1 Text Body;}
{\s18\sbasedon17\snext18\dbch\af8\langfe1081\dbch\af8\afs24\ql\keep\nowidctlpar\sb0\sa120\ltrpar\cf0\loch\f7\fs24\lang1033\kerning1 List;}
{\s19\sbasedon0\snext19\dbch\af9\langfe1081\dbch\af8\afs24\ai\ql\keep\nowidctlpar\sb120\sa120\ltrpar\cf0\loch\f6\fs24\lang1033\i\kerning1 Caption;}
{\s20\sbasedon0\snext20\dbch\af8\langfe1081\dbch\af8\afs24\ql\keep\nowidctlpar\sb0\sa720\ltrpar\cf0\loch\f7\fs24\lang1033\kerning1 Index;}
{\s21\sbasedon0\snext21\dbch\af8\langfe1081\dbch\af8\afs24\ai\ql\keep\nowidctlpar\sb120\sa120\ltrpar\cf0\loch\f7\fs24\lang1033\i\kerning1 caption;}
{\s22\sbasedon0\snext22\dbch\af8\langfe1081\dbch\af8\afs16\ql\keep\nowidctlpar\sb0\sa720\ltrpar\cf0\loch\f5\fs16\lang1033\kerning1 Balloon Text;}
{\s23\sbasedon0\snext23\dbch\af8\langfe1081\dbch\af8\afs24\ql\keep\nowidctlpar\sb0\sa720\ltrpar\cf0\loch\f6\fs24\lang1033\kerning1 Table Contents;}
{\s24\sbasedon23\snext24\dbch\af8\langfe1081\dbch\af8\afs24\ab\qc\keep\nowidctlpar\sb0\sa720\ltrpar\cf0\loch\f6\fs24\lang1033\b\kerning1 Table Heading;}
}
\formshade\paperh15840\paperw12240\margl360\margr360\margt360\margb360\sectd\sbknone\sectunlocked1\pgndec\pgwsxn12240\pghsxn15840\marglsxn360\margrsxn360\margtsxn360\margbsxn360\cols2\colsx720\ftnbj\ftnstart1\ftnrstcont\ftnnar\aenddoc\aftnrstcont\aftnstart1\aftnnrlc
\pard\plain \s0\dbch\af8\langfe1081\dbch\af8\afs24\alang1081\ql\keep\nowidctlpar\sb0\sa720\ltrpar\hyphpar0\aspalpha\cf0\loch\f6\fs24\lang1033\kerning1\ql\tx4320
"""
    rtf_footer = r"}"

    rtf_content = rtf_header

    try:
        for label in labels:
            for field, value in label:
                if field.startswith("iNat") or field.startswith("iNaturalist"):
                    first_char, rest = field[0], field[1:]
                    rtf_content += r"{\rtlch \ltrch\loch{\ul{\b " + first_char + r"}}}" + r"{\rtlch \ltrch\scaps\loch{\ul{\b " + rest + r":}}} " + str(value) + r"\line "
                elif field == "Scientific Name":
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} {\b\i " + str(value) + r"}\line "
                elif field == "GPS Coordinates":
                    # Directly replace the ± symbol with the RTF escape code
                    value_rtf = value.replace("±", r"\'b1")
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} " + value_rtf + r"\line "
                elif field == "Notes":
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} "
                    value = remove_formatting_tags(value)
                    value = escape_rtf(str(value))
                    value_rtf = str(value)
                    # Replace newlines with RTF line breaks
                    value_rtf = value_rtf.replace('\n', r'\line ')
                    value_rtf = value_rtf.replace('__BOLD_START__', r'{\b ').replace('__BOLD_END__', r'}')
                    value_rtf = value_rtf.replace('__ITALIC_START__', r'{\i ').replace('__ITALIC_END__', r'}')
                    # Remove the line about the MO to iNat import, as this isn't important on a label
                    value_rtf = re.sub(r'\\line Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value_rtf)
                    rtf_content += value_rtf + r"\line "
                else:
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} " + str(value) + r"\line "
            rtf_content += r"\par "

        rtf_content += rtf_footer
    except Exception as e:
        print(f"Error in create_rtf_content: {e}")
        return rtf_header + r"Error generating content" + rtf_footer

    return rtf_content

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create herbarium labels from iNaturalist observation numbers or URLs")
    parser.add_argument("observation_ids", nargs="+", help="Observation number(s) or URL(s)")
    parser.add_argument("--rtf", metavar="filename.rtf", help="Output to RTF file (filename must end with .rtf)")

    if len(sys.argv) > 1 and sys.argv[-1] == '--rtf':
        parser.error("argument --rtf: expected a filename ending in .rtf")

    args = parser.parse_args()

    if args.rtf and not args.rtf.lower().endswith('.rtf'):
        parser.error("argument --rtf: filename must end with .rtf")

    labels = []

    for input_value in args.observation_ids:
        observation_id = extract_observation_id(input_value)

        if observation_id is None:
            print(f"Error: Invalid input '{input_value}'. Please provide a valid observation number or URL.")
            continue

        observation_data = get_observation_data(observation_id)
        if observation_data:
            label = create_inaturalist_label(observation_data)
            labels.append(label)

    if args.rtf:
        rtf_content = create_rtf_content(labels)
        with open(args.rtf, 'w') as rtf_file:
            rtf_file.write(rtf_content)
        print(f"RTF file created: {args.rtf}")
    else:
        for label in labels:
            for field, value in label:
                if field == "Notes":
                    value = remove_formatting_tags(value)
                    # Remove the line about the MO to iNat import, as this isn't important on a label
                    value = re.sub(r'Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value)
                print(f"{field}: {value}")
            print("\n")  # Blank line between labels
