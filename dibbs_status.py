"""
DIBBS status + PDF checker - Aeropanel Case Study B
===================================================
For every AMSC-G candidate solicitation from a DIBBS collector run, this script
  1. opens the official RFQ package page (RFQRec.aspx?sn=...) and records its Status
     (Open / Removed / Awarded / ...), Issue Date and Return By, saving the page as evidence
  2. downloads the solicitation PDF (for quantities per line, price history and gates)

It drives the browser like a person would (you accept the DoD notice yourself). PDFs arrive as
downloads from DIBBS's second server; the script catches each download and saves it to data/dibbs_pdfs.

    python dibbs_status.py                              # latest DIBBS run, all G candidates
    python dibbs_status.py --run data/runs/dibbs_<id>   # a specific run
    python dibbs_status.py --limit 50                   # quick test
    python dibbs_status.py --resume data/dibbs_status/<check_id>   # continue an interrupted check
Outputs: data/dibbs_status/<check_id>/package_status.csv (+ one saved .html per solicitation)
         data/dibbs_pdfs/<SOL>.PDF
"""
import argparse, html as htmlmod, json, re, sys, time
from pathlib import Path
import pandas as pd
from common import DATA, norm_sol, utc_stamp, log_run

DIBBS_HOME = "https://www.dibbs.bsm.dla.mil/"
PKG_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RFQRec.aspx?sn={sol}"
POPULATIONS = ["g_candidates_main_2026.csv", "g_pre2026_carry_forward.csv", "g_date_unresolved.csv"]


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmlmod.unescape(h)).strip()


def parse_package(h: str) -> dict:
    """Pull Status / Issue Date / Return By / PR quantities out of an RFQ package page."""
    t = html_to_text(h)
    out = {"is_package_page": "RFQ Package Data" in t or "Package Data" in t}
    m = re.search(r"Return By\s+([A-Z0-9-]{13,20})\s+(.{0,80}?)\s+(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})", t)
    if m:
        raw = re.sub(r"\bQ\s*uote\b|\buote\b", "", m.group(2)).strip()
        low = raw.lower()
        norm = ("Open" if low.startswith("open") else "Removed" if "remov" in low else
                "Awarded" if "award" in low else "Cancelled" if "cancel" in low else
                "Closed" if "closed" in low else raw or "unknown")
        out.update({"solicitation_display": m.group(1), "status_raw": raw, "status": norm,
                    "issue_date": m.group(3), "return_by": m.group(4)})
    out["no_longer_available"] = "no longer available" in t.lower()
    out["quote_link_present"] = "QuoteFrm.aspx" in h
    prs = re.findall(r"\b(\d{10})\s+Qty\s*[:\-]?\s*([\d,]+|See Solicitation)", t)
    out["pr_quantities"] = "; ".join(f"{p}: {q}" for p, q in prs)
    out["parse_issue"] = "" if m else ("not a package page (notice/consent/error page?)"
                                       if not out["is_package_page"] else "status row not parsed")
    return out


def targets_from_run(run_dir: Path) -> pd.DataFrame:
    frames = []
    for f in POPULATIONS:
        p = run_dir / f
        if p.exists() and p.stat().st_size > 2:
            frames.append(pd.read_csv(p, dtype=str))
    if not frames:
        sys.exit(f"No G-candidate files found in {run_dir}")
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates("sol_key")[["sol_key", "solicitation", "pdf_url"]]


def latest_dibbs_run() -> Path:
    runs = sorted(p for p in (DATA / "runs").glob("dibbs_*") if (p / "g_candidates_main_2026.csv").exists())
    if not runs:
        sys.exit("No DIBBS collector run with g_candidates_main_2026.csv found under data/runs")
    return runs[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="DIBBS collector run folder (default: latest)")
    ap.add_argument("--limit", type=int, help="only the first N solicitations (testing)")
    ap.add_argument("--no-pdfs", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--resume", help="existing data/dibbs_status/<check_id> folder to continue")
    args = ap.parse_args()

    run_dir = Path(args.run) if args.run else latest_dibbs_run()
    targets = targets_from_run(run_dir)
    if args.limit:
        targets = targets.head(args.limit)
    out_dir = Path(args.resume) if args.resume else DATA / "dibbs_status" / f"status_{utc_stamp()}"
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)
    pdf_dir = DATA / "dibbs_pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "package_status.csv"
    done = set(pd.read_csv(csv_path, dtype=str)["sol_key"]) if csv_path.exists() else set()
    counts = {"run_id": out_dir.name, "source": "DIBBS package status", "from_collector_run": str(run_dir),
              "targets": len(targets), "already_done": len(done)}
    print(f"{len(targets)} solicitations to check ({len(done)} already done) from {run_dir}")

    from playwright.sync_api import sync_playwright
    rows, pdf_stats = [], {"downloaded": 0, "already_had": 0, "failed": 0}

    def save_progress():
        nonlocal rows
        if rows:
            old = pd.read_csv(csv_path, dtype=str) if csv_path.exists() else pd.DataFrame()
            pd.concat([old, pd.DataFrame(rows)], ignore_index=True).to_csv(csv_path, index=False)
            rows = []

    def is_notice(h: str) -> bool:
        t = html_to_text(h).lower()
        return ("package data" not in t) and any(k in t for k in ("consent", "notice", "i agree", "you are accessing"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(DIBBS_HOME, timeout=120_000)
            input("\nAccept the DoD notice banner in the browser, then press Enter here.\n")

            # ---- PDF handling: DIBBS serves PDFs as downloads from a second server (dibbs2),
            # which shows its own notice the first time. We catch the download event and save it ourselves.
            pdf_page = context.new_page()
            downloads = []
            pdf_page.on("download", lambda dl: downloads.append(dl))

            def fetch_pdf(url: str, dest: Path) -> str:
                downloads.clear()
                resp = None
                try:
                    resp = pdf_page.goto(url, timeout=90_000)
                except Exception:
                    pass                                   # "Download is starting" is expected here
                if resp is not None:
                    try:
                        body = resp.body()
                        if body[:4] == b"%PDF":            # PDF shown inline instead of downloaded
                            dest.write_bytes(body); return "downloaded"
                    except Exception:
                        pass
                for _ in range(60):                        # wait up to 30 s for the download to start
                    if downloads:
                        break
                    pdf_page.wait_for_timeout(500)
                if downloads:
                    downloads[-1].save_as(str(dest))
                    return "downloaded" if dest.exists() and dest.read_bytes()[:4] == b"%PDF" else "failed (download not a PDF)"
                try:
                    return "notice" if is_notice(pdf_page.content()) else "failed (no download)"
                except Exception:
                    return "failed (no download)"

            prompted_pdf = False
            for i, t in enumerate(targets.itertuples(index=False), 1):
                if t.sol_key in done:
                    continue
                url = PKG_URL.format(sol=t.sol_key)
                checked = utc_stamp()
                h, code = "", ""
                for attempt in (1, 2):                     # real page navigation (same as a person clicking)
                    try:
                        resp = page.goto(url, timeout=120_000, wait_until="domcontentloaded")
                        code = resp.status if resp else ""
                        h = page.content()
                    except Exception as e:
                        h, code = "", f"error: {type(e).__name__}"
                    if attempt == 1 and is_notice(h):
                        input("\nDIBBS showed its notice again. Accept it in the browser, then press Enter.\n")
                        continue
                    break
                evid = out_dir / "pages" / f"{t.sol_key}.html"
                evid.write_text(h, encoding="utf-8")
                info = parse_package(h)
                row = {"sol_key": t.sol_key, "package_url": url, "http_status": code, "checked_utc": checked,
                       "evidence_file": str(evid), "status": "", "status_raw": "", "issue_date": "", "return_by": "",
                       **info}

                pdf = pdf_dir / f"{t.sol_key}.PDF"
                if args.no_pdfs:
                    row["pdf"] = "skipped"
                elif pdf.exists():
                    row["pdf"] = "already had"; pdf_stats["already_had"] += 1
                elif str(t.pdf_url).startswith("http"):
                    result = fetch_pdf(t.pdf_url, pdf)
                    if result == "notice" and not prompted_pdf:
                        prompted_pdf = True
                        pdf_page.bring_to_front()
                        input("\nThe PDF server is showing its own notice (in the 2nd tab). Accept it there, "
                              "then press Enter.\n")
                        page.bring_to_front()
                        result = fetch_pdf(t.pdf_url, pdf)
                    row["pdf"] = result
                    pdf_stats["downloaded" if result == "downloaded" else "failed"] += 1
                else:
                    row["pdf"] = "no link"
                rows.append(row)
                print(f"  [{i}/{len(targets)}] {t.sol_key}: {row['status'] or row['parse_issue']} | pdf: {row['pdf']}",
                      flush=True)
                if len(rows) >= 25:
                    save_progress()
                time.sleep(args.delay)
            save_progress()
            browser.close()
    except (Exception, KeyboardInterrupt) as e:
        save_progress()
        counts.update({"status": "FAILED/INTERRUPTED", "error": f"{type(e).__name__}: {e}",
                       "resume_with": f"python dibbs_status.py --run {run_dir} --resume {out_dir}"})
        log_run(counts)
        print("\n*** STATUS CHECK INTERRUPTED - progress saved. Resume with:\n   " + counts["resume_with"])
        sys.exit(1)

    res = pd.read_csv(csv_path, dtype=str) if csv_path.exists() else pd.DataFrame(columns=["status", "parse_issue"])
    st = res.get("status", pd.Series(dtype=str)).fillna("").replace("", "unparsed")
    counts.update({"checked_total": len(res), "status_counts": st.value_counts().to_dict(),
                   "parse_issues": int((res.get("parse_issue", pd.Series(dtype=str)).fillna("") != "").sum()),
                   "pdfs": pdf_stats, "status": "SUCCESS" if (st != "unparsed").any() else "FAILED (no page parsed)"})
    (out_dir / "stage_counts.json").write_text(json.dumps(counts, indent=2))
    log_run(counts)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()