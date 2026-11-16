#!/usr/bin/env python3
import os
import datetime
import pandas as pd

import matplotlib

matplotlib.use("Agg")  # important for cron/headless runs
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

import usage_stats
from usage_stats import (  # noqa: F401  (re-exported for existing callers)
    PDF_BYTES_PER_LABEL,
    RTF_BYTES_PER_LABEL,
    estimate_label_count,
)


# --- Paths (absolute, relative to this script) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

JOBS_DIR = usage_stats.JOBS_DIR
LEDGER_PATH = usage_stats.LEDGER_PATH
OUTPUT_GRAPH_PATH = os.path.join(BASE_DIR, "static", "usage_graph.jpg")


def analyze_jobs() -> pd.DataFrame | None:
    """Aggregates daily stats from the usage ledger and live job directories.

    Job directories are pruned after a retention window, so days older than that
    come from the ledger written at prune time and recent days come from the
    directories still on disk.  A directory is counted by exactly one of the two
    sources, because folding into the ledger and deleting happen together.
    """
    daily_data = usage_stats.daily_usage(jobs_dir=JOBS_DIR, ledger_path=LEDGER_PATH)

    if not daily_data:
        print("No job data found to generate a graph.")
        return None

    df = pd.DataFrame(daily_data)
    df["date"] = pd.to_datetime(df["date"])
    daily_summary = df.groupby("date", as_index=False).sum()

    # Fill in missing days with zeros so the chart is continuous
    date_range = pd.date_range(
        start=daily_summary["date"].min(),
        end=daily_summary["date"].max(),
        freq="D",
    )
    daily_summary = (
        daily_summary.set_index("date")
        .reindex(date_range)
        .fillna(0)
        .reset_index()
        .rename(columns={"index": "date"})
    )

    daily_summary["jobs"] = daily_summary["jobs"].astype(int)
    daily_summary["labels"] = daily_summary["labels"].astype(int)

    return daily_summary


def create_usage_graph(df: pd.DataFrame) -> None:
    """Generates and saves the usage graph."""
    if df is None or df.empty:
        return

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    # 7-day average for labels (line)
    df["labels_7d_avg"] = df["labels"].rolling(7, min_periods=1).mean()

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, ax1 = plt.subplots(figsize=(14, 8))

    # Bars (daily, thin is fine)
    bar_width = 0.4
    dates = df["date"]

    # Jobs per day
    ax1.bar(
        dates - datetime.timedelta(days=bar_width / 2),
        df["jobs"],
        bar_width,
        label="Jobs per Day",
        alpha=0.8,
    )

    # Labels per day
    ax1.bar(
        dates + datetime.timedelta(days=bar_width / 2),
        df["labels"],
        bar_width,
        label="Labels per Day",
        alpha=0.8,
    )

    # 7-day average line for labels
    ax1.plot(
        dates,
        df["labels_7d_avg"],
        linewidth=2,
        label="Labels (7-day avg)",
    )

    ax1.set_title("Daily Label Generation", fontsize=20, pad=20)
    ax1.set_ylabel("Labels", fontsize=14)
    ax1.set_xlabel("Date", fontsize=14)

    # Y-axis: start at 0; integer ticks
    max_y = max(df["jobs"].max(), df["labels"].max(), float(df["labels_7d_avg"].max()))
    ax1.set_ylim(0, max_y * 1.15 if max_y > 0 else 1)
    ax1.get_yaxis().set_major_locator(plt.MaxNLocator(integer=True))

    # --- X axis labels ---
    # Use weekday-based ticks so the "next tick" after max date is less likely to appear on the right.
    num_days = int((df["date"].max() - df["date"].min()).days)

    if num_days <= 120:
        # weekly labels (Mondays)
        ax1.xaxis.set_major_locator(
            mdates.WeekdayLocator(byweekday=mdates.MO, interval=1)
        )
    elif num_days <= 240:
        # every 2 weeks (Mondays)
        ax1.xaxis.set_major_locator(
            mdates.WeekdayLocator(byweekday=mdates.MO, interval=2)
        )
    else:
        # monthly labels
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=1))

    # Month/day normally, but:
    # - first visible tick includes year
    # - first tick of each subsequent year includes year
    # Matplotlib formats the ticks several times (get_xticklabels, layout and
    # rendering), so decide from the tick position alone, never retained state.
    def _fmt(x, pos=None):
        dt = mdates.num2date(x)  # timezone-handled by matplotlib
        if not pos:
            return dt.strftime("%Y %b %d")
        locs = ax1.xaxis.get_majorticklocs()
        if pos < len(locs) and mdates.num2date(locs[pos - 1]).year != dt.year:
            return dt.strftime("%Y %b %d")
        return dt.strftime("%b %d")

    ax1.xaxis.set_major_formatter(FuncFormatter(_fmt))
    plt.setp(ax1.get_xticklabels(), rotation=45, ha="right")

    # Use more of the canvas for bars: remove default x-margins and set tight x-limits.
    # Need a little padding so the left/right shifted bars aren't clipped.
    pad = datetime.timedelta(days=0.65)  # > (shift 0.2 + half-width 0.2)
    ax1.set_xlim(df["date"].min() - pad, df["date"].max() + pad)
    ax1.margins(x=0)

    ax1.tick_params(axis="both", which="major", labelsize=12)
    ax1.legend(loc="upper left", fontsize=12)

    fig.tight_layout()
    fig.subplots_adjust(bottom=0.18)

    os.makedirs(os.path.dirname(OUTPUT_GRAPH_PATH), exist_ok=True)

    try:
        plt.savefig(OUTPUT_GRAPH_PATH, dpi=120, bbox_inches="tight")
        print(f"Graph successfully saved to {OUTPUT_GRAPH_PATH}")
    except Exception as e:
        print(f"Error saving graph: {e}")


def main() -> None:
    print("Starting usage analysis...")
    daily_stats = analyze_jobs()
    if daily_stats is not None and not daily_stats.empty:
        create_usage_graph(daily_stats)
    else:
        print("Could not generate graph due to lack of data or an error.")
    print("Analysis complete.")


if __name__ == "__main__":
    main()
