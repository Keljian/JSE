"""Turn a triage packet into a decision brief.

A packet is 4-10 MB of JSON. Nothing useful can be done with that in a model's
context, and the daily loop should not spend tokens re-deriving the same
deterministic judgements every morning: which closing dates are manufactured,
which rows are the same job scraped twice, which titles are trades noise, which
"warm" rows have no contact behind them.

All of that happens here, for free, and what comes out is a few kilobytes: the
roles worth a human go/no-go, the ones closing for real, and the queue already
committed to. Written to <shortlists>/daily_brief.md and .json, plus a dated copy.

Usage:  python prefilter.py [packet.json]      (default: newest packet)
"""

import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

SHORTLISTS = Path(r"C:\JSE\shortlists")

# Titles that are never the target, whatever the keyword match says. The
# engineering lane pulls these in on hardware nouns; see the 17 Aug commentary.
JUNK_TITLE = re.compile(
    r"\b(technician|electrician|fitter|turner|apprentice|traineeship|trainee|"
    r"graduate|cadet|intern|nurse|midwife|lecturer|research fellow|phd|postdoc|"
    r"childcare|educator|teacher|barista|chef|cook|waiter|waitress|cleaner|"
    r"labourer|driver|warehouse|retail|store person|storeperson|receptionist|"
    r"carer|lifeguard|gardener|plumber|mechanic|welder|machinist|operator)\b",
    re.I,
)

# Management and senior-IC signals. Not a hard gate on its own: a scored row
# survives on its score regardless of title.
LEVEL_TITLE = re.compile(
    r"\b(head of|chief|c[tio]o|director|manager|management|lead|leader|principal|"
    r"superintendent|architect|owner|partner)\b",
    re.I,
)

# The eastern corridor, which is the single biggest time-saver on this search and
# the thing the broken commute field should be surfacing.
EAST = re.compile(
    r"croydon|ringwood|bayswater|mitcham|nunawading|box hill|burwood|blackburn|"
    r"scoresby|mulgrave|clayton|knox|wantirna|glen waverley|doncaster|camberwell|"
    r"hawthorn|notting hill|rowville|vermont|lilydale|chirnside|healesville|"
    r"forest hill|kilsyth|bulleen|templestowe|donvale|mount waverley|oakleigh|"
    r"maroondah|whitehorse|manningham|monash|boronia|ferntree gully|"
    r"heathmont|nunawading|tally ho|burwood east|surrey hills|balwyn",
    re.I,
)

NON_VIC = re.compile(
    r"\b(sydney|brisbane|perth|adelaide|canberra|darwin|hobart|newcastle|"
    r"wollongong|gold coast|nsw|qld|wa\b|sa\b|act\b|nt\b|tas\b|auckland|"
    r"wellington|singapore|london|manila)\b",
    re.I,
)
VIC_HINT = re.compile(r"victoria|melbourne|\bvic\b|remote|anywhere|australia wide", re.I)

WORD = re.compile(r"[a-z0-9]+")
FIT = re.compile(r"Fit Level:\s*([A-Za-z_ -]+)")
ACTION = re.compile(r"Recommended Action:\s*(.+)")


def norm(value):
    return " ".join(WORD.findall(str(value or "").lower()))


def parse_date(value):
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def fit_level(job):
    m = FIT.search(job.get("analysis") or "")
    return m.group(1).strip().lower() if m else None


def recommended_action(job):
    m = ACTION.search(job.get("analysis") or "")
    return m.group(1).strip()[:60] if m else ""


def newest_packet():
    files = sorted(SHORTLISTS.glob("shortlist_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"no packets in {SHORTLISTS}")
    return files[0]


def suburb(job):
    text = str(job.get("location") or "")
    return text.split(",")[0].strip()[:24] or "-"


def employer(job):
    """The packet's `company` is often a token scraped out of the ad body."""
    company = str(job.get("company") or "").strip()
    advertiser = str(job.get("advertiser") or "").strip()
    if company.lower() in ("", "unknown", "none", "commercial", "product", "c-suite", "software"):
        return advertiser or company or "Unknown"
    return company


def flag_types(job):
    seen = []
    for flag in job.get("flags") or []:
        if isinstance(flag, dict):
            kind = flag.get("type")
            if kind and kind not in seen:
                seen.append(kind)
    return ",".join(seen)


def build(packet_path):
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    jobs = packet.get("jobs") or []
    total = len(jobs)
    if not total:
        raise SystemExit("packet is empty")

    # A closing date carried by a large share of the file is a scraper default.
    counts = Counter(str(j.get("closing_date"))[:10] for j in jobs if j.get("closing_date"))
    placeholders = {d for d, c in counts.items() if c >= max(15, int(total * 0.04))}

    scored_total = sum(1 for j in jobs if j.get("match_score") is not None)

    kept, dropped = [], Counter()
    for job in jobs:
        title = str(job.get("title") or "")
        stage = job.get("pipeline_stage") or "new"
        if stage not in ("new", "interested"):
            dropped["stage"] += 1
            continue
        if (job.get("commute_verdict") or "") == "blocked":
            dropped["commute_blocked"] += 1
            continue
        if JUNK_TITLE.search(title):
            dropped["title_level"] += 1
            continue
        where = f"{job.get('location') or ''} {employer(job)}"
        if NON_VIC.search(where) and not VIC_HINT.search(where):
            dropped["interstate"] += 1
            continue
        raw_close = str(job.get("closing_date") or "")[:10]
        job["_closes"] = None if raw_close in placeholders else parse_date(raw_close)
        job["_employer"] = employer(job)
        job["_east"] = bool(EAST.search(where))
        job["_level"] = bool(LEVEL_TITLE.search(title))
        kept.append(job)

    # Same ad, scraped twice under two employer strings. Keep the richer row.
    best = {}
    for job in kept:
        key = (norm(job.get("title")), norm(job["_employer"]))
        current = best.get(key)
        rank = (job.get("match_score") or 0, 1 if job.get("analysis") else 0, job.get("id") or 0)
        if current is None or rank > current[0]:
            best[key] = (rank, job)
        else:
            dropped["duplicate"] += 1
    kept = [entry[1] for entry in best.values()]

    today = date.today()
    scored = [j for j in kept if j.get("match_score") is not None]
    unscored = [j for j in kept if j.get("match_score") is None]

    # Rank on the match score, not the composite: the composite still carries the
    # warmth bonus and `warm_path` is empty on every row in the file.
    def urgency(job):
        if not job["_closes"]:
            return 0
        days = (job["_closes"] - today).days
        return 6 if 0 <= days <= 2 else 4 if days <= 5 else 2 if days <= 9 else 0

    scored.sort(key=lambda j: (
        -(j.get("match_score") or 0) - urgency(j) - (3 if j["_east"] else 0),
        str(j["_closes"] or "9999-12-31"),
    ))
    unscored.sort(key=lambda j: (
        -(urgency(j) + (4 if j["_east"] else 0) + (3 if j["_level"] else 0)),
        str(j["_closes"] or "9999-12-31"),
    ))

    horizon = today + timedelta(days=4)
    closing = sorted(
        [j for j in kept if j["_closes"] and today <= j["_closes"] <= horizon],
        key=lambda j: (j["_closes"], -(j.get("match_score") or 0)),
    )
    queue = sorted(
        [j for j in kept if (j.get("pipeline_stage") or "") == "interested"],
        key=lambda j: str(j["_closes"] or "9999-12-31"),
    )

    return {
        "packet": Path(packet_path).name,
        "generated_at": packet.get("generated_at"),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "packet_rows": total,
        "packet_scored": scored_total,
        "packet_scored_pct": round(100 * scored_total / total),
        "after_filters": len(kept),
        "dropped": dict(dropped),
        "placeholder_dates": sorted(placeholders),
        "candidates_scored": scored[:12],
        "candidates_unscored": unscored[:10],
        "closing_soon": closing[:15],
        "queue": queue,
    }


def row(job, show_score=True):
    bits = [
        f"`{job.get('id')}`",
        (job.get("title") or "")[:52],
        job["_employer"][:26],
        suburb(job),
    ]
    if show_score:
        score = job.get("match_score")
        bits.append(f"{score}/{fit_level(job) or chr(63)}" if score is not None else "-")
    bits.append(job["_closes"].strftime("%a %d %b") if job["_closes"] else "-")
    km = job.get("commute_km")
    bits.append("east" if job["_east"] else (f"{km}km" if km not in (None, "", 28.7, 0.0) else "-"))
    bits.append(flag_types(job) or "-")
    return "| " + " | ".join(str(b) for b in bits) + " |"


def markdown(brief):
    out = [
        f"# Daily brief - {brief['today']}",
        "",
        f"Packet `{brief['packet']}` generated {brief['generated_at']}. "
        f"{brief['packet_rows']} rows, {brief['packet_scored']} scored "
        f"({brief['packet_scored_pct']}%), {brief['after_filters']} survive filtering.",
        "",
        "Dropped: " + ", ".join(f"{k} {v}" for k, v in sorted(brief["dropped"].items())) + ".",
        "Closing dates ignored as scraper defaults: "
        + (", ".join(brief["placeholder_dates"]) or "none") + ".",
        "",
    ]
    if brief["packet_scored_pct"] < 60:
        out += [
            f"> **Coverage warning.** Only {brief['packet_scored_pct']}% of the packet was "
            "scored, so the ranked list below is drawn from a minority of the market. "
            "Check the analysis step before trusting it.",
            "",
        ]

    head = "| id | role | employer | where | score/fit | closes | commute | flags |"
    rule = "|---|---|---|---|---|---|---|---|"
    head_ns = "| id | role | employer | where | closes | commute | flags |"
    rule_ns = "|---|---|---|---|---|---|---|"

    if brief["closing_soon"]:
        out += ["## Closing within four days", "", head, rule]
        out += [row(j) for j in brief["closing_soon"]] + [""]

    out += ["## Top scored candidates", "", head, rule]
    out += [row(j) for j in brief["candidates_scored"]] + [""]

    if brief["candidates_unscored"]:
        out += [
            "## On-level roles the scorer never assessed",
            "",
            head_ns,
            rule_ns,
        ]
        out += [row(j, show_score=False) for j in brief["candidates_unscored"]] + [""]

    if brief["queue"]:
        out += ["## Already committed (interested)", "", head, rule]
        out += [row(j) for j in brief["queue"]] + [""]

    out += [
        "## Pick",
        "",
        "Choose five. Weight a real closing date over a score, and prefer the eastern "
        "corridor where the fit is comparable. Then call `jse_prepare_applications` "
        "with the five ids.",
    ]
    return "\n".join(out) + "\n"


def main():
    packet_path = Path(sys.argv[1]) if len(sys.argv) > 1 else newest_packet()
    if not packet_path.is_absolute():
        packet_path = SHORTLISTS / packet_path
    brief = build(packet_path)

    def slim(job):
        return {
            "id": job.get("id"),
            "title": job.get("title"),
            "employer": job["_employer"],
            "location": job.get("location"),
            "suburb": suburb(job),
            "east": job["_east"],
            "match_score": job.get("match_score"),
            "fit": fit_level(job),
            "recommended_action": recommended_action(job),
            "closes": job["_closes"].isoformat() if job["_closes"] else None,
            "commute_km": job.get("commute_km"),
            "salary": job.get("salary"),
            "flags": flag_types(job),
            "flag_summary": job.get("flag_summary"),
            "stage": job.get("pipeline_stage"),
            "source": job.get("source"),
            "url": job.get("url"),
        }

    payload = {k: v for k, v in brief.items() if not isinstance(v, list)}
    for key in ("candidates_scored", "candidates_unscored", "closing_soon", "queue"):
        payload[key] = [slim(j) for j in brief[key]]

    stamp = datetime.now().strftime("%Y-%m-%d")
    SHORTLISTS.mkdir(parents=True, exist_ok=True)
    text = markdown(brief)
    for target in (SHORTLISTS / "daily_brief.md", SHORTLISTS / f"daily_brief_{stamp}.md"):
        target.write_text(text, encoding="utf-8")
    for target in (SHORTLISTS / "daily_brief.json", SHORTLISTS / f"daily_brief_{stamp}.json"):
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "packet": brief["packet"],
        "packet_rows": brief["packet_rows"],
        "scored_pct": brief["packet_scored_pct"],
        "after_filters": brief["after_filters"],
        "brief_md": str(SHORTLISTS / "daily_brief.md"),
        "brief_chars": len(text),
    }))


if __name__ == "__main__":
    main()
