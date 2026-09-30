"""
DIBBS status + PDF checker - Aeropanel Case Study B
===================================================
For every AMSC-G candidate solicitation from a DIBBS collector run, this script
  1. opens the official RFQ package page (RFQRec.aspx?sn=...) and records its Status
     (Open / Removed / Awarded / ...), Issue Date and Return By, saving the page as evidence
  2. downloads the solicitation PDF (for quantities per line, price history and gates)

It uses the browser's own session (you accept the DoD notice yourself), but fetches pages
directly over HTTP, which is much faster than clicking through them.

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
    prompted_pkg = prompted_pdf = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(DIBBS_HOME, timeout=120_000)
            input("\nAccept the DoD notice banner in the browser, then press Enter here.\n")
            for i, t in enumerate(targets.itertuples(index=False), 1):
                if t.sol_key in done:
                    continue
                url = PKG_URL.format(sol=t.sol_key)
                checked = utc_stamp()
                for attempt in (1, 2):
                    try:
                        resp = context.request.get(url, timeout=120_000)
                        h, code = resp.text(), resp.status
                    except Exception as e:
                        h, code = "", f"error: {e}"
                    info = parse_package(h)
                    if info["parse_issue"].startswith("not a package page") and attempt == 1 and not prompted_pkg:
                        prompted_pkg = True
                        page.goto(url, timeout=120_000)
                        input("\nThe package page came back as a notice page. Accept it in the browser, "
                              "then press Enter.\n")
                        continue
                    break
                evid = out_dir / "pages" / f"{t.sol_key}.html"
                evid.write_text(h, encoding="utf-8")
                row = {"sol_key": t.sol_key, "package_url": url, "http_status": code, "checked_utc": checked,
                       "evidence_file": str(evid), **info}

                # ---- PDF
                pdf = pdf_dir / f"{t.sol_key}.PDF"
                if args.no_pdfs:
                    row["pdf"] = "skipped"
                elif pdf.exists():
                    row["pdf"] = "already had"; pdf_stats["already_had"] += 1
                elif str(t.pdf_url).startswith("http"):
                    for attempt in (1, 2):
                        try:
                            body = context.request.get(t.pdf_url, timeout=120_000).body()
                        except Exception:
                            body = b""
                        if body[:4] == b"%PDF":
                            pdf.write_bytes(body); row["pdf"] = "downloaded"; pdf_stats["downloaded"] += 1
                            break
                        if attempt == 1 and not prompted_pdf:
                            prompted_pdf = True
                            (out_dir / "_first_non_pdf_response.html").write_bytes(body)
                            tab = context.new_page()
                            try:
                                tab.goto(t.pdf_url, timeout=120_000)
                            except Exception:
                                pass
                            input("\nA PDF link returned a web page (probably the PDF server's notice). "
                                  "Accept it in the new tab, then press Enter.\n")
                            continue
                        row["pdf"] = "failed (not a PDF)"; pdf_stats["failed"] += 1
                        break
                else:
                    row["pdf"] = "no link"
                rows.append(row)
                if len(rows) % 25 == 0:                # save progress regularly
                    pd.concat([pd.read_csv(csv_path, dtype=str) if csv_path.exists() else pd.DataFrame(),
                               pd.DataFrame(rows)]).to_csv(csv_path, index=False)
                    rows = []
                    print(f"  {i}/{len(targets)} checked", flush=True)
                time.sleep(args.delay)
            browser.close()
    except (Exception, KeyboardInterrupt) as e:
        if rows:
            pd.concat([pd.read_csv(csv_path, dtype=str) if csv_path.exists() else pd.DataFrame(),
                       pd.DataFrame(rows)]).to_csv(csv_path, index=False)
        counts.update({"status": "FAILED/INTERRUPTED", "error": f"{type(e).__name__}: {e}",
                       "resume_with": f"python dibbs_status.py --run {run_dir} --resume {out_dir}"})
        log_run(counts)
        print("\n*** STATUS CHECK INTERRUPTED - progress saved. Resume with:\n   " + counts["resume_with"])
        sys.exit(1)

    if rows:
        pd.concat([pd.read_csv(csv_path, dtype=str) if csv_path.exists() else pd.DataFrame(),
                   pd.DataFrame(rows)]).to_csv(csv_path, index=False)
    res = pd.read_csv(csv_path, dtype=str)
    counts.update({"checked_total": len(res), "status_counts": res["status"].fillna("unparsed").value_counts().to_dict(),
                   "parse_issues": int((res["parse_issue"].fillna("") != "").sum()), "pdfs": pdf_stats,
                   "status": "SUCCESS"})
    (out_dir / "stage_counts.json").write_text(json.dumps(counts, indent=2))
    log_run(counts)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()