# inat.label.py

# iNaturalist Herbarium Label Generator

## Description

The iNaturalist Herbarium Label Generator is a powerful Python tool designed to create formatted herbarium labels from iNaturalist observation data. This project streamlines the process of generating professional quality labels for herbarium specimens.

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

- Fetches observation data directly from the iNaturalist API
- Supports multiple observation IDs or URLs as input
- Generates labels with key information including:
  - Scientific Name (in italics)
  - Common Name
  - iNaturalist Observation Number
  - iNaturalist URL
  - Location
  - GPS Coordinates (with accuracy)
  - Date Observed
  - Observer Name
  - DNA Barcode ITS (if available)
  - GenBank Accession Number (if available)
  - Provisional Species Name (if available)
  - Notes
- Outputs labels to console for quick viewing
- Creates formatted RTF files for high-quality printing
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
   pip install requests python-dateutil
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

The script generates herbarium labels with the following information:

- Scientific Name (in italics)
- Common Name
- iNaturalist Observation Number
- iNaturalist URL
- Location
- GPS Coordinates (with accuracy if available)
- Date Observed
- Observer Name
- DNA Barcode ITS (if available)
- GenBank Accession Number (if available)
- Provisional Species Name (if available)
- Observation Notes

When using the RTF output option, the labels are formatted for optimal readability and professional appearance.

## Dependencies

- Python 3.6+
- requests
- python-dateutil

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

