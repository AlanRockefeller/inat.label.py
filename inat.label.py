#!/usr/bin/env python3

"""
iNaturalist Herbarium Label Generator

Author: Alan Rockefeller
Date:March 15, 2025
Version: 2.2


This script creates herbarium labels from iNaturalist observation numbers or URLs.
It fetches data from the iNaturalist API and formats it into printable labels suitable for
herbarium specimens.  While it can output the labels to stdout, the RTF output makes more
professional looking labels that include a QR code.

Features:
- Supports multiple observation IDs or URLs as input
- Can output labels to the console or to an RTF file
- Includes various data fields such as scientific name, common name, location,
  GPS coordinates, observation date, observer, and more
- Handles special fields like DNA Barcode ITS (and LSU, TEF1, RPB1, RPB2), GenBank Accession Number,
  Provisional Species Name, Mobile or Traditional Photography?, Microscopy Performed, Herbarium Catalog Number,
  Herbarium Name, Mycoportal ID, Voucher number(s) and Mushroom Observer URL when available
- Generates a QR code which links to the iNaturalist URL

Usage:
1. Basic usage (output to console - mostly just for testing):
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...]

2. Output to RTF file: (recommended - much better formatting and adds a QR code)
   ./inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>

Examples:
- Generate label for a single observation:
  ./inat.label.py 150291663

- Generate labels for multiple observations:
  ./inat.label.py 150291663 62240372 https://www.inaturalist.org/observations/105658809

- Generate labels and save to an RTF file:
  ./inat.label.py 150291663 62240372 --rtf two_labels.rtf

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

    pip install requests python-dateutil beautifulsoup4 qrcode[pil] colorama replace-accents pillow

Python version 3.6 or higher is recommended.

"""

import argparse
import colorama
import datetime
import re
import sys
import time
import unicodedata
from io import BytesIO
import requests
from replace_accents import replace_accents_characters
import binascii
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
import qrcode
from PIL import Image


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
        '"': '\\ldblquote ',
        '"': '\\rdblquote ',
        ''': '\\lquote ',
        ''': '\\rquote ',
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
        tag.replace_with('__BOLD_START__' + tag.string + '__BOLD_END__')
    for tag in soup.find_all(['em', 'i']):
        tag.replace_with('__ITALIC_START__' + tag.string + '__ITALIC_END__')
    for tag in soup.find_all(['ins', 'u']):
        tag.replace_with('' + tag.string + '')

    processed_text = str(soup).strip()
    return processed_text

def normalize_string(s):
    return unicodedata.normalize('NFKD', s.strip().lower())

def extract_observation_id(input_string, debug = False):
    # Check if the input is a URL
    url_match = re.search(r'observations/(\d+)', input_string)
    if url_match:
        return url_match.group(1)

    # Check if the input is a number
    if input_string.isdigit():
        return input_string

    # If neither, return None
    return None

def get_taxon_details(taxon_id):
    """Fetch detailed information about a taxon, including its ancestors."""
    url = f"https://api.inaturalist.org/v1/taxa/{taxon_id}"
    try:
        # Add timeout to prevent hanging indefinitely
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data['results']:
                return data['results'][0]
        elif response.status_code == 429:
            print(f"Rate limit exceeded when fetching taxon details. Waiting 5 seconds before retry...")
            time.sleep(5)  # Wait 5 seconds before next request
            return get_taxon_details(taxon_id)  # Retry
        else:
            print(f"Warning: Received status code {response.status_code} when fetching taxon {taxon_id}")
            
    except requests.exceptions.Timeout:
        print(f"Timeout error when fetching taxon {taxon_id}. Continuing without detailed taxon information.")
    except requests.exceptions.RequestException as e:
        print(f"Network error when fetching taxon {taxon_id}: {str(e)}. Continuing without detailed taxon information.")
    except Exception as e:
        print(f"Unexpected error when fetching taxon {taxon_id}: {str(e)}. Continuing without detailed taxon information.")
        
    return None

def get_observation_data(observation_id):
    url = f"https://api.inaturalist.org/v1/observations/{observation_id}"
    try:
        # Add timeout to prevent hanging indefinitely
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data['results']:
                observation = data['results'][0]
                taxon = observation.get('taxon', {})
                iconic_taxon_name = taxon.get('iconic_taxon_name') if taxon else 'Life'
                
                # If we have a taxon, fetch its complete taxonomic data
                if taxon and 'id' in taxon:
                    taxon_id = taxon['id']
                    taxon_details = get_taxon_details(taxon_id)
                    if taxon_details:
                        observation['taxon_details'] = taxon_details
                
                return observation, iconic_taxon_name
            else:
                print(f"Error: Observation {observation_id} does not exist.")
                return None, 'Life'
        elif response.status_code == 429:
            print(f"Rate limit exceeded. Waiting 5 seconds before retry...")
            time.sleep(5)  # Wait 5 seconds before next request
            return get_observation_data(observation_id)  # Retry
        else:
            print(f"Error: Unable to fetch data for observation {observation_id}. Status code: {response.status_code}")
            return None, 'Life'
            
    except requests.exceptions.Timeout:
        print(f"Timeout error when fetching observation {observation_id}. Skipping.")
        return None, 'Life'
    except requests.exceptions.RequestException as e:
        print(f"Network error when fetching observation {observation_id}: {str(e)}. Skipping.")
        return None, 'Life'
    except Exception as e:
        print(f"Unexpected error when fetching observation {observation_id}: {str(e)}. Skipping.")
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
    date_part = getattr(date_string, 'split', lambda x: [' '])()[0]

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

def create_inaturalist_label(observation_data, iconic_taxon_name):
    obs_number = observation_data['id']
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
    location = re.sub(r'\b\d{5}\b,?\s*', '', location)

    #If the location is long, remove the first part of the location (usually a street address)
    if len(location) > 40:
        comma_index = location.find(',')
        if comma_index != -1:
         location = location[comma_index + 1:].strip()


    # Remove unusual characters if we are in rtf mode - rtf readers don't handle these well
    if args.rtf:
        location = replace_accents_characters(location)

    coords, accuracy = get_coordinates(observation_data)
    gps_coords = f"{coords} (±{accuracy}m)" if accuracy else coords

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
    label.extend([
    ("iNat Observation Number", str(obs_number)),
    ("iNaturalist URL", url),
    ("Location", location),
    ("GPS Coordinates", gps_coords),
    ("Date Observed", date_observed_str),
    ("Observer", observer)
])

    # Include these fields only if they are populated
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

    # Include Genbank accession number whether it's in Genbank Accession or Genbank Accession Number observation field
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
        label.append(("Voucher number(s)", voucher_numbers))

    mushroom_observer_url = get_field_value(observation_data, 'Mushroom Observer URL')
    if mushroom_observer_url:
        # Format Mushroom Observer URL in the shortest possible way
        formatted_url = format_mushroom_observer_url(mushroom_observer_url)
        label.append(("Mushroom Observer URL", formatted_url))

    notes = observation_data.get('description') or ''
    # Convert HTML in notes field to text
    notes_parsed = parse_html_notes(notes)
    label.append(("Notes", notes_parsed))

    return label, iconic_taxon_name

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
        for label, iconic_taxon_name in labels:
            inat_url = next((value for field, value in label if field == "iNaturalist URL"), None)

            for field, value in label:
                if field.startswith("iNat") or field.startswith("iNaturalist"):
                    first_char, rest = field[0], field[1:]
                    rtf_content += r"{\rtlch \ltrch\loch{\ul{\b " + first_char + r"}}}" + r"{\rtlch \ltrch\scaps\loch{\ul{\b " + rest + r":}}} " + str(value) + r"\line "
                elif field == "Scientific Name":
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} {\b\i " + str(value) + r"}\line "
                    # Tell the user which species is being added to the label on stdout.   Fungi in blue, plants in green, everything else in white.
                    colorama.init()
                    if iconic_taxon_name == "Fungi":
                        print(f"\033[94mAdded label for {iconic_taxon_name}\033[0m {value}")
                    elif iconic_taxon_name == "Plantae":
                        print(f"\033[92mAdded label for {iconic_taxon_name}\033[0m {value}")
                    else:
                        print(f"Added label for {iconic_taxon_name} {value}")
                elif field == "GPS Coordinates":
                    # Replace the ± symbol with the RTF escape code
                    value_rtf = value.replace("±", r"\'b1")
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} " + value_rtf + r"\line "
                elif field == "Notes":
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} "
                    value = escape_rtf(str(value))
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
                    rtf_content += value_rtf + r"\line \tab"
                else:
                    rtf_content += r"{\rtlch \ltrch\scaps\loch{\ul{\b " + field + r":}}} " + str(value) + r"\line "

            def split_hex_string(s, n):
                # Split hex string into lines of n characters
                return '\n'.join([s[i:i+n] for i in range(0, len(s), n)])

            # Add the QR code to the label

            # Save QR to a png file for debugging
            # qr_filename = f"qr_code_{label_index}.png"
            qr_hex, qr_size = generate_qr_code(inat_url)
            # os.remove(qr_filename)

            if qr_hex:
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

                # print(f"QR code embedded successfully.")
            else:
                print("Failed to generate QR code.")

            # Add some vertical space between labels
            rtf_content += r"\par "

        rtf_content += rtf_footer
    except Exception as e:
        print(f"Error in create_rtf_content: {e}")
        return rtf_header + r"Error generating content" + rtf_footer

    return rtf_content

# Check to see if the observation is in California,
def is_within_california(latitude, longitude):
    # Approximate bounding box for California
    CA_NORTH = 42.0
    CA_SOUTH = 32.5
    CA_WEST = -124.4
    CA_EAST = -114.1

    return (CA_SOUTH <= latitude <= CA_NORTH) and (CA_WEST <= longitude <= CA_EAST)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create herbarium labels from iNaturalist observation numbers or URLs")
    parser.add_argument("observation_ids", nargs="*", help="Observation number(s) or URL(s)")
    parser.add_argument("--file", metavar="filename", help="File containing observation numbers or URLs (separated by spaces, commas, or newlines)")
    parser.add_argument("--rtf", metavar="filename.rtf", help="Output to RTF file (filename must end with .rtf)")
    parser.add_argument("--find-ca", action="store_true", help="Find observations within California")
    parser.add_argument('--debug', action='store_true', help='Print debug output')

    args = parser.parse_args()

    # Suggested by James Chelin to fix a bug that caused large jobs to crash when called from cron
    sys.setrecursionlimit(100000)

    # If no arguments are provided, show help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    if args.rtf and not args.rtf.lower().endswith('.rtf'):
        parser.error("argument --rtf: filename must end with .rtf")

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
    request_count = 0

    for input_value in observation_ids:
        observation_id = extract_observation_id(input_value, debug=args.debug)

        if observation_id is None:
            print(f"Error: Invalid input '{input_value}'. Please provide a valid observation number or URL.")
            continue

        # Add delay if more than 20 requests
        if request_count >= 20:
            time.sleep(1)  # 1 second delay

        result = get_observation_data(observation_id)
        observation, iconic_taxon_name = result

        request_count += 1  # Increment the request counter

        if result is None:
            continue  # Skip to the next observation if there was an error

        observation_data, iconic_taxon_name = result

        # If the --find-ca command line option is given, only print out URL's of California observations
        if args.find_ca:
            if 'geojson' in observation_data and observation_data['geojson']:
                coordinates = observation_data['geojson']['coordinates']
                latitude, longitude = coordinates[1], coordinates[0]
                if is_within_california(latitude, longitude):
                    print(f"https://www.inaturalist.org/observations/{observation_id}")
        # Otherwise create the label
        else:
            label, iconic_taxon_name = create_inaturalist_label(observation_data, iconic_taxon_name)
            labels.append((label, iconic_taxon_name))

    if not args.find_ca:
        if labels:
            if args.rtf:
                rtf_content = create_rtf_content(labels)
                with open(args.rtf, 'w') as rtf_file:
                    rtf_file.write(rtf_content)
                print(f"RTF file created: {args.rtf}")
            else:
                # Print labels to stdout
                for label, _ in labels:
                    for field, value in label:
                        if field == "Notes":
                            value = remove_formatting_tags(value)
                            value = re.sub(r'Originally posted to Mushroom Observer on [A-Za-z]+\. \d{1,2}, \d{4}\.', '', value)
                            value = re.sub(r'Imported by Mushroom Observer \d{4}-\d{2}-\d{2}', '', value)
                        print(f"{field}: {value}")
                    print("\n")  # Blank line between labels
        else:
            print("No valid observations found.")
