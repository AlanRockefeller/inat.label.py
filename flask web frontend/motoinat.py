#!/usr/bin/python3

# Convert Mushroom Observer observation numbers to iNaturalist observation numbers

# Version 1.1 - Alan Rockefeller - May 15, 2025

# Usage: motoinat.py 12345
# Usage: motoinat.py -q --file input-numbers.txt

import sys
import requests
import argparse
import json

# The web frontend ships inat_shared.py next to this script so conversions obey
# the same cross-process iNaturalist pacing and cooldowns as lookups and print
# jobs.  Standalone use works without it.
try:
    import inat_shared
except ImportError:
    inat_shared = None

# (connect, read) seconds; requests otherwise waits indefinitely.
REQUEST_TIMEOUT = (5, 15)


def _inat_get(url, params):
    """GET an iNaturalist URL, paced and cooled down through inat_shared if present."""
    if inat_shared is not None:
        inat_shared.wait_turn(max_wait=5)
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if response.status_code == 429 and inat_shared is not None:
        inat_shared.cooldown(inat_shared.retry_after(response))
    return response


def find_inaturalist_observation(
    mo_number, debug=False, url_only=False, number_only=False
):
    """Print the matching iNaturalist observation.

    Returns True when a match was found or every query succeeded without one,
    and False when a query failed, so a failure is never reported as "no match".
    """
    base_url = "https://api.inaturalist.org/v1/observations"

    # All possible URL formats that might be stored in iNaturalist
    mo_urls = [
        f"http://mushroomobserver.org/observer/show_observation/{mo_number}",
        f"https://mushroomobserver.org/observer/show_observation/{mo_number}",
        f"http://mushroomobserver.org/{mo_number}",
        f"https://mushroomobserver.org/{mo_number}",
        f"http://mushroomobserver.org/obs/{mo_number}",
        f"https://mushroomobserver.org/obs/{mo_number}",
    ]

    failed = False
    for mo_url in mo_urls:
        params = {"field:Mushroom Observer URL": mo_url, "verifiable": "any"}

        if debug:
            print(f"\n--- DEBUG: Request Details ---")
            print(f"Base URL: {base_url}")
            print(f"Mushroom Observer URL: {mo_url}")
            print(f"Parameters: {json.dumps(params, indent=2)}")

        try:
            response = _inat_get(base_url, params)
        except requests.exceptions.RequestException as exc:
            failed = True
            print(
                f"Error querying iNaturalist for Mushroom Observer #{mo_number}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue

        if debug:
            print(f"\n--- DEBUG: Response Details ---")
            print(f"Status Code: {response.status_code}")
            print(f"Response Headers: {json.dumps(dict(response.headers), indent=2)}")

        if response.status_code == 200:
            data = response.json()

            if debug:
                print(f"\n--- DEBUG: Response Data ---")
                print(json.dumps(data, indent=2))

            if data["total_results"] > 0:
                obs = data["results"][0]
                inat_url = f"https://www.inaturalist.org/observations/{obs['id']}"
                if number_only:
                    print(obs["id"])
                elif url_only:
                    print(inat_url)
                else:
                    print(f"\nMushroom Observer #{mo_number}:")
                    print(f"  iNaturalist Observation: {inat_url}")
                    print(f"  Species: {obs['species_guess']}")
                    print(f"  Location: {obs['place_guess']}")
                    print(f"  Matched URL: {mo_url}")
                    print()
                return True  # Exit the function after finding a match
        else:
            failed = True
            if debug:
                print(
                    f"Error fetching data for Mushroom Observer #{mo_number} with URL {mo_url}"
                )

    if failed:
        print(
            f"Could not check every URL form for Mushroom Observer #{mo_number}; "
            "iNaturalist lookup failed.",
            file=sys.stderr,
        )
        return False

    # If we get here, no match was found after trying all URL formats
    if url_only or number_only:
        print(
            f"Mushroom Observer #{mo_number} has no iNaturalist observation associated with it."
        )
    else:
        print(f"No iNaturalist observation found for Mushroom Observer #{mo_number}")
        print()
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find iNaturalist observations for Mushroom Observer numbers."
    )
    parser.add_argument(
        "mo_numbers", nargs="*", help="One or more Mushroom Observer numbers"
    )
    parser.add_argument(
        "--file", type=str, help="File containing Mushroom Observer numbers"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument(
        "--url", action="store_true", help="Output only the iNaturalist URL"
    )
    parser.add_argument(
        "-q", action="store_true", help="Output only the iNaturalist observation number"
    )
    args = parser.parse_args()

    mo_numbers = args.mo_numbers

    if args.file:
        try:
            with open(args.file, "r") as file:
                file_content = file.read()
                mo_numbers.extend(file_content.split())
        except FileNotFoundError:
            print(f"Error: File {args.file} not found.")
            sys.exit(1)

    # Ensure all numbers are numeric
    mo_numbers = [num for num in mo_numbers if num.isdigit()]
    if not mo_numbers:
        print("Error: No Mushroom Observer numbers provided.")
        print(
            "Usage: python script.py [mo_numbers] [--file FILE] [--debug] [--url] [-q]"
        )
        sys.exit(1)

    all_ok = True
    for mo_number in mo_numbers:
        if not find_inaturalist_observation(mo_number, args.debug, args.url, args.q):
            all_ok = False
    sys.exit(0 if all_ok else 1)
