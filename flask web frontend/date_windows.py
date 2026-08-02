"""Pure date-window partitioning for capped observation searches."""

from collections import defaultdict
from datetime import date, datetime, timedelta


def _normalized_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except (TypeError, ValueError):
        return None


def build_date_windows(
    dated_counts,
    requested_start,
    requested_end,
    cap=500,
    *,
    newest_first=True,
):
    """Partition daily counts without splitting a calendar day.

    Invalid item dates and non-positive counts are ignored. Returned windows span
    from the first matching date through the last matching date; zero-count days
    between matching dates are included in the adjacent window boundaries.
    """
    start = _normalized_date(requested_start)
    end = _normalized_date(requested_end)
    if start is None or end is None or start > end:
        raise ValueError("requested_start and requested_end must be a valid date range")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ValueError("cap must be a positive integer")

    entries = dated_counts.items() if hasattr(dated_counts, "items") else dated_counts
    daily_counts = defaultdict(int)
    for entry in entries or ():
        try:
            raw_date, raw_count = entry
            item_date = _normalized_date(raw_date)
            item_count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if item_date is None or item_date < start or item_date > end or item_count < 1:
            continue
        daily_counts[item_date] += item_count

    if not daily_counts:
        return []

    windows = []
    first_match = min(daily_counts)
    last_match = max(daily_counts)
    window_start = first_match
    window_count = 0

    for item_date in sorted(daily_counts):
        item_count = daily_counts[item_date]

        if item_count > cap:
            if window_count:
                windows.append(
                    {
                        "start": window_start.isoformat(),
                        "end": (item_date - timedelta(days=1)).isoformat(),
                        "count": window_count,
                        "over_cap_single_day": False,
                    }
                )
            windows.append(
                {
                    "start": item_date.isoformat(),
                    "end": item_date.isoformat(),
                    "count": item_count,
                    "over_cap_single_day": True,
                }
            )
            window_start = item_date + timedelta(days=1)
            window_count = 0
            continue

        if window_count and window_count + item_count > cap:
            windows.append(
                {
                    "start": window_start.isoformat(),
                    "end": (item_date - timedelta(days=1)).isoformat(),
                    "count": window_count,
                    "over_cap_single_day": False,
                }
            )
            window_start = item_date
            window_count = item_count
        else:
            if not window_count and window_start > item_date:
                window_start = item_date
            window_count += item_count

    if window_count:
        windows.append(
            {
                "start": window_start.isoformat(),
                "end": last_match.isoformat(),
                "count": window_count,
                "over_cap_single_day": False,
            }
        )

    return list(reversed(windows)) if newest_first else windows
