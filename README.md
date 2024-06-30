# inat.label.py

# iNaturalist Herbarium Label Generator version 1.4
# By Alan Rockefeller
# June 29, 2024

## Description

The iNaturalist Herbarium Label Generator is a powerful Python tool designed to create formatted herbarium labels from iNaturalist observation data. This project streamlines the process of generating professional quality labels for herbarium specimens.   It is designed to be robust and work on many different platforms.

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

- Fetches observation data using the iNaturalist API
- Supports multiple observation IDs or URLs as input
- Generates labels with key information including:
  - Scientific Name (in italics)
  - Common Name (if different from scientific name)
  - iNaturalist Observation Number
  - iNaturalist URL (in case the herbarium manager doesn't know how to create iNaturalist URL's)
  - Location (in text format)
  - GPS Coordinates (with accuracy - accuracy is set to 20km if observation geoprivacy is obscured)
  - Date Observed
  - Observer Name and iNaturalist login
  - DNA Barcode ITS, LSU, RPB1, RPB2 and TEF1 (if present)
  - GenBank Accession Number (if present)
  - Provisional Species Name (if present)
  - Mushroom Observer URL (if present, formatted in simplest possible URL form)
  - Microscopy Performed (if present)
  - Traditional or Mobile Photography (if present)
  - Observation Notes
- Outputs labels to console for quick viewing
- Optionally creates formatted RTF files for high-quality printing
- Handles special characters and formatting (e.g., italics for scientific names, proper display of ± symbol)

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/yourusername/inat-herbarium-label-generator.git
   ```

2. Navigate to the project directory:
   ```
   cd inat-herbarium-label-generator
   ```

3. Install the required dependencies:
   ```
   pip install requests python-dateutil beautifulsoup4
   ```

## Usage

Run the script from the command line, providing one or more iNaturalist observation IDs or URLs:

```
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...]
```

To generate an RTF file, use the `--rtf` option:

```
python inat.label.py <observation_number_or_url> [<observation_number_or_url> ...] --rtf <filename.rtf>
```

### Examples:

1. Generate label for a single observation:
   ```
   python3 inat.label.py 183905751
   ```

2. Generate labels for multiple observations:
   ```
   python3 inat.label.py 183905751 147249599 https://www.inaturalist.org/observations/106191917
   ```

3. Generate labels and save to an RTF file:
   ```
   python3 inat.label.py 183905751 147249599 --rtf two_labels.rtf
   ```

## Output

The script generates herbarium labels to the standard output by default, or labels are written to a RTF file if the --rtf command line argument is given.   RTF labels generally look more professional when printed.

## Dependencies

- Python 3.6+
- requests
- python-dateutil
- beautifulsoup4

## Contributing

Contributions to the iNaturalist Herbarium Label Generator are welcome! Here's how you can contribute:

1. Fork the repository
2. Create a new branch (`git checkout -b feature/AmazingFeature`)
3. Make your changes
4. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
5. Push to the branch (`git push origin feature/AmazingFeature`)
6. Open a Pull Request
7. Contact Alan Rockefeller via email, old fashioned phone call or messenger of your choice


## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

## Contact

Alan Rockefeller - My email address is my full name at gmail, or message me on Facebook or Instagram

Project Link: [https://github.com/AlanRockefeller/inat-herbarium-label-generator](https://github.com/AlanRockefeller/inat-herbarium-label-generator)

