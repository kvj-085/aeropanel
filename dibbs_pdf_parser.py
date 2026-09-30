"""
DIBBS solicitation PDF parser - Aeropanel Case Study B
Usage:  python dibbs_pdf_parser.py [folder]        (default: data/dibbs_pdfs)

Reads each downloaded RFQ PDF and extracts, with the PDF page as the source:
  - line items (CLIN, PR, unit, quantity, NSN) - first-article (FAT) lines flagged, never valued
  - exact-NSN procurement history (CAGE, contract, qty, unit cost, award date)
  - set-aside clause and other eligibility gates (first article, CMMC, export control, ...)
  - a screening value = supported quantity x most recent history unit price
Outputs to data/pdf_extracts/<timestamp>/
"""
import re, sys, json
from pathlib import Path
from datetime import datetime
import pandas as pd
from pypdf import PdfReader
from common import DATA, norm_nsn, norm_sol, utc_stamp, log_run

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
SET_ASIDES = [("52.219-6", "Total small business set-aside"), ("52.219-7", "Partial small business set-aside"),
              ("52.219-3", "HUBZone set-aside"), ("52.219-27", "SDVOSB set-aside"),
              ("52.219-30", "WOSB set-aside"), ("52.219-29", "EDWOSB set-aside")]
GATES = {  # patterns chosen to avoid DLA boilerplate that appears in EVERY solicitation
         "first_article_required": r"FIRST ARTICLE APPROVAL|GOVERNMENT FIRST ARTICLE TEST|FIRST ARTICLE TEST REQUIREMENT \(FAT\)",
         "cmmc_required": r"\bCMMC\b",
         "export_controlled_data": r"RQ032|EXPORT CONTROL OF TECHNICAL DATA|\bITAR\b",
         "critical_application_item": r"CRITICAL APPLICATION ITEM",
         "full_and_open_phrase": r"FULL AND OPEN COMPETITION APPLY",
         "origin_inspection": r"ORIGIN INSPECTION REQUIRED",
         "generic_late_quote_boilerplate": r"anticipate quoting on a solicitation after the closing date"}
CLIN_RE = re.compile(r"^\s*(\d{4})\s+(\d{10})\s+(?:(\d{4})\s+)?([A-Z]{2})\s+([\d,]+\.\d+)\s*$.*?NSN/MATERIAL:\s*(\S+)",
                     re.M | re.S)
HIST_RE = re.compile(r"^\s*([0-9A-Z]{5})\s+([A-Z0-9]{8,20})\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+(\d{8})\s+([YN])\s*$", re.M)
DATE_RE = re.compile(r"\b(20\d\d) (JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC) (\d\d)\b")


def pdf_pages(path: Path):
    return [p.extract_text() or "" for p in PdfReader(str(path)).pages]


def page_of(pages, pattern):
    for i, t in enumerate(pages, 1):
        if re.search(pattern, t, re.I):
            return i
    return None


def parse_pdf(path: Path):
    pages = pdf_pages(path)
    text = "\n".join(pages)
    sol = re.search(r"REQUEST NO\.\s*([A-Z0-9-]{13,20})", text)
    sol = sol.group(1) if sol else path.stem
    dates = [f"{y}-{MONTHS[m]:02d}-{d}" for y, m, d in DATE_RE.findall("\n".join(pages[:1]))]
    summary = {"sol_key": norm_sol(sol), "solicitation": sol, "pdf_file": path.name,
               "pdf_date_issued": dates[0] if dates else "", "pdf_return_by": dates[1] if len(dates) > 1 else "",
               "pages": len(pages)}
    found = [(c, label) for c, label in SET_ASIDES if c in text]
    summary["set_aside"] = "; ".join(label for _, label in found) or "No set-aside clause found (check SF18 block)"
    summary["set_aside_page"] = page_of(pages, re.escape(found[0][0])) if found else ""
    for k, pat in GATES.items():
        pg = page_of(pages, pat)
        summary[k] = bool(pg)
        summary[k + "_page"] = pg or ""

    lines = []
    for clin, pr, prli, ui, qty, nsn in CLIN_RE.findall(text):
        is_fat = nsn.upper().startswith("0001S") or pr == "0000000000"
        lines.append({"sol_key": summary["sol_key"], "clin": clin, "pr": pr, "prli": prli or "", "unit": ui,
                      "quantity": float(qty.replace(",", "")), "nsn": norm_nsn(nsn) or nsn,
                      "is_first_article_line": is_fat,
                      "line_key": f"DIBBS|{summary['sol_key']}|CLIN{clin}",
                      "source": f"{path.name}, CLIN table"})

    hist = []
    hm = re.search(r"Procurement History for NSN/FSC:\s*(\d{9})/(\d{4})", text)
    hist_nsn = f"{hm.group(2)}-{hm.group(1)[:2]}-{hm.group(1)[2:5]}-{hm.group(1)[5:]}" if hm else ""
    hist_page = page_of(pages, r"Procurement History")
    for cage, contract, q, cost, awd, surplus in HIST_RE.findall(text):
        hist.append({"sol_key": summary["sol_key"], "history_nsn": hist_nsn, "cage": cage, "contract": contract,
                     "quantity": float(q.replace(",", "")), "unit_cost": float(cost.replace(",", "")),
                     "award_date": f"{awd[:4]}-{awd[4:6]}-{awd[6:]}", "surplus": surplus,
                     "source": f"{path.name} p.{hist_page}"})

    # ---- screening value: most recent non-surplus exact-NSN history price
    product_lines = [l for l in lines if not l["is_first_article_line"]]
    usable = sorted([h for h in hist if h["surplus"] == "N"], key=lambda h: h["award_date"])
    if usable:
        last = usable[-1]
        age = (datetime.now() - datetime.fromisoformat(last["award_date"])).days / 365.25
        summary.update({"hist_unit_price": last["unit_cost"], "hist_award_date": last["award_date"],
                        "hist_order_qty": last["quantity"], "hist_contract": last["contract"], "hist_cage": last["cage"],
                        "hist_source": last["source"], "hist_age_years": round(age, 1),
                        "hist_records": len(usable),
                        "price_note": ("history older than 5 years - compare with DLA management price"
                                       if age > 5 else "recent exact-NSN history")})
        for l in product_lines:
            same_nsn = (not hist_nsn) or (l["nsn"] == hist_nsn)
            l["unit_price_selected"] = last["unit_cost"] if same_nsn else None
            l["price_basis"] = ("exact-NSN history, most recent award" if same_nsn
                                else "history NSN differs from line NSN - unpriced")
            l["estimated_value"] = l["quantity"] * last["unit_cost"] if same_nsn else None
    else:
        summary["price_note"] = "no procurement history in PDF - investigate DLA management price; line stays unpriced"
        for l in product_lines:
            l.update({"unit_price_selected": None, "price_basis": "unpriced", "estimated_value": None})
    summary["product_lines"] = len(product_lines)
    summary["fat_lines"] = len(lines) - len(product_lines)
    summary["estimated_value_total"] = sum(l["estimated_value"] or 0 for l in product_lines) if usable else None
    summary["parse_warnings"] = "; ".join(w for w, bad in [("no CLINs parsed", not lines),
                                                           ("no dates parsed", not dates)] if bad)
    return summary, lines, hist


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "dibbs_pdfs"
    pdfs = sorted(folder.glob("*.PDF")) + sorted(folder.glob("*.pdf"))
    run_id = f"pdfparse_{utc_stamp()}"
    out = DATA / "pdf_extracts" / run_id
    out.mkdir(parents=True, exist_ok=True)
    summaries, all_lines, all_hist, failures = [], [], [], []
    for p in pdfs:
        try:
            s, l, h = parse_pdf(p)
            summaries.append(s); all_lines += l; all_hist += h
        except Exception as e:
            failures.append({"pdf": p.name, "error": f"{type(e).__name__}: {e}"})
    pd.DataFrame(summaries).to_csv(out / "pdf_summary.csv", index=False)
    pd.DataFrame(all_lines).to_csv(out / "pdf_lines.csv", index=False)
    pd.DataFrame(all_hist).to_csv(out / "procurement_history.csv", index=False)
    pd.DataFrame(failures).to_csv(out / "pdf_failures.csv", index=False)
    counts = {"run_id": run_id, "source": "DIBBS PDFs", "pdfs": len(pdfs), "parsed": len(summaries),
              "failed": len(failures), "lines": len(all_lines), "history_rows": len(all_hist),
              "status": "SUCCESS" if not failures else "PARTIAL"}
    log_run(counts)
    print(json.dumps(counts, indent=2))
    print(f"Outputs: {out}")


if __name__ == "__main__":
    main()
