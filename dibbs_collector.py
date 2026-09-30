"""
DIBBS collector - Aeropanel Case Study B
========================================
Drives the public DIBBS "RFQ Solicitation Text Search" in a real browser (Playwright),
saves every results page as evidence, parses each result line, then applies the SAME
keyword/exclusion rules as the SAM.gov pipeline.

First-time setup:
    pip install playwright pandas python-dotenv
    python -m playwright install chromium

Typical runs:
    python dibbs_collector.py --terms "PANEL" --max-pages 2          # small test run first!
    python dibbs_collector.py --preset roots --max-pages 30          # main run
    python dibbs_collector.py --preset roots --max-pages 30 --pdfs   # also download PDFs for AMSC-G candidates
    python dibbs_collector.py --reparse data/dibbs_raw/<run_id>      # re-parse saved pages, no website access

How access works (no bypassing): the script opens a visible browser. YOU accept the
DoD notice banner and open the Text Search page yourself, then press Enter in the
terminal. The script only automates typing search terms and turning pages.
"""
import argparse, json, re, sqlite3, sys, time
from datetime import datetime
from pathlib import Path
import pandas as pd

from common import (DATA, START_2026, CUTOFF, normalize, match, exclusion, norm_nsn, norm_sol,
                    utc_stamp, log_run, CATEGORIES, kw_pattern)

DIBBS_HOME = "https://www.dibbs.bsm.dla.mil/"
DB = DATA / "pipeline.db"

# Root words: every brief phrase contains at least one of these, so searching the roots and
# then matching the exact phrases locally (on the NOMENCLATURE only) covers the full keyword list
# with far fewer slow searches. Words that appear in DLA boilerplate (e.g. MARKING, PLATE, LIGHT)
# return huge result sets - their page caps are logged as coverage limits, never hidden.
ROOT_TERMS = ["PANEL", "CONTROL HEAD", "LIGHTPLATE", "LIGHT PLATE", "HIDDEN UNTIL LIT", "MIL-DTL-7788",
              "BEZEL", "FACEPLATE", "FACE PLATE", "ESCUTCHEON", "OVERLAY", "DIAL",
              "LENS", "WINDOW", "NVIS", "LAMP", "HOUSING",
              "INDICATOR", "ANNUNCIATOR", "WARNING LIGHT", "LIGHTING", "COCKPIT",
              "FORMATION LIGHT", "NAVIGATION LIGHT", "NAVIGATIONAL", "POSITION LIGHT", "ANTI-COLLISION",
              "EXTERIOR LIGHT", "BLACKOUT", "VEHICLE LIGHT", "WINGTIP", "DROGUE",
              "SWITCH", "PUSHBUTTON", "PUSH BUTTON", "KEYBOARD", "KEYPAD", "ACTUATOR", "DIMMER",
              "PLATE", "NAMEPLATE", "LEGEND", "MARKING", "ILLUMINATED",
              "TACHOMETER", "TORQUEMETER", "FUEL", "TRANSDUCER", "THERMOCOUPLE",
              "HARNESS", "CABLE ASSEMBLY", "BOX"]
FULL_TERMS = [kw for kws in CATEGORIES.values() for kw in kws]

SOL_SPLIT = re.compile(r"\b([A-Z0-9]{13})\.PDF\b")
LINE_RE = re.compile(r"NSN:\s*(\S+)\s+Nomenclature:\s*(.+?)\s+AMSC:\s*([A-Z0-9]?)\s+NAICS:\s*(\S*)\s+Quantity:\s*([\d,]+)",
                     re.S)


# ---------------------------------------------------------------- parsing (pure text, testable offline)
def parse_results_text(text: str, links: list, term: str, page_no: int, fetched_utc: str) -> list:
    pdf_links = {t.upper().replace(".PDF", ""): h for t, h in links if t.upper().endswith(".PDF")}
    pkg_links = [h for t, h in links if "package view" in t.lower()]
    parts = SOL_SPLIT.split(text)
    rows = []
    # parts = [before, sol1, block1, sol2, block2, ...]
    for i in range(1, len(parts), 2):
        sol, block = parts[i], parts[i + 1]
        g = lambda pat: (re.search(pat, block, re.S).group(1).strip() if re.search(pat, block, re.S) else "")
        prs = re.findall(r"\b\d{10}\b", g(r"Purchase Request:\s*([\d,\s]+)"))
        base = {"solicitation": sol, "sol_key": norm_sol(sol),
                "issued": g(r"Issued:\s*(\d{2}-\d{2}-\d{4})"),
                "return_by": g(r"Return By:\s*(\d{2}-\d{2}-\d{4})"),
                "doc_posted_updated": g(r"Post/Updated:\s*([\d-]+\s+[\d:]+)"),
                "fob": g(r"FOB Point:\s*(\w+)"), "inspection": g(r"Inspection Point:\s*(\w+)"),
                "tech_data": g(r"Tech Data:\s*(\w+)"), "purchase_requests": "; ".join(prs),
                "pdf_url": pdf_links.get(sol, ""),
                "package_url": pkg_links[(i - 1) // 2] if (i - 1) // 2 < len(pkg_links) else "",
                "official_link": f"https://www.dibbs.bsm.dla.mil/rfq/rfqrec.aspx?sn={sol}",
                "search_term": term, "page": page_no, "retrieved_utc": fetched_utc}
        lines = LINE_RE.findall(block)
        if not lines:
            rows.append({**base, "parse_issue": "no NSN line parsed"})
        for nsn, nom, amsc, naics, qty in lines:
            src = re.search(r"CAGE\s+Part Number\s+Company Name\s+(\S+)\s+(\S+)\s+([^\n]+)", block)
            rows.append({**base, "nsn": norm_nsn(nsn) or nsn, "nsn_raw": nsn,
                         "nomenclature": " ".join(nom.split()), "amsc": amsc, "naics": naics,
                         "quantity_listed": int(qty.replace(",", "")),
                         "approved_source": " | ".join(src.groups()).strip() if src else "",
                         "parse_issue": ""})
    return rows


# ---------------------------------------------------------------- classification
def classify(df: pd.DataFrame, run_id: str) -> dict:
    today = pd.Timestamp.now().normalize()
    df = df.copy()
    df["issued_dt"] = pd.to_datetime(df["issued"], format="%m-%d-%Y", errors="coerce")
    df["return_by_dt"] = pd.to_datetime(df["return_by"], format="%m-%d-%Y", errors="coerce")
    # one row per solicitation + NSN; remember every term that found it
    terms = df.groupby(["sol_key", "nsn"])["search_term"].agg(lambda s: "; ".join(sorted(set(s))))
    df = df.drop_duplicates(subset=["sol_key", "nsn"]).set_index(["sol_key", "nsn"])
    df["found_by_terms"] = terms
    df = df.reset_index()
    df["line_key"] = "DIBBS|" + df["sol_key"] + "|" + df["nsn"].astype(str)

    out = {"fat": [], "no_nomenclature_match": [], "broad_context": [], "excluded": [], "non_g": [], "review_amsc": [],
           "g_main_2026": [], "g_pre2026_open": [], "g_date_unresolved": [], "pre2026_closed": []}
    for _, r in df.iterrows():
        rec = r.to_dict()
        nom = str(rec.get("nomenclature") or "")
        if str(rec.get("nsn_raw", "")).upper().startswith("0001S") or "FIRST ARTIC" in nom.upper():
            rec["reason"] = "First article test line - not a product line"
            out["fat"].append(rec); continue
        m = match(normalize(nom))
        if not m:
            rec["reason"] = "Search hit came from solicitation text, not the item nomenclature"
            out["no_nomenclature_match"].append(rec); continue
        rec.update(m)
        rec["review_flag"] = "broad keyword only" if m["broad_only"] else ""
        # DLA item names are "NOUN, MODIFIERS" (e.g. "INSERT, PANEL FASTENER" is an insert, not a panel).
        # A broad word that appears only in the modifiers is context, not the item itself.
        noun = normalize(nom.split(",")[0])
        hits = [k for k in m["keyword_tags"].split("; ") if k]
        if m["broad_only"] and not any(kw_pattern(k).search(noun) for k in hits):
            rec["reason"] = f"Broad keyword only in modifier, item noun is '{nom.split(',')[0].strip()}'"
            out["broad_context"].append(rec); continue
        if m["broad_only"]:
            rec["review_flag"] = "broad keyword, but it is the item noun"
        reason = exclusion(normalize(nom), m, fsc=str(rec.get("nsn") or "")[:4])
        if reason:
            rec["reason"] = reason; out["excluded"].append(rec); continue
        amsc = str(rec.get("amsc") or "").strip().upper()
        rec["amsc_source"] = f"DIBBS RFQ Text Search result, AMSC field, retrieved {rec.get('retrieved_utc')}"
        if amsc == "":
            rec["reason"] = "AMSC blank/unavailable"; out["review_amsc"].append(rec); continue
        if amsc != "G":
            rec["reason"] = f"AMSC {amsc} (not G)"; out["non_g"].append(rec); continue
        rec["deadline_position"] = ("open by deadline" if pd.notna(r["return_by_dt"]) and r["return_by_dt"] >= today
                                    else "past deadline" if pd.notna(r["return_by_dt"]) else "deadline unknown")
        if pd.isna(r["issued_dt"]):
            out["g_date_unresolved"].append(rec)
        elif pd.Timestamp(START_2026) <= r["issued_dt"] <= pd.Timestamp(CUTOFF):
            out["g_main_2026"].append(rec)
        elif r["issued_dt"] < pd.Timestamp(START_2026):
            (out["g_pre2026_open"] if rec["deadline_position"] == "open by deadline" else out["pre2026_closed"]).append(rec)
        else:
            rec["reason"] = "Issued after cutoff"; out["excluded"].append(rec)
    out["_deduped"] = df
    return out


def upsert(records: list, run_id: str) -> dict:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS dibbs_lines(
        line_key TEXT PRIMARY KEY, sol_key TEXT, nsn TEXT, nomenclature TEXT, amsc TEXT, issued TEXT,
        return_by TEXT, quantity_listed INTEGER, population TEXT, category TEXT,
        first_seen_run TEXT, last_seen_run TEXT, times_seen INTEGER, last_quantity_change TEXT)""")
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    for pop, r in records:
        cur = con.execute("SELECT return_by, quantity_listed, amsc FROM dibbs_lines WHERE line_key=?",
                          (r["line_key"],)).fetchone()
        vals = (r["sol_key"], r["nsn"], r.get("nomenclature"), r.get("amsc"), r.get("issued"), r.get("return_by"),
                r.get("quantity_listed"), pop, r.get("category"))
        if cur is None:
            con.execute("INSERT INTO dibbs_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'')",
                        (r["line_key"], *vals, run_id, run_id)); stats["inserted"] += 1
        else:
            changed = (cur[0], cur[1], cur[2]) != (r.get("return_by"), r.get("quantity_listed"), r.get("amsc"))
            note = f"{run_id}: qty {cur[1]}->{r.get('quantity_listed')}" if cur[1] != r.get("quantity_listed") else ""
            con.execute("""UPDATE dibbs_lines SET sol_key=?, nsn=?, nomenclature=?, amsc=?, issued=?, return_by=?,
                           quantity_listed=?, population=?, category=?, last_seen_run=?, times_seen=times_seen+1,
                           last_quantity_change=CASE WHEN ?<>'' THEN ? ELSE last_quantity_change END
                           WHERE line_key=?""", (*vals, run_id, note, note, r["line_key"]))
            stats["updated" if changed else "unchanged"] += 1
    con.commit()
    stats["total_rows_in_db"] = con.execute("SELECT COUNT(*) FROM dibbs_lines").fetchone()[0]
    con.close()
    return stats


def write_outputs(all_rows: list, term_summary: list, run_id: str, counts: dict):
    out = DATA / "runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    raw = pd.DataFrame(all_rows)
    raw.to_csv(out / "dibbs_all_rows.csv", index=False)
    pd.DataFrame(term_summary).to_csv(out / "term_summary.csv", index=False)
    counts["rows_parsed_all_terms"] = len(raw)
    counts["parse_issues"] = int((raw.get("parse_issue", pd.Series(dtype=str)).fillna("") != "").sum())
    good = raw[raw.get("parse_issue", "").fillna("") == ""] if len(raw) else raw
    if len(good) == 0:
        return counts
    res = classify(good, run_id)
    counts["unique_solicitation_nsn_lines"] = len(res.pop("_deduped"))
    names = {"g_main_2026": "g_candidates_main_2026.csv", "g_pre2026_open": "g_pre2026_carry_forward.csv",
             "g_date_unresolved": "g_date_unresolved.csv", "review_amsc": "review_queue_amsc.csv",
             "non_g": "non_g_exclusions.csv", "excluded": "exclusion_log.csv", "fat": "first_article_lines.csv",
             "no_nomenclature_match": "text_hit_not_in_nomenclature.csv", "pre2026_closed": "pre2026_closed.csv",
             "broad_context": "broad_context_review.csv"}
    for k, fname in names.items():
        pd.DataFrame(res[k]).to_csv(out / fname, index=False)
        counts[k] = len(res[k])
    to_db = [(k, r) for k in ("g_main_2026", "g_pre2026_open", "g_date_unresolved", "review_amsc") for r in res[k]]
    counts["database"] = upsert(to_db, run_id)
    return counts


# ---------------------------------------------------------------- browser automation
MARK_JS = """
() => {
  // The RFQ page has TWO forms: 'RFQ Database Search' (left) and 'RFQ Solicitation Text Search' (right).
  // Mark the Text Search box and its Query button so we never type into the left form.
  document.querySelectorAll('[data-aero]').forEach(e => e.removeAttribute('data-aero'));
  const inTextSearch = el => {
    let n = el;
    for (let i = 0; i < 12 && n; i++) {
      n = n.parentElement;
      if (!n) break;
      const t = (n.innerText || '').toUpperCase();
      if (t.includes('QUERY STRING')) return !t.includes('SEARCH VALUE');
    }
    return false;
  };
  const box = [...document.querySelectorAll('textarea, input[type=text]')].find(inTextSearch);
  if (box) box.setAttribute('data-aero', 'qbox');
  const label = e => ((e.value || e.innerText || e.textContent || '') + '').trim().toUpperCase();
  const btns = [...document.querySelectorAll('input[type=submit], input[type=button], button, a')]
               .filter(e => label(e) === 'QUERY');
  const btn = btns.find(inTextSearch) || btns[btns.length - 1];
  if (btn) btn.setAttribute('data-aero', 'qbtn');
  return {box: !!box, button: !!btn, buttons_found: btns.length};
}
"""


def settle(page, timeout=60_000):
    """Wait until the page has finished loading (DIBBS pages post back and reload often)."""
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except Exception:
        pass


def safe_eval(page, js, tries=6):
    """page.evaluate that survives 'execution context was destroyed' (page reloaded mid-call)."""
    for i in range(tries):
        try:
            settle(page)
            return page.evaluate(js)
        except Exception as e:
            if "context was destroyed" not in str(e) and "navigat" not in str(e).lower() or i == tries - 1:
                raise
            time.sleep(2)


def find_search_page(context):
    """Return the open tab that shows the Text Search form (the user may have opened a new tab)."""
    for _ in range(10):
        for pg in reversed(context.pages):
            settle(pg, 30_000)
            try:
                if "QUERY STRING" in pg.inner_text("body").upper():
                    pg.bring_to_front()
                    return pg
            except Exception:
                pass
        time.sleep(2)
    return None


def click_no_wait(page, locator, what):
    """Click without Playwright's built-in 30 s navigation wait. DIBBS searches can take minutes;
    our own polling loop does the (longer, visible) waiting instead."""
    try:
        locator.click(no_wait_after=True, timeout=60_000)
    except Exception as e:
        if "Timeout" not in type(e).__name__ and "timeout" not in str(e).lower():
            raise
        # the click itself happened; only the navigation wait timed out - carry on and poll
    print(f"    {what}", end="", flush=True)


CLEAR_FOUND_JS = """
() => {
  // Erase the old 'Found N records' text before a click, so the next 'Found' we see is NEW.
  // (Bug seen 2026-09-30: later searches were read from the stale PANEL page.)
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let t, n = 0;
  while ((t = w.nextNode())) {
    if (/Found \\d+ records/.test(t.nodeValue)) { t.nodeValue = t.nodeValue.replace(/Found \\d+ records/, 'AERO-WAITING'); n++; }
  }
  return n;
}
"""


def clear_old_results(page):
    safe_eval(page, CLEAR_FOUND_JS)


def wait_for_results(page, dialogs, raw_dir, name, limit=600):
    """Poll every 5 s until a NEW 'Found N records' is on the page. Popups and time-outs
    become visible errors with a screenshot."""
    waited = 0
    while True:
        if dialogs:
            debug_dump(page, raw_dir, f"{name}_popup")
            raise RuntimeError(f"DIBBS showed a popup: {dialogs[0]!r}")
        try:
            if re.search(r"Found \d+ records", page.inner_text("body")):
                break
        except Exception:
            pass                                          # page is mid-navigation
        if waited >= limit:
            debug_dump(page, raw_dir, f"{name}_timeout")
            raise RuntimeError(f"No results after {limit} s - screenshot saved in the raw folder")
        time.sleep(5); waited += 5
        print(".", end="", flush=True)
    print(f" {waited}s")
    settle(page)
    time.sleep(1)


def safe_content(page, tries=6):
    for i in range(tries):
        try:
            settle(page)
            return page.content()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2)


SEARCH_URL = {"url": None}


def set_sort_newest(page):
    """Pick a newest-first sort if DIBBS offers one; always report which options exist."""
    try:
        for sel in page.locator("select").all():
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            if not any("match" in o.lower() for o in opts):      # Text Search sort list has "Best Match";
                continue                                          # skip the left form's Scope list
            def newest_first(o):
                l = o.lower()
                if "newest" in l and "oldest" in l:
                    return l.index("newest") < l.index("oldest")
                return any(k in l for k in ("newest", "most recent", "descending", "latest"))
            pick = [o for o in opts if newest_first(o)]
            if pick:
                sel.select_option(label=pick[0])
                return f"{pick[0]} (options: {' | '.join(opts)})"
            return f"unchanged (Best Match) (options: {' | '.join(opts)})"
    except Exception:
        pass
    return "unchanged (Best Match)"


def debug_dump(page, raw_dir, name):
    try:
        page.screenshot(path=str(raw_dir / f"{name}.png"), full_page=True)
        (raw_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def run_search(page, term, max_pages, delay, raw_dir, run_id):
    safe = re.sub(r"[^A-Za-z0-9]+", "_", term)
    dialogs = []
    page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    found = {"box": False, "button": False}
    for attempt in range(4):
        found = safe_eval(page, MARK_JS)
        if found["box"] and found["button"]:
            break
        time.sleep(3)
        if attempt == 2 and SEARCH_URL["url"]:            # page got lost: reload the search page
            page.goto(SEARCH_URL["url"], timeout=120_000); settle(page)
    if not found["box"]:
        debug_dump(page, raw_dir, f"{safe}_no_box")
        raise RuntimeError("Text Search 'Query String' box not found - are you on the RFQs page with "
                           "'RFQ Solicitation Text Search' on the right?")
    if not found["button"]:
        debug_dump(page, raw_dir, f"{safe}_no_button")
        raise RuntimeError("Text Search 'Query' button not found")
    box = page.locator("[data-aero=qbox]")
    box.fill(term)
    if box.input_value().strip() != term:
        raise RuntimeError(f"Typed '{term}' but the box contains '{box.input_value()}'")
    sort_used = set_sort_newest(page)
    clear_old_results(page)
    click_no_wait(page, page.locator("[data-aero=qbtn]"), "searching")
    wait_for_results(page, dialogs, raw_dir, safe)
    check = safe_eval(page, MARK_JS)                     # the results page should still show our term
    if check["box"]:
        shown = page.locator("[data-aero=qbox]").input_value().strip()
        if shown and shown.upper() != term.upper():
            debug_dump(page, raw_dir, f"{safe}_wrong_term")
            raise RuntimeError(f"Results page shows query '{shown}', expected '{term}' - not saving stale results")
    body = page.inner_text("body")
    reported = int(re.search(r"Found (\d+) records", body).group(1))
    rows, pages, stop_reason = [], 0, "all pages retrieved"
    for page_no in range(1, max_pages + 1):
        fetched = utc_stamp()
        settle(page)
        body = page.inner_text("body")
        links = safe_eval(page, "() => [...document.querySelectorAll('a')].map(e => [e.innerText.trim(), e.href])")
        (raw_dir / f"{safe}_p{page_no}.html").write_text(safe_content(page), encoding="utf-8")
        (raw_dir / f"{safe}_p{page_no}.txt").write_text(body, encoding="utf-8")
        (raw_dir / f"{safe}_p{page_no}.links.json").write_text(json.dumps(links))
        rows += parse_results_text(body, links, term, page_no, fetched)
        pages = page_no
        if reported == 0:
            break
        nxt = page.locator("a").filter(has_text=re.compile(rf"^\s*{page_no + 1}\s*$"))
        if nxt.count() == 0:
            nxt = page.locator("a").filter(has_text=re.compile(r"^\s*(\.\.\.|>|Next)\s*$", re.I))
        if nxt.count() == 0:
            if len({r['solicitation'] for r in rows}) < reported:
                stop_reason = "no next-page link found before all records were retrieved"
            break
        if page_no == max_pages:
            stop_reason = f"stopped at --max-pages {max_pages}"
            break
        clear_old_results(page)
        click_no_wait(page, nxt.first, f"page {page_no + 1}")
        wait_for_results(page, dialogs, raw_dir, f"{safe}_p{page_no + 1}")
        time.sleep(delay)                                    # be polite to a government server
    retrieved = len({r["solicitation"] for r in rows})
    issued = pd.to_datetime(pd.Series([r.get("issued") for r in rows], dtype=str), format="%m-%d-%Y", errors="coerce")
    first = rows[0]["solicitation"] if rows else ""
    return rows, {"first_solicitation": first, "earliest_issued_seen": str(issued.min().date()) if issued.notna().any() else "",
                  "latest_issued_seen": str(issued.max().date()) if issued.notna().any() else "","term": term, "records_reported": reported, "pages_fetched": pages,
                  "solicitations_retrieved": retrieved, "lines_parsed": len(rows), "sort": sort_used,
                  "complete": retrieved >= reported, "stop_reason": stop_reason, "status": "SUCCESS"}


def download_pdfs(context, candidates_csv: Path, pdf_dir: Path) -> dict:
    """Download solicitation PDFs with the browser's own session.
    The PDF server can show its own DoD notice first (seen 2026-09-30: HTTP 200 but an HTML page).
    In that case the script opens one PDF in a tab, YOU accept the notice, and it retries."""
    df = pd.read_csv(candidates_csv, dtype=str) if candidates_csv.exists() and candidates_csv.stat().st_size > 1 else pd.DataFrame()
    pdf_dir.mkdir(parents=True, exist_ok=True)
    stats = {"requested": 0, "downloaded": 0, "already_had": 0, "failed": []}
    prompted = False
    rows = df.drop_duplicates("sol_key").to_dict("records") if len(df) else []
    for r in rows:
        dest = pdf_dir / f"{r['sol_key']}.PDF"
        stats["requested"] += 1
        if dest.exists():
            stats["already_had"] += 1; continue
        url = str(r.get("pdf_url") or "")
        if not url.startswith("http"):
            stats["failed"].append(f"{r['sol_key']}: no PDF link"); continue
        for attempt in (1, 2):
            try:
                resp = context.request.get(url, timeout=120_000)
                body = resp.body()
            except Exception as e:
                body, resp = b"", None
                err = str(e)
            if body[:4] == b"%PDF":
                dest.write_bytes(body); stats["downloaded"] += 1; break
            if attempt == 1 and not prompted:
                prompted = True
                (pdf_dir / "_first_non_pdf_response.html").write_bytes(body or b"")
                tab = context.new_page()
                try:
                    tab.goto(url, timeout=120_000)
                except Exception:
                    pass                                  # a direct PDF download also lands here
                input("\nA PDF link returned a web page instead of the PDF (probably a notice banner).\n"
                      "In the new browser tab: accept the notice if one is shown. Then press Enter here.\n")
                continue
            stats["failed"].append(f"{r['sol_key']}: HTTP {resp.status if resp else '?'}, not a PDF")
            break
        time.sleep(1)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=["roots", "full"], help="roots = root words (fewer searches); full = every brief phrase")
    ap.add_argument("--terms", nargs="*", default=[], help="explicit search terms")
    ap.add_argument("--terms-file", help="text file, one search term per line (e.g. NSNs from SAM.gov matches)")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between page requests")
    ap.add_argument("--pdfs", action="store_true", help="download PDFs for AMSC-G candidates")
    ap.add_argument("--pdfs-only", help="download PDFs for an existing run folder, e.g. data/runs/dibbs_<id> (no searching)")
    ap.add_argument("--start-at", help="skip preset terms before this one (resume an interrupted run)")
    ap.add_argument("--reparse", nargs="+", help="re-parse one or more saved data/dibbs_raw/<run_id> folders "
                                                  "into ONE combined output (no website access)")
    args = ap.parse_args()

    run_id = f"dibbs_{utc_stamp()}"
    counts = {"run_id": run_id, "source": "DIBBS", "cutoff_provisional": CUTOFF}

    if args.pdfs_only:
        from playwright.sync_api import sync_playwright
        run_dir = Path(args.pdfs_only)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(DIBBS_HOME, timeout=120_000)
            input("\nAccept the DoD notice banner in the browser, then press Enter here.\n")
            stats = download_pdfs(context, run_dir / "g_candidates_main_2026.csv", DATA / "dibbs_pdfs")
            browser.close()
        counts.update({"mode": f"pdf download for {run_dir}", "pdfs": stats,
                       "status": "SUCCESS" if not stats["failed"] else "PARTIAL"})
        log_run(counts); print(json.dumps(counts, indent=2)); return

    if args.reparse:
        rows, seen_terms = [], set()
        for folder in args.reparse:                    # later folders win if a term appears twice
            raw_dir = Path(folder)
            for txt in sorted(raw_dir.glob("*_p*.txt")):
                m = re.match(r"(.+)_p(\d+)$", txt.stem)
                links_file = raw_dir / f"{txt.stem}.links.json"
                if not m or not links_file.exists():
                    continue
                rows = [r for r in rows if not (r["search_term"] == m.group(1) and r.get("_folder") != folder)]
                for r in parse_results_text(txt.read_text(encoding="utf-8"), json.loads(links_file.read_text()),
                                            m.group(1), int(m.group(2)), f"saved page {raw_dir.name}/{txt.name}"):
                    r["_folder"] = folder
                    rows.append(r)
        for r in rows:
            r.pop("_folder", None)
        counts["mode"] = f"offline reparse of {len(args.reparse)} saved folder(s): {', '.join(args.reparse)}"
        counts = write_outputs(rows, [], run_id, counts)
        counts["status"] = "SUCCESS (offline reparse of saved live pages)"
        log_run(counts); print(json.dumps(counts, indent=2, default=str)); return

    if args.terms_file:
        args.terms += [t.strip() for t in Path(args.terms_file).read_text(encoding="utf-8").splitlines() if t.strip()]
    terms = args.terms or (ROOT_TERMS if args.preset == "roots" else FULL_TERMS if args.preset == "full" else [])
    if not terms:
        sys.exit("Give --terms or --preset")
    if args.start_at:
        up = [t.upper() for t in terms]
        if args.start_at.upper() not in up:
            sys.exit(f"--start-at {args.start_at!r} is not in the term list")
        terms = terms[up.index(args.start_at.upper()):]
    raw_dir = DATA / "dibbs_raw" / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    counts.update({"mode": "live", "terms": len(terms), "max_pages": args.max_pages, "raw_pages": str(raw_dir)})

    from playwright.sync_api import sync_playwright
    all_rows, summary = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(DIBBS_HOME, timeout=120_000)
            input("\n1) In the browser: read and accept the DoD notice banner.\n"
                  "2) Open RFQ -> Solicitation Text Search (the page with the 'Query String' box).\n"
                  "3) Press Enter here to start.\n")
            page = find_search_page(context)
            if page is None:
                raise RuntimeError("No open tab shows the 'Query String' Text Search form")
            counts["search_page_url"] = page.url
            SEARCH_URL["url"] = page.url
            print(f"Using tab: {page.url}")
            for i, term in enumerate(terms, 1):
                print(f"[{i}/{len(terms)}] {term} ...", flush=True)
                try:
                    # Always start each term from a fresh search page. (2026-09-30: searching again from a
                    # results page returned the PREVIOUS term's results.)
                    page.goto(SEARCH_URL["url"], timeout=120_000)
                    settle(page)
                    rows, info = run_search(page, term, args.max_pages, args.delay, raw_dir, run_id)
                    prev = next((x for x in reversed(summary) if x.get("status") == "SUCCESS"), None)
                    if (prev and info["records_reported"] > 0 and info["records_reported"] == prev["records_reported"]
                            and info["first_solicitation"] == prev["first_solicitation"]):
                        raise RuntimeError(f"Results identical to previous term '{prev['term']}' "
                                           f"({info['records_reported']} records, same first solicitation) - "
                                           "treated as stale, not saved")
                    all_rows += rows
                    print(f"    reported {info['records_reported']}, retrieved {info['solicitations_retrieved']}"
                          f" ({info['stop_reason']})")
                except Exception as e:
                    info = {"term": term, "status": "FAILED", "error": f"{type(e).__name__}: {e}"}
                    print(f"    FAILED: {info['error']}")
                summary.append(info)
            counts = write_outputs(all_rows, summary, run_id, counts)
            if args.pdfs:
                counts["pdfs"] = download_pdfs(context, DATA / "runs" / run_id / "g_candidates_main_2026.csv",
                                               DATA / "dibbs_pdfs")
            browser.close()
    except (Exception, KeyboardInterrupt) as e:
        counts.update({"status": "FAILED", "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
                       "terms_completed": [s_["term"] for s_ in summary]})
        log_run(counts)
        print("\n*** RUN FAILED/INTERRUPTED - this is NOT a zero-result run ***\n" + counts["error"])
        sys.exit(1)

    failed = [s["term"] for s in summary if s.get("status") == "FAILED"]
    incomplete = [s["term"] for s in summary if s.get("status") == "SUCCESS" and not s.get("complete")]
    counts["terms_failed"], counts["terms_incomplete"] = failed, incomplete
    counts["status"] = "PARTIAL" if failed else "SUCCESS"
    (DATA / "runs" / run_id / "stage_counts.json").write_text(json.dumps(counts, indent=2, default=str))
    log_run(counts)
    print(json.dumps(counts, indent=2, default=str))


if __name__ == "__main__":
    main()