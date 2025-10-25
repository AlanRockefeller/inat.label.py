# inat.label.py

# iNaturalist Herbarium Label Generator version 2.9
# By Alan Rockefeller
# October 24, 2025


## Description

The Herbarium Label Generator is a Python tool designed to create formatted herbarium labels from a iNaturalist and Mushroom Observer observations. This project rapidly creates professional quality labels for herbarium specimens.   It is designed to be robust, work on many different platforms and handle errors or unexpected input gracefully.

An easy to use online version is at https://images.mushroomobserver.org/labels

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Output](#output)
- [Dependencies](#dependencies)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)

## Features

- Fetches observation data using the iNaturalist / Mushroom Observer API.
- Supports multiple iNat or MO observation IDs or URLs as input - or a file can be specified.
- Uses parallel processing, intelligent rate limiting, and advanced caching for reliable and fast label generation.
- Generates labels with key information including:
  - Scientific Name (in italics)
  - Common Name (if different from scientific name)
  - iNaturalist / Mushroom Observer Observation Number
  - iNaturalist / Mushroom Observer URL 
  - Location (in text format)
  - GPS Coordinates (with accuracy - accuracy is set to 20km if observation geoprivacy is obscured)
  - Date Observed
  - Observer Name and iNaturalist login
  - DNA Barcode ITS, LSU, RPB1, RPB2 and TEF1 (if present)
  - GenBank Accession Number (if present)
  - Provisional Species Name (if present)
  - Species Name Override (if present - and it actually overrides the name on the label)
  - Mushroom Observer URL (if present, formatted in best URL form so it's accessible to users who aren't logged in)
  - Microscopy Performed (if present)
  - Traditional or Mobile Photography (if present)
  - Herbarium Catalog Number (if present)
  - Herbarium Name (if present)
  - Mycoportal ID (if present)
  - Voucher number(s) (if present)
  - Observation Notes
- By default outputs labels to console for quick viewing / testing
- Optionally creates RTF files for high-quality printing + QR code (RTF or PDF output is strongly recommended)
- Optionally creates PDF files for more compatibility
- Handles special characters and formatting (e.g., italics for scientific names, proper display of ± symbol)
- An optional command line switch can print out the iNaturalist URL's of observations which are in California.   This makes it easy to add these observations to the Mycomap CA Network project.
- Adds a QR code to the PDF and RTF labels which points to the iNaturalist URL
- When generating RTF labels it prints the iconic taxon along with the name - fungi in blue, plants in green and everything else in white.   This will help you quickly notice if an observation number is mistyped.

## Installation

Instead of installing this software, consider using the online version: https://images.mushroomobserver.org/labels


1. Clone this repository:
   ```bash
   git clone https://github.com/AlanRockefeller/inat.label.py
   ```

2. Navigate to the project directory:
   ```bash
   cd inat.label.py
   ```

3. Install the required dependencies:
   ```bash
   pip install requests python-dateutil beautifulsoup4 qrcode[pil] colorama replace-accents pillow reportlab requests-toolbelt
   ```

## Usage

Run the script from the command line, providing one or more iNaturalist observation IDs or URLs:

```bash
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...]
```

To generate an RTF file, use the `--rtf` option:

```
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>
```

To generate a PDF file, use the `--pdf` option:

```
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --pdf <filename.pdf>
```

To print out a list of URL's of observations that are in California, use the `--find-ca` option.    This was added to make it easy to add observations to the Mycomap CA Network project.   I paste the list of URL's into the Bulk URL Opener Chrome extension and add each tab to the project.   If there is an easier way, I haven't found it yet.

```
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --find-ca
```

### Examples:

1. Generate label for a single observation:
   ```
   python3 inat.label.py 183905751
   ```

2. Generate labels for multiple observations:
   ```
   python3 inat.label.py 183905751 147249599 https://www.inaturalist.org/observations/106191917 MO505283
   ```

3. Generate labels and save to an RTF file:
   ```
   python3 inat.label.py 183905751 147249599 --rtf two_labels.rtf
   ```

## Output

The script generates herbarium labels to the standard output by default, or labels are written to an RTF file if the --rtf command line argument is given.   RTF labels look much more professional when printed and include QR codes - the standard output is mostly for testing.

## Dependencies

- Python 3.6+
- requests
- requests-toolbelt
- python-dateutil
- beautifulsoup4
- qrcode[pil]
- colorama
- replace-accents
- pillow
- reportlab

## Contributing

Contributions to the iNaturalist Herbarium Label Generator are welcome! Here's how you can contribute:

1. Fork the repository
2. Create a new branch (`git checkout -b feature/AmazingFeature`)
3. Make your changes
4. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
5. Push to the branch (`git push origin feature/AmazingFeature`)
6. Open a Pull Request
7. Contact Alan Rockefeller via email, old fashioned phone call or messenger of your choice

Or just contact me with suggestions.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

## Contact

Alan Rockefeller - My email address is my full name at gmail, or message me on Facebook, Linkedin or Instagram

Project Link: [https://github.com/AlanRockefeller/inat.label.py](https://github.com/AlanRockefeller/inat.label.py)
