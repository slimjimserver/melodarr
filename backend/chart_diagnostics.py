"""Server-side chart refresh and per-user disposition report (no HTTP route)."""

import argparse
import json
from collections import Counter

from .api_cache import init_cache_db
from .recommendation_feed import current_feed
from .services import charts
from .storage import get_recommendation_cache

EXCLUSION_REASONS = ("plex_mbid", "plex_name", "lidarr_available", "requested",
                     "lidarr_search_pending", "download_pending", "dismissed", "featured_elsewhere")


def build_report(country, user_id=None):
    snapshot = charts.cached_chart_diagnostics(country)
    if snapshot is None:
        raise RuntimeError("No chart diagnostic snapshot exists; run with --refresh first.")
    rows = [dict(row) for row in snapshot["rows"]]
    outcomes = {}
    if user_id is not None:
        chart_items = charts.cached_popular_albums(country)["items"]
        expected = {(row["rank"], row.get("matchedMbid")) for row in rows
                    if row["resolution"] == "matched"}
        actual = {(item.get("chartRank"), item["id"]) for item in chart_items}
        if actual != expected:
            raise RuntimeError("Chart candidates and diagnostic rows are from different refreshes.")
        cached = get_recommendation_cache(user_id)
        payload = json.loads(cached["value"]) if cached else {}
        current_feed(user_id, charts.with_cached_charts(payload), chart_trace=outcomes)
    for row in rows:
        outcome = outcomes.get((country, row["rank"]))
        if outcome:
            row.update(outcome)
        row["finalDisposition"] = (outcome["disposition"] if outcome else
                                   "matched_not_personally_evaluated" if row["resolution"] == "matched" else
                                   row["resolution"])
    resolution = Counter(row["resolution"] for row in rows)
    methods = Counter(row.get("matchMethod", "exact") for row in rows if row["resolution"] == "matched")
    stages = Counter(row.get("evidenceStage", "exact_release_group" if row.get("matchMethod", "exact") == "exact"
                         else "scored_fallback") for row in rows if row["resolution"] == "matched")
    personal = Counter(outcome["disposition"] for (charted_country, _), outcome in outcomes.items()
                       if charted_country == country)
    exclusion_signals = Counter(reason for (charted_country, _), outcome in outcomes.items()
                                if charted_country == country for reason in outcome["exclusions"])
    exclusion_categories = {
        "Plex": {"plex_mbid", "plex_name"},
        "Lidarr": {"lidarr_available"},
        "Requested/pending": {"requested", "lidarr_search_pending", "download_pending"},
        "Dismissed": {"dismissed"},
        "Featured elsewhere": {"featured_elsewhere"},
    }
    source = snapshot["sourceRows"]
    unique_matched = resolution["matched"]
    if len(rows) != source:
        raise RuntimeError("Chart diagnostic rows do not reconcile with the source count.")
    if user_id is not None and sum(charted_country == country for charted_country, _ in outcomes) != unique_matched:
        raise RuntimeError("Chart candidates and diagnostic rows are from different refreshes.")
    excluded = sum(count for reason, count in personal.items() if reason != "final_requestable")
    return {
        "country": country, "updated": snapshot["updated"], "sourceUrl": snapshot["sourceUrl"],
        "userId": user_id,
        "summary": {
            "sourceRows": source,
            "matchAttempted": sum(bool(row.get("matchAttempted")) for row in rows),
            "mbMatchedUnique": unique_matched,
            "exactMatches": methods["exact"],
            "fallbackMatches": methods["fallback"],
            "evidenceStages": {stage: stages[stage] for stage in
                               ("exact_release_group", "apple_url_release", "release_title", "scored_fallback")},
            "requestCounts": snapshot.get("requestCounts"),
            "mbUnmatched": resolution["no_match"],
            "mbAmbiguous": resolution["ambiguous"],
            "invalidReleaseType": resolution["invalid_release_type"],
            "duplicateMbidsCollapsed": resolution["duplicate_mbid"],
            "missingSourceMetadata": resolution["missing_source_metadata"],
            "invalidSourceRows": resolution["invalid_source_row"],
            "lookupErrors": resolution["lookup_error"],
            "notAttemptedAfterOutage": resolution["not_attempted_after_outage"],
            "musicBrainzResolutionLossPercent": round(100 * (source - unique_matched) / source, 2) if source else 0,
            "personallyExcluded": excluded if user_id is not None else None,
            "primaryExclusionReasons": {reason: personal[reason] for reason in EXCLUSION_REASONS}
                                       if user_id is not None else None,
            "exclusionSignals": {reason: exclusion_signals[reason] for reason in EXCLUSION_REASONS}
                                if user_id is not None else None,
            "exclusionCategories": {
                label: sum(bool(set(outcome["exclusions"]) & reasons)
                           for (charted_country, _), outcome in outcomes.items() if charted_country == country)
                for label, reasons in exclusion_categories.items()
            } if user_id is not None else None,
            "personalFilteringLossPercent": round(100 * excluded / unique_matched, 2)
                                            if user_id is not None and unique_matched else None,
            "finalRequestable": personal["final_requestable"] if user_id is not None else None,
        },
        "rows": rows,
    }


def format_report(report):
    summary = report["summary"]
    lines = [f"Chart: {report['country']} | Apple updated: {report['updated']}",
             f"Source rows: {summary['sourceRows']}",
             f"MusicBrainz attempts: {summary['matchAttempted']}",
             f"MB matched (unique): {summary['mbMatchedUnique']}",
             f"  exact: {summary['exactMatches']}",
             f"  fallback: {summary['fallbackMatches']}",
             *(f"  {stage}: {count}" for stage, count in summary["evidenceStages"].items()),
             f"MB unmatched: {summary['mbUnmatched']}",
             f"MB ambiguous: {summary['mbAmbiguous']}",
             f"Invalid release type: {summary['invalidReleaseType']}",
             f"Duplicate MBIDs collapsed: {summary['duplicateMbidsCollapsed']}",
             f"Other resolution failures: {summary['missingSourceMetadata'] + summary['invalidSourceRows'] + summary['lookupErrors'] + summary['notAttemptedAfterOutage']}",
             f"MusicBrainz resolution loss: {summary['musicBrainzResolutionLossPercent']}%"]
    if summary["requestCounts"]:
        lines.append("MusicBrainz requests (logical/live):")
        lines.extend(f"  {stage}: {counts['logical']}/{counts['live']}"
                     for stage, counts in summary["requestCounts"].items())
    if report["userId"] is not None:
        lines.append("Personally excluded (categories overlap):")
        lines.extend(f"  {reason}: {count}" for reason, count in summary["exclusionCategories"].items())
        lines.append("Detailed exclusion signals:")
        lines.extend(f"  {reason}: {count}" for reason, count in summary["exclusionSignals"].items())
        lines.append("Primary dispositions (one per excluded row):")
        lines.extend(f"  {reason}: {count}" for reason, count in summary["primaryExclusionReasons"].items())
        lines.extend([f"  excluded total: {summary['personallyExcluded']}",
                      f"Personal filtering loss: {summary['personalFilteringLossPercent']}%",
                      f"Final requestable: {summary['finalRequestable']}"])
    lines.extend(["", "Rank | Apple title | Apple artist | MBID | Evidence stage | Resolution | Final disposition",
                  "--- | --- | --- | --- | --- | --- | ---"])
    for row in report["rows"]:
        cells = (row["rank"], row["sourceTitle"], row["sourceArtist"], row.get("matchedMbid", ""),
                 row.get("evidenceStage", row.get("matchMethod", "")), row["resolution"], row["finalDisposition"])
        lines.append(" | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in cells))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", choices=charts.CHART_COUNTRIES, default="us")
    parser.add_argument("--refresh", action="store_true", help="fetch Apple and resolve this chart now")
    parser.add_argument("--user-id", type=int, help="apply cached personal exclusions for this user")
    parser.add_argument("--json", help="write full row diagnostics to this local JSON path")
    args = parser.parse_args()
    init_cache_db()
    if args.refresh:
        charts.popular_albums(args.country, force_refresh=True)
    report = build_report(args.country, args.user_id)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    print(format_report(report))


if __name__ == "__main__":
    main()
