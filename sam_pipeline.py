"""
SAM.gov pipeline - Aeropanel Case Study B
=========================================
Input options (pick one or combine):
  python sam_pipeline.py --from-downloads            # newest ContractOpportunitiesFullCSV*.csv in ~/Downloads
  python sam_pipeline.py --file path\\to\\file.csv    # a specific file (repeatable: active + archived)
  python sam_pipeline.py --download                  # OPTIONAL: fetch the CSV directly (URL in .env SAM_CSV_URL)

What one run does:
  1. Snapshots every input file (dated + versioned copy, SHA-256 fingerprint, registry entry)
  2. Filters: 2026 window, actionable notice types, brief keywords + exclusions
  3. Writes auditable outputs + stage counts to data/runs/sam_<timestamp>/
  4. Compares with the previous snapshot: new / changed / removed / became-award notices
  5. Upserts matches into data/pipeline.db (one row per NoticeId -> reruns never duplicate)
  6. Logs the run (SUCCESS or FAILED) to data/run_log.jsonl. A failure is never reported as zero results.
"""
import argparse, hashlib, json, os, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path
import pandas as pd

from common import (DATA, START_2026, CUTOFF, normalize, match, exclusion, norm_nsn, norm_sol,
                    utc_stamp, log_run)

SNAP_DIR = DATA / "sam_snapshots"
REGISTRY = SNAP_DIR / "registry.json"
DB = DATA / "pipeline.db"
ACTIONABLE = {"Solicitation", "Combined Synopsis/Solicitation"}
REQUIRED_COLS = {"NoticeId", "Title", "Sol#", "PostedDate", "Type", "ResponseDeadLine", "Active", "Description"}
CHANGE_FIELDS = ["Title", "Type", "ResponseDeadLine", "Active", "ArchiveDate", "AwardNumber", "Award$", "Description"]


# ---------------------------------------------------------------- inputs
def newest_in_downloads(folder: Path) -> Path:
    files = sorted(folder.glob("ContractOpportunitiesFullCSV*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"No ContractOpportunitiesFullCSV*.csv found in {folder}")
    return files[-1]


# Public file-extract endpoints for the same CSV that the Data Services page serves.
# (1) SAM.gov's file-extract download API, (2) the public S3 bucket it points to.
DEFAULT_CSV_URLS = [
    "https://sam.gov/api/prod/fileextractservices/v1/api/download/Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv?privacy=Public",
    "https://s3.amazonaws.com/falextracts/Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv",
]


def download_csv(counts: dict) -> Path:
    """Automated download. Tries SAM_CSV_URL from .env first (if set), then the public extract URLs.
    A URL counts only if the first line of what comes back is the CSV header (NoticeId,...)."""
    import requests
    from dotenv import load_dotenv
    load_dotenv()
    custom = os.environ.get("SAM_CSV_URL", "").strip()
    urls = ([custom] if custom and "data-services" not in custom else []) + DEFAULT_CSV_URLS
    attempts = []
    for url in urls:
        dest = DATA / "sam_downloads" / f"ContractOpportunitiesFullCSV_{utc_stamp()}.csv"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(url, stream=True, timeout=900) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status_code != 200 or "html" in ctype.lower():
                    attempts.append(f"{url[:90]} -> HTTP {r.status_code}, {ctype}")
                    continue
                print(f"  downloading from {url[:90]} ...", flush=True)
                size = 0
                with dest.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk); size += len(chunk)
            with dest.open("r", encoding="cp1252", errors="replace") as f:
                header = f.readline()
            if "NoticeId" not in header:
                attempts.append(f"{url[:90]} -> not the opportunities CSV (header: {header[:60]!r})")
                dest.unlink(missing_ok=True)
                continue
            counts["download_url"] = r.url            # final URL after redirects
            counts["download_bytes"] = size
            counts["download_attempts"] = attempts + [f"{url[:90]} -> OK"]
            return dest
        except Exception as e:
            attempts.append(f"{url[:90]} -> {type(e).__name__}: {e}")
    counts["download_attempts"] = attempts
    raise RuntimeError("Automated download failed on every URL:\n  " + "\n  ".join(attempts))


# ---------------------------------------------------------------- snapshots
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(src: Path) -> dict:
    """Keep a dated, versioned copy. Same content downloaded twice -> reuses the existing snapshot."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    reg = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []
    digest = sha256(src)
    for s in reg:
        if s["sha256"] == digest:
            print(f"  {src.name}: identical to existing snapshot {s['snapshot_id']} (not stored again)")
            return {**s, "reused": True}
    downloaded = datetime.fromtimestamp(src.stat().st_mtime)        # when the file landed on disk
    date = downloaded.strftime("%Y-%m-%d")
    kind = "archived" if "archiv" in src.name.lower() else "active"
    version = 1 + sum(1 for s in reg if s["snapshot_date"] == date and s["kind"] == kind)
    sid = f"SAM_{kind}_{date}_v{version}"
    stored = SNAP_DIR / f"{sid}.csv"
    shutil.copy2(src, stored)
    entry = {"snapshot_id": sid, "kind": kind, "snapshot_date": date, "snapshot_version": version,
             "downloaded_local": downloaded.isoformat(timespec="seconds"),
             "ingested_utc": utc_stamp(), "original_file": str(src), "stored_as": str(stored),
             "sha256": digest}
    reg.append(entry)
    REGISTRY.write_text(json.dumps(reg, indent=2))
    print(f"  {src.name}: stored as {sid}")
    return {**entry, "reused": False}


def cleanup_download(src: Path):
    """Files the script downloaded itself are temporary: the snapshot copy is the kept evidence."""
    if (DATA / "sam_downloads").resolve() in src.resolve().parents:
        src.unlink(missing_ok=True)


def previous_snapshot(current: dict):
    reg = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []
    same = [s for s in reg if s["kind"] == current["kind"] and s["snapshot_id"] != current["snapshot_id"]
            and s["ingested_utc"] < current["ingested_utc"]]
    return same[-1] if same else None


def load_csv(path) -> pd.DataFrame:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc, low_memory=False)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not decode {path}")


# ---------------------------------------------------------------- change detection
def row_fingerprint(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in CHANGE_FIELDS if c in df.columns]
    return df[cols].fillna("").astype(str).agg("|".join, axis=1).map(lambda s: hashlib.md5(s.encode()).hexdigest())


def compare(prev_df: pd.DataFrame, cur_df: pd.DataFrame, prev_id: str, cur_id: str) -> pd.DataFrame:
    p = prev_df.set_index("NoticeId")
    c = cur_df.set_index("NoticeId")
    p_fp, c_fp = row_fingerprint(p.reset_index()), row_fingerprint(c.reset_index())
    p_fp.index, c_fp.index = p.index, c.index
    out = []
    for nid in c.index.difference(p.index):
        out.append({"NoticeId": nid, "change": "new", "Title": c.at[nid, "Title"]})
    for nid in p.index.difference(c.index):
        out.append({"NoticeId": nid, "change": "no longer in file", "Title": p.at[nid, "Title"]})
    both = c.index.intersection(p.index)
    changed = both[(c_fp.loc[both] != p_fp.loc[both]).values]
    for nid in changed:
        fields = [f for f in CHANGE_FIELDS if f in c.columns and str(p.at[nid, f]) != str(c.at[nid, f])]
        kind = "became award notice" if c.at[nid, "Type"] == "Award Notice" and p.at[nid, "Type"] != "Award Notice" else "changed"
        out.append({"NoticeId": nid, "change": kind, "fields_changed": "; ".join(fields), "Title": c.at[nid, "Title"]})
    res = pd.DataFrame(out)
    res["compared_from"], res["compared_to"] = prev_id, cur_id
    return res


# ---------------------------------------------------------------- matching
def run_matching(frame: pd.DataFrame, population: str):
    kept, excluded = [], []
    for _, row in frame.iterrows():
        t_n, d_n = normalize(row.get("Title")), normalize(row.get("Description"))
        m, where = match(t_n), "title"
        if not m:
            m, where = match(d_n), "description_only"
        if not m:
            continue
        rec = row.to_dict()
        rec.update(m)
        rec.update({"population": population, "matched_in": where,
                    "sol_key": norm_sol(row.get("Sol#")),
                    "public_link": f"https://sam.gov/opp/{row.get('NoticeId')}/view",
                    "nsn_found": norm_nsn(f"{row.get('Title')} {row.get('Description')}"),
                    "amsc": "", "amsc_status": "unverified (SAM.gov has no AMSC field)",
                    "review_flag": "broad keyword only" if m["broad_only"] else ""})
        reason = exclusion(t_n, m)
        if reason:
            rec["exclusion_reason"] = reason
            excluded.append(rec)
        else:
            kept.append(rec)
    return kept, excluded


# ---------------------------------------------------------------- database
def upsert(records: list, run_id: str) -> dict:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS sam_notices(
        notice_id TEXT PRIMARY KEY, sol_key TEXT, title TEXT, type TEXT, posted TEXT, deadline TEXT,
        population TEXT, matched_in TEXT, category TEXT, keyword_tags TEXT, nsn_found TEXT,
        content_hash TEXT, first_seen_run TEXT, last_seen_run TEXT, times_seen INTEGER)""")
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    for r in records:
        h = hashlib.md5("|".join(str(r.get(f, "")) for f in CHANGE_FIELDS).encode()).hexdigest()
        cur = con.execute("SELECT content_hash FROM sam_notices WHERE notice_id=?", (r["NoticeId"],)).fetchone()
        vals = (r.get("sol_key"), r.get("Title"), r.get("Type"), r.get("PostedDate"), r.get("ResponseDeadLine"),
                r.get("population"), r.get("matched_in"), r.get("category"), r.get("keyword_tags"),
                r.get("nsn_found"), h)
        if cur is None:
            con.execute("INSERT INTO sam_notices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                        (r["NoticeId"], *vals, run_id, run_id))
            stats["inserted"] += 1
        else:
            con.execute("""UPDATE sam_notices SET sol_key=?, title=?, type=?, posted=?, deadline=?, population=?,
                           matched_in=?, category=?, keyword_tags=?, nsn_found=?, content_hash=?,
                           last_seen_run=?, times_seen=times_seen+1 WHERE notice_id=?""",
                        (*vals, run_id, r["NoticeId"]))
            stats["updated" if cur[0] != h else "unchanged"] += 1
    con.commit()
    stats["total_rows_in_db"] = con.execute("SELECT COUNT(*) FROM sam_notices").fetchone()[0]
    con.close()
    return stats


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=[], help="CSV path (repeatable)")
    ap.add_argument("--from-downloads", nargs="?", const=str(Path.home() / "Downloads"),
                    help="use newest ContractOpportunitiesFullCSV*.csv in this folder (default ~/Downloads)")
    ap.add_argument("--download", action="store_true", help="OPTIONAL: download directly using SAM_CSV_URL")
    args = ap.parse_args()

    run_id = f"sam_{utc_stamp()}"
    out = DATA / "runs" / run_id
    counts = {"run_id": run_id, "source": "SAM.gov", "cutoff_provisional": CUTOFF}
    try:
        inputs = [Path(f) for f in args.file]
        if args.from_downloads:
            inputs.append(newest_in_downloads(Path(args.from_downloads)))
        if args.download:
            inputs.append(download_csv(counts))
            counts["retrieval"] = "automated download"
        else:
            counts["retrieval"] = "manual download, automated processing"
        if not inputs:
            raise RuntimeError("No input given. Use --from-downloads, --file or --download.")
        for p in inputs:
            if not p.exists():
                raise FileNotFoundError(f"Input file not found: {p}")

        print("Snapshots:")
        snaps = [snapshot(p) for p in inputs]
        for p in inputs:
            cleanup_download(p)
        frames, changes = [], []
        for s in snaps:
            df = load_csv(s["stored_as"])
            missing = REQUIRED_COLS - set(df.columns)
            if missing:
                raise RuntimeError(f"{s['snapshot_id']}: expected columns missing {missing} (SAM.gov format changed?)")
            df["snapshot_id"], df["snapshot_date"], df["snapshot_version"] = \
                s["snapshot_id"], s["snapshot_date"], s["snapshot_version"]
            frames.append(df)
            prev = previous_snapshot(s)
            if prev:
                changes.append(compare(load_csv(prev["stored_as"]), df, prev["snapshot_id"], s["snapshot_id"]))
        counts["snapshots"] = [{k: s[k] for k in ("snapshot_id", "snapshot_date", "snapshot_version", "sha256", "reused")}
                               for s in snaps]

        df = pd.concat(frames, ignore_index=True)
        counts["rows_in_files"] = len(df)
        df = df.drop_duplicates(subset="NoticeId", keep="first")     # active file listed first wins
        counts["rows_after_noticeid_dedupe"] = len(df)

        df["posted_dt"] = pd.to_datetime(df["PostedDate"], errors="coerce")
        counts["posted_date_min"], counts["posted_date_max"] = str(df["posted_dt"].min()), str(df["posted_dt"].max())
        out.mkdir(parents=True, exist_ok=True)

        awards = df[df["Type"] == "Award Notice"]
        awards.to_csv(out / "award_notices.csv", index=False)
        counts["award_notices"] = len(awards)

        unresolved = df[df["posted_dt"].isna() & df["Type"].isin(ACTIONABLE)]
        un_kept, _ = run_matching(unresolved, "date_unresolved")
        pd.DataFrame(un_kept).to_csv(out / "date_unresolved.csv", index=False)
        counts["date_unresolved_matches"] = len(un_kept)

        window = df[(df["posted_dt"] >= pd.Timestamp(START_2026)) & (df["posted_dt"] <= pd.Timestamp(CUTOFF))]
        counts["posted_in_2026_window"] = len(window)
        actionable = window[window["Type"].isin(ACTIONABLE)]
        counts["actionable_type"] = len(actionable)

        kept, excluded = run_matching(actionable, "main_2026")
        title_hits = [r for r in kept if r["matched_in"] == "title"]
        desc_only = [r for r in kept if r["matched_in"] == "description_only"]
        pd.DataFrame(title_hits).to_csv(out / "keyword_matches.csv", index=False)
        pd.DataFrame(desc_only).to_csv(out / "description_only_review.csv", index=False)
        pd.DataFrame(excluded).to_csv(out / "exclusion_log.csv", index=False)
        counts.update({"matched_in_title": len(title_hits), "matched_in_description_only": len(desc_only),
                       "excluded_by_rules": len(excluded),
                       "title_matches_with_nsn": sum(bool(r["nsn_found"]) for r in title_hits)})

        deadline = pd.to_datetime(df["ResponseDeadLine"], errors="coerce", utc=True).dt.tz_localize(None)
        pre = df[(df["posted_dt"] < pd.Timestamp(START_2026)) & df["Type"].isin(ACTIONABLE)
                 & (df["Active"].fillna("").str.strip().str.upper() == "YES") & (deadline >= pd.Timestamp.now())]
        pre_kept, _ = run_matching(pre, "pre2026_carry_forward")
        pd.DataFrame(pre_kept).to_csv(out / "pre2026_still_open.csv", index=False)
        counts["pre2026_still_open_matches"] = len(pre_kept)

        if changes:
            ch = pd.concat(changes, ignore_index=True)
            ch.to_csv(out / "changes_vs_previous.csv", index=False)
            counts["changes_vs_previous"] = ch["change"].value_counts().to_dict()
        else:
            counts["changes_vs_previous"] = "first snapshot - nothing to compare"

        counts["database"] = upsert(title_hits + desc_only + pre_kept + un_kept, run_id)
        counts["status"] = "SUCCESS"
        (out / "stage_counts.json").write_text(json.dumps(counts, indent=2, default=str))
        log_run(counts)
        print(json.dumps(counts, indent=2, default=str))
        print(f"\nOutputs: {out}")
    except Exception as e:
        counts.update({"status": "FAILED", "error": f"{type(e).__name__}: {e}"})
        log_run(counts)
        print("\n*** RUN FAILED - this is NOT a zero-result run ***")
        print(counts["error"])
        sys.exit(1)


if __name__ == "__main__":
    main()