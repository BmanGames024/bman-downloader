"""Check every game in Bman Downloader's archive.org sources against the cover repo
(github.com/BmanGames024/OG-Xbox-and-360-Cover-Art), using the app's own matching.

Put this next to bman_core.py and run:
    python check_covers.py
It uses the app's saved game list (core\\index_cache.json) and downloads a fresh one
from archive.org only if there is none (add --refresh to always download it).

Writes, next to this script:
    cover_check_missing.csv   games that get NO cover at all
    cover_check_review.csv    games matched only by a weak name match - worth a look
    cover_check_all.csv       every game: which cover it gets and how
and prints a summary per source.
"""
from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bman_core as core                                    # noqa: E402

REVIEW_BELOW = 0.6        # name matches scoring under this go in the review list


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="download the game lists from archive.org again")
    ap.add_argument("--review-below", type=float, default=REVIEW_BELOW,
                    help=f"name-match score under which a cover goes in the review list (default {REVIEW_BELOW})")
    args = ap.parse_args()

    print("Loading the game list...")
    index = core.Index.load_combined(use_cache=not args.refresh)
    if args.refresh or index.status in ("offline", "stale"):
        print("Downloading the game lists from archive.org (this can take a minute)...")
        index.refresh()
    if index.failed_sources:
        print(f"   couldn't load: {', '.join(index.failed_sources)}")
    print(f"   {len(index.entries)} files in {len({e.archive_identifier for e in index.entries})} sources")

    if core._cover_index() is None:
        print("Couldn't get the cover list from GitHub - check the internet connection.")
        return 1

    rows, seen = [], {}
    for entry in index.entries:
        og = core.is_og_xbox_entry(entry)
        dlc = core.is_dlc_entry(entry)
        if not core.cover_search_name(entry.name, dlc=dlc) and not core.catalog_title_id(entry.name):
            continue                                        # not a game file (pictures, metadata...)
        key = (entry.name, og, dlc)
        if key not in seen:
            seen[key] = core.cover_match(entry.name, og, dlc)
        path, how, score = seen[key]
        rows.append({
            "source": entry.archive_identifier or "",
            "console": "Original Xbox" if og else "Xbox 360",
            "kind": "DLC" if dlc else ("Content" if core.entry_is_content(entry) else "Game"),
            "file": entry.name,
            "cover": path.rsplit("/", 1)[-1] if path else "",
            "how": how if path else "no cover",
            "score": f"{score:.2f}" if path else "",
        })

    rows.sort(key=lambda r: (r["source"], r["file"].lower()))
    fields = ["source", "console", "kind", "file", "cover", "how", "score"]

    def write(name, subset):
        with (HERE / name).open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(subset)

    missing = [r for r in rows if not r["cover"]]
    review = [r for r in rows if r["cover"] and r["how"] == "name match" and float(r["score"]) < args.review_below]
    write("cover_check_all.csv", rows)
    write("cover_check_missing.csv", missing)
    write("cover_check_review.csv", sorted(review, key=lambda r: float(r["score"])))

    print(f"\n{'source':40s} {'files':>6s} {'cover':>6s} {'none':>5s} {'review':>7s}")
    by_source = collections.defaultdict(list)
    for r in rows:
        by_source[r["source"]].append(r)
    for source in sorted(by_source):
        rs = by_source[source]
        n_missing = sum(1 for r in rs if not r["cover"])
        n_review = sum(1 for r in rs if r in review)
        print(f"{source[:40]:40s} {len(rs):6d} {len(rs) - n_missing:6d} {n_missing:5d} {n_review:7d}")
    print(f"{'TOTAL':40s} {len(rows):6d} {len(rows) - len(missing):6d} {len(missing):5d} {len(review):7d}")
    print(f"\nWrote cover_check_missing.csv ({len(missing)}), cover_check_review.csv ({len(review)}) "
          f"and cover_check_all.csv ({len(rows)}) next to this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
