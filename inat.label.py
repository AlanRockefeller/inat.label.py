#!/usr/bin/env python3

"""
iNaturalist Herbarium Label Generator

Author: Alan Rockefeller
Date: June 27, 2024
Version: 1.0

This script creates herbarium labels from iNaturalist observation numbers or URLs.
It fetches data from the iNaturalist API and formats it into printable labels suitable for
herbarium specimens.

Features:
- Supports multiple observation IDs or URLs as input
- Can output labels to the console or to an RTF file
- Includes various data fields such as scientific name, common name, location,
  GPS coordinates, observation date, observer, and more
- Handles special fields like DNA Barcode ITS, GenBank Accession Number, and
  Provisional Species Name when available

Usage:
1. Basic usage (output to console):
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...]

2. Output to RTF file:
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>

Examples:
- Generate label for a single observation:
  ./inat.label.py 12345

- Generate labels for multiple observations:
  ./inat.label.py 12345 67890 https://www.inaturalist.org/observations/11111

- Generate labels and save to an RTF file:
  ./inat.label.py 12345 67890 --rtf my_labels.rtf

Notes:
- If the observation is a section, for example Amanita sect. Phalloideae, the scientific name
  will just be Phalloideae - in that case the full scientific name will be in the Common name field.
- It is recommended to print herbarium labels on 100% cotton paper for maximum longevity.
- The RTF output is formatted to closely match the style of traditional herbarium labels.

Dependencies:
- requests
- dateutil

The dependencies can be installed with the following commands:

    pip install requests
    pip install python-dateutil

Python version 3.6 or higher is recommended.

"""

import requests
import sys
import os
from datetime import datetime
import json
from dateutil import parser as dateutil_parser
import re
import argparse

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

def get_coordinates(observation_data):
    if 'geojson' in observation_data and observation_data['geojson']:
        coordinates = observation_data['geojson']['coordinates']
        latitude = f"{coordinates[1]:.5f}"
        longitude = f"{coordinates[0]:.5f}"
        accuracy = observation_data.get('positional_accuracy')
        if accuracy:
            return f"{latitude}, {longitude}", accuracy
        else:
            return f"{latitude}, {longitude}", None
    return 'Not available', None

def parse_date(date_string):
    try:
        return dateutil_parser.parse(date_string)
    except ValueError:
        pass

    date_formats = [
        '%Y-%m-%d',
        '%Y/%m/%d %I:%M %p',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y/%m/%d',
        '%Y-%m-%d %H:%M:%S %z',
        '%Y-%m-%d %H:%M:%S',
        '%B %d, %Y',
    ]
    for format in date_formats:
        try:
            return datetime.strptime(date_string, format)
        except ValueError:
            continue

    try:
        return datetime.strptime(date_string.split()[0], '%Y-%m-%d')
    except ValueError:
        return None

def create_inaturalist_label(observation_data):
    obs_number = observation_data['id']
    url = f"https://www.inaturalist.org/observations/{obs_number}"

    taxon = observation_data.get('taxon', {})
    common_name = taxon.get('preferred_common_name', taxon.get('name', 'Not available'))
    scientific_name = observation_data.get('taxon', {}).get('name', 'Not available')

    location = observation_data.get('place_guess') or 'Not available'
    coords, accuracy = get_coordinates(observation_data)
    if accuracy:
        gps_coords = f"{coords} (±{accuracy}m)"
    else:
        gps_coords = coords

    date_observed = parse_date(observation_data['observed_on_string'])
    date_observed_str = date_observed.strftime('%Y-%m-%d') if date_observed else 'Not available'

    user = observation_data['user']
    display_name = user.get('name')
    login_name = user['login']
    observer = f"{display_name} ({login_name})" if display_name else login_name

    label = [
        ("Scientific Name", scientific_name),
        ("Common Name", common_name),
        ("iNat Observation Number", str(obs_number)),
        ("iNaturalist URL", url),
        ("Location", location),
        ("GPS Coordinates", gps_coords),
        ("Date Observed", date_observed_str),
        ("Observer", observer)
    ]

    dna_barcode_its = get_field_value(observation_data, 'DNA Barcode ITS')
    if dna_barcode_its:
        bp_count = len(dna_barcode_its)
        label.append(("DNA Barcode ITS", f"{bp_count} bp"))

    genbank_accession = get_field_value(observation_data, 'GenBank Accession Number')
    if not genbank_accession:
        genbank_accession = get_field_value(observation_data, 'GenBank Accession')
    if genbank_accession:
        label.append(("GenBank Accession Number", genbank_accession))

    provisional_name = get_field_value(observation_data, 'Provisional Species Name')
    if provisional_name:
        label.append(("Provisional Species Name", provisional_name))

    notes = observation_data.get('description') or 'No notes available'
    label.append(("Notes", notes))

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
                    # Replace ± with its ASCII code
                    value_rtf = str(value).replace("±", r"\'b1")
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} " + value_rtf + r"\line "
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
                print(f"{field}: {value}")
            print("\n\n")  # Blank line between labels
