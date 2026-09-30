# Aeropanel Case Study B: DIBBS + SAM.gov Opportunity Feed

Working notes, decisions and run instructions. This file is the single source of truth for the
project: what we built, what we tried, what we learned about each data source, and what is left.

---

## 1. The problem in plain words

Aeropanel makes control panels, lightplates, bezels, switches, lighting, harnesses and similar parts.
The government posts purchase requests ("solicitations") on two websites:

- **DIBBS**: the Defense Logistics Agency's (DLA) bid board
- **SAM.gov**: the main federal contracts site

We must build a **working, repeatable system** that:

1. Finds every **2026** solicitation (Jan 1, 2026 to the reporting cutoff) matching Aeropanel's keywords, open *or* past deadline.
2. Keeps only items whose sourcing code is **AMSC G** (the government owns the full drawings, so anyone qualified can build it). Missing or unverified codes go to a **review queue**. Other codes go to an **exclusion log**.
3. Removes **awarded or cancelled** work and records why.
4. **Counts each solicitation line once**, even across amendments, multiple lines, both websites and overlapping keywords.
5. **Estimates value** = quantity × most recent price paid for that exact NSN (fallback: DLA management price; otherwise "unpriced", never $0).
6. Reports by **category (C01–C10)** and **status**: open / late route verified / follow-up needed / unresolved.
7. **Proves it works**: live runs, saved raw data, counts at every stage, 20–30 hand-checked records, a rerun with no duplicates, and a failed-source test.

**Deadlines:** submit Thursday night, Oct 1, 2026 (reply to Jonah's email). On-site demo: Friday, Oct 2, in Boonton.

**Rules we must follow:** own personal accounts only. Don't register Aeropanel, share credentials,
bypass restrictions or submit bids. No API keys or passwords in the submission. Separate verified facts from estimates.

---

## 2. Project files

| File | What it does |
|---|---|
| `common.py` | Shared rules: keyword list (copied from the brief), text normalization, matching, exclusions, NSN/solicitation formatting, run log. Both sources use this, so they're judged identically. |
| `sam_pipeline.py` | SAM.gov: snapshots the CSV (dated + versioned), filters, detects changes vs the previous snapshot, stores matches in the database, logs the run. |
| `dibbs_collector.py` | DIBBS: drives the Text Search in a browser, saves every results page, parses each line (NSN, nomenclature, AMSC, qty, dates), classifies, stores in the database. Optional PDF download. |
| `dibbs_status.py` | For every G candidate: fetches the official RFQ package page (Status, Issue Date, Return By; page saved as evidence) and downloads the solicitation PDF, in one browser session. Resumable. |
| `build_report.py` | Merges everything into the workbook: status rules, cross-post dedupe, line quantities, prices, values, Summary (formula-driven, brief layout), G_Main list, all separate populations, manual-check sheet, run log. |
| `dibbs_pdf_parser.py` | Reads solicitation PDFs: line items (CLINs), procurement history (past prices), set-aside, eligibility gates, screening value. |
| `requirements.txt` | Python packages. |
| `.env.example` | Template for `.env` (holds your SAM.gov key and optional download link). Never submit `.env`. |
| `data/` | Created by the scripts: snapshots, raw pages, run outputs, `pipeline.db`, `run_log.jsonl`. |

### Setup (once)

```
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env        (then edit .env)
```

### Typical run sequence

```
# SAM.gov: after downloading the CSV (see section 6)
python sam_pipeline.py --from-downloads

# DIBBS: search (about 1.5 h), then status + PDFs for the G candidates
python dibbs_collector.py --preset roots --max-pages 20
python dibbs_status.py --limit 20                       # quick test
python dibbs_status.py --resume data/dibbs_status/<id>  # finish the rest

# Parse downloaded PDFs (prices, lines, gates)
python dibbs_pdf_parser.py

# Build the workbook
python build_report.py

# Failed-source test (must show FAILED, not zero results)
python sam_pipeline.py --file does_not_exist.csv
```

Every run writes to `data/runs/<run_id>/` and appends one line (SUCCESS / PARTIAL / FAILED) to `data/run_log.jsonl`.

---

## 3. What we learned about each source (the investigation log)

### SAM.gov API: blocked, documented
- Got a personal API key (emailed). Stored in `.env`, never in code.
- Every request to `https://api.sam.gov/opportunities/v2/search` **and** `/prod/opportunities/v2/search`
  returned **HTTP 404 with an empty body** in ~2 ms (`server: istio-envoy`). This happened even with a fake key,
  so the key was never checked.
- GSA docs say 404 can mean "no data," but the query (all notices, Sept 2026) definitely has data.
  One third-party developer reported the same empty 404 when the caller's region/network is blocked.
- Quota note: non-federal personal keys may get as few as ~10 requests/day.
- **Decision:** document as an external blocker (exact request, response headers, date). Use the official bulk CSV instead.
  Worth one retry from a different network (phone hotspot).

### SAM.gov bulk CSV (Data Services): working
- Page: `https://sam.gov/data-services/Contract%20Opportunities/datagov?privacy=Public`
  → click the CSV → scroll → Accept → file downloads.
- `ContractOpportunitiesFullCSV.csv`: 82,605 rows, cp1252 encoding, posted dates 2008 → Sept 28, 2026.
  It holds **active** notices only. Archived 2026 notices are in the **Archived Data** folder, which must be added for full 2026 coverage.
- Useful columns: `NoticeId`, `Sol#`, `PostedDate`, `Type`, `BaseType`, `ResponseDeadLine`, `Active`,
  `AwardNumber/AwardDate/Award$/Awardee`, `ClassificationCode`, `Description`, `Link`.
- **No AMSC field.** SAM.gov matches can only reach the G-only list if we find their NSN and confirm AMSC on DIBBS/DLA.
- `Type` = "Award Notice" rows are award evidence. Some have `BaseType` = the original solicitation type.
- The same event can appear twice with different NoticeIds (e.g., two "USACE New Orleans Industry Day" rows), so dedupe carefully.
- Descriptions contain amendment text ("Amendment 1, September 28, 2026 …").

First run results (active file only):

| Stage | Count |
|---|---|
| Rows in file | 82,605 |
| Posted in 2026 window | 61,944 |
| Actionable type (Solicitation / Combined Synopsis) | 41,033 |
| Keyword match in **title** | 1,501 |
| Keyword match in **description only** (mostly boilerplate) | 3,720 |
| Excluded by rules | 54 |
| Title matches mentioning an NSN | (rerun to get) |
| Award notices (for status checks) | 15,277 |

### DIBBS: what each page does

| Page | What we found |
|---|---|
| Home page | You must accept a DoD notice banner (normal public access). |
| RFQs page (Solicitations → RFQs) | Holds **two forms side by side**: left = "RFQ Database Search" (NSN/part number, with a **Scope** dropdown, default "Open - RFQs available for quoting"); right = "RFQ Solicitation Text Search" (subtitle: *search open RFQ solicitation documents*; braces `{ }` = exact phrase; `*` = wildcard). First collector run typed into the wrong form. Fixed by targeting the box under "Query String". |
| RFQ search (NSN / solicitation) | Shows **open RFQs only**. The example solicitation returned 0 because its deadline had passed. |
| Direct link `rfq/rfqrec.aspx?sn=<SOL>` ("Package View") | Works even for closed/removed items. Shows **Status** (Open / Removed), issue date, return-by date, lines (NSN, nomenclature, PR number, qty), PDF link. |
| Solicitation PDF | The richest source: quantity per line (CLIN), **procurement history** (past prices for the exact NSN), set-aside clause, first article, CMMC, export control, delivery days. **AMSC is not printed** in the PDF. |
| **RFQ Solicitation Text Search** | Our main DIBBS collector route. Returns **closed** solicitations too, and shows **AMSC per line**, NAICS, quantity, approved source (CAGE/part number), issued, return-by, post/updated date, PR numbers. ~160,567 documents indexed. Slow. |
| Awards search (Awards menu) | "DLA Public Awards Database". Search categories: Awardee CAGE, Award/Basic Number, Delivery Order, Award Basic Delivery Order Counter, **Solicitation**, **Purchase Request (PR)**, **NSN/Part Number**, **Nomenclature**. Scope: Posted Today / Past 15 Days / **All** / posted or awarded for a date range. Results follow DIBBS document-retention policy. First attempt failed because "Awardee CAGE" (5-char company code) was selected. |
| RFQ Database Search (left form) Scope | Open (available for quoting) / Today's / Recent (15 days) / **All - RFQs whether available for quoting or not**. Useful for NSN lookups and status checks beyond open items. |
| Quote button | Goes to a login page for registered vendors. Not needed and not allowed (no bids). |

Text Search behaviour we observed (these become rules in the code):
1. **Full-text hits include boilerplate.** Searching "panel" returned a nonmetallic sheet because its packaging text says
   "one panel of palletized load". → We match keywords against the **nomenclature** only. Text hits outside it are logged.
2. **First-article (FAT) lines can show AMSC G** (e.g., `GOVERNMENT FIRST ARTIC`, NSN `0001S00000052`). → FAT lines are never products and never valued.
3. **Quantity can be aggregated** (results page said 4; package page showed 4 PR lines of 1 each). → PDF CLINs are the line-level truth.
4. **Issue date can differ from post date** (Issued 09-30-2026, posted 2026-09-29). → We use **Issued** as the publication date (it matches the PDF's "Date Issued"). Rule documented.
5. **Removed solicitations vanish from the search index.** SPE7M926T0038 (status Removed) returned 0 hits by solicitation number, NSN and PR number. → Save a snapshot every run. A record we have seen once is never lost.
6. Text Search reaches **before 2026**: the first test run (sort: oldest first) returned 4 pre-2026 closed items. Each term's earliest/latest issued date seen is now recorded in `term_summary.csv`.
6b. **"Document Post/Updated" is not a publication date.** In the 09-30 pages, most results (issued anywhere from Oct 2025 to Aug 2026) share the same Post/Updated stamp, `2026-08-21 02:41:27`, apparently a bulk re-index. This confirms the rule: publication date = **Issued**.
6c. Test/pseudo lines use NSNs starting `0001S` (GOVERNMENT/CONTRACTOR FIRST ARTIC, PRODUCTION LOT TESTING). All go to `first_article_lines.csv`, never valued.
7. **Package page "Open" can outlive the deadline.** SPE4A626T45E6 (return-by 05-18-2026) and SPE7M126T125G (06-15-2026) still show Status **Open** with an active Quote link. DIBBS's own Scope wording defines Open as "RFQs available for quoting". This is the strongest late-quotation evidence available and is recorded with a check timestamp.
8. **DLA item names are "NOUN, MODIFIERS".** "INS ERT, PANEL FASTEN" is an insert (a fastener part), not a panel. Broad keywords (PANEL, SWITCH, PLATE, WINDOW, LENS, ACTUATOR, MARKING, LEGEND) only count when they are in the noun part (before the first comma). Otherwise the line goes to `broad_context_review.csv`, outside G totals.

### Collector incidents (and what they taught us)

| Date | What happened | Fix |
|---|---|---|
| 09-29 | Typed into the left form ("Search Value(s)") instead of Text Search | Target the box under "Query String" and its own Query button |
| 09-29 | "Execution context destroyed" (page reloaded mid-read) | Wait for load + retry reads; pick the tab that shows the form |
| 09-30 | Query click timed out after 30 s (search slower than Playwright's default) | Click without waiting, then poll for results every 5 s (up to 10 min) |
| 09-30 | **First full run (52 terms) read stale results.** Every term after PANEL reported PANEL's count (1,084) and saved PANEL's pages, because the old "Found 1084 records" text was still on screen when the new search started. The run was marked PARTIAL and not used. | Before each click, the old "Found N records" text is erased, so only a new result counts. After each search, the page's query box must show the term just searched; otherwise the term fails instead of saving wrong data. Each term records its first solicitation for spot checks. |
| 09-30 | 2-term test (PANEL, BEZEL) after the fix: BEZEL **still** returned PANEL's results (1,084 records, same first solicitation SPE8E526T4503), even though it genuinely waited for a new page. So a search submitted **from a results page** re-runs the previous query on DIBBS's side. | Every term now starts from a **freshly loaded search page**. A guard also fails any term whose count and first solicitation are identical to the previous term's, instead of saving it. The sort options DIBBS offers are logged per term (no newest-first option was found, so Best Match is used). |
| 09-30 | PDF downloads: HTTP 200 but a web page, not a PDF | The PDF server shows its own notice. The script now opens one PDF in a tab, you accept the notice, and it retries. `--pdfs-only <run folder>` downloads PDFs without re-searching. |

---

## 4. Where each required field comes from

| Field | DIBBS source | SAM.gov source |
|---|---|---|
| Solicitation number | Text Search result / PDF block 1 | `Sol#` |
| NSN | Text Search result / PDF CLIN table | Only if written in title/description (`nsn_found`) |
| Item description | Text Search nomenclature / PDF Section B | `Title`, `Description` |
| Publication date | Text Search "Issued" / PDF block 2 | `PostedDate` |
| Deadline | Text Search "Return By" / PDF block 10 | `ResponseDeadLine` |
| Quantity + unit | PDF CLIN table (line level) | Description/attachments (often missing → unvalued) |
| **AMSC** | **Text Search AMSC field** | **Not available** → look up the NSN on DIBBS/DLA |
| Historical price | PDF "Procurement History for NSN" | Not available → via NSN on DIBBS |
| DLA management price | Not yet sourced (PUB LOG / DLA item data, still to investigate) | – |
| Set-aside / gates | PDF clauses (52.219-6 etc., CMMC, RQ032, FAT) | `SetASide` column |
| Status (open/removed) | Package page Status | `Active`, `Type`, `ArchiveType` |
| Award evidence | DIBBS awards search (to do) | `Type` = Award Notice, `AwardNumber` |

---

## 5. Decisions log

| # | Decision | Why |
|---|---|---|
| 1 | Provisional cutoff **Oct 1, 2026 12:00 ET**, stated in every output | Jonah has not confirmed the common cutoff yet. Email sent asking. |
| 2 | SAM.gov via **official bulk CSV**, API documented as blocked | API returns empty 404 on every request. CSV has full descriptions and no daily quota. |
| 3 | DIBBS via **Text Search in a real browser (Playwright)** | Only DIBBS route found that returns closed solicitations **and** AMSC. The user accepts the banner herself (no bypassing). |
| 4 | Search **root words**, then match exact phrases locally on nomenclature | ~106 phrases × slow searches is impractical. Roots cover every phrase. Local matching avoids boilerplate hits. |
| 5 | Keyword matching on **title/nomenclature first**; description-only hits → review file | Proven boilerplate false positives ("MARKING", "panel of palletized load"). |
| 6 | Most specific (longest) keyword decides the primary category; all hits kept as tags | Brief: "assign the most specific primary category, retain keyword tags". |
| 7 | "Published" = DIBBS **Issued** date; SAM `PostedDate` | Matches the official PDF. Never infer year from "26" in a solicitation number. |
| 8 | FAT lines excluded from products and value | They are test requirements, not deliverables. |
| 9 | Price = most recent **non-surplus exact-NSN** award in the PDF history. Flag if > 5 years old. | Brief's preferred basis. Old prices flagged for comparison with DLA management price. |
| 10 | Generic "if you anticipate quoting after the closing date…" text is **not** a verified late route | It appears in every DLA RFQ and says DLA can still award to a timely offer. It's a lead, not evidence. |
| 11 | Every run logs SUCCESS / PARTIAL / FAILED; failures exit with an error | Brief: a failed source must never look like a zero-result run. |
| 12 | Snapshots are dated + versioned + SHA-256 fingerprinted; identical files aren't stored twice | Lets us show changes between downloads, including two downloads on the same day. |
| 13 | SQLite database keyed by NoticeId (SAM) and solicitation+NSN (DIBBS) | Reruns update rows instead of adding duplicates. Counts inserted/updated/unchanged prove it. |
| 15 | Text Search sorted **Newest → Oldest** | With page caps on huge terms, the most recent 2026 records are kept first. The cap is logged per term. |
| 16 | Broad keyword must be the item **noun** (DLA "NOUN, MODIFIER" naming) | Test run: "INSERT, PANEL FASTENER" matched PANEL but is a fastener insert. |
| 17 | DIBBS status **Open after return-by date** = late-quote route candidate, with check timestamp | DIBBS defines Open as "available for quoting". Strongest source-specific evidence available. Recorded separately from open-by-deadline. |
| 18 | PANEL matches in structural Federal Supply Classes are excluded: 1560 airframe structural, 2510 vehicle cab/body/frame, 5410/5411/5419 buildings & shelters, 5670/5680 building/construction materials (list in `common.py`, `STRUCTURAL_FSC`) | Brief excludes structural/airframe/body/shelter panels. Item names alone don't say "structural" (e.g. "PANEL ASSEMBLY" in FSC 5411 rigid wall shelters, "PANEL ASSEMBLY, ELEVATOR" in 1560 airframe). The FSC (first 4 digits of the NSN) does. Applied to SAM.gov via `ClassificationCode`. Each exclusion names the FSC. |
| 19 | Main DIBBS run: 52 root terms, 20-page cap (up to 2,000 results per term). Capped: OVERLAY, PLATE, MARKING, FUEL, CABLE ASSEMBLY, BOX. DIBBS itself reports at most 5,000 hits per search. | Time budget; each capped term is listed as incomplete coverage in the workbook. |
| 20 | Status per solicitation from the official DIBBS package page (saved), plus SAM.gov award notices matched by solicitation number. DIBBS Awards database not queried per solicitation (covered in manual checks). | 1,000+ solicitations; package pages give Status + dates in one fetch. |
| 21 | Lines without a solicitation-level status check stay in **Status unresolved**, never promoted | Brief: "do not promote an unchecked record by default". |
| 22 | SAM.gov notice with the same solicitation number as a DIBBS record = cross-post, counted once under DIBBS. SAM.gov AMSC comes from the DIBBS listing of the same NSN; otherwise review queue. | Dedupe rule + SAM.gov has no AMSC field. |
| 23 | Summary counts/values are Excel formulas over G_Main with helper flags (first line of each solicitation per category/status) | Distinct-solicitation counts are recomputed, never added across rows; summary recalculates if rows are edited. |
| 14 | Gate detection uses specific phrases (e.g., "FIRST ARTICLE APPROVAL", "RQ032") | Broad words like "first article" and "export-controlled" appear in boilerplate in every PDF. |

---

## 6. Automating the SAM.gov download (and demo-day plan)

**Important:** the brief says *"processing a saved file alone is not proof of a live connection"* and
*"manual copying presented as automation"* does not satisfy the assignment. So we should automate the download if we can,
and be transparent about any manual step.

**Current process (manual download, automated processing):**
1. Open the Data Services page → click the CSV → scroll → Accept → file lands in Downloads.
2. `python sam_pipeline.py --from-downloads` picks the newest `ContractOpportunitiesFullCSV*.csv` automatically.

**Full automation (`--download`):**
- Attempt 1 (Sept 29): copied page link in `SAM_CSV_URL` → HTTP 200 but an **HTML page**, not the file. Logged as FAILED.
- The script now tries, in order: `SAM_CSV_URL` (if set), SAM.gov's public file-extract download endpoint
  (`sam.gov/api/prod/fileextractservices/v1/api/download/Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv?privacy=Public`),
  then the public S3 bucket that endpoint serves from (`s3.amazonaws.com/falextracts/...`). A download only counts if the
  file's first line is the CSV header. Every attempt and the final URL are recorded in the run log.
- Record links: the CSV's `Link` column points to a `/workspace/` page. We build the public link `https://sam.gov/opp/<NoticeId>/view` instead.

**Snapshots and versions:** every file becomes e.g. `SAM_active_2026-09-29_v1`. A second, different download
on the same day becomes `_v2`. An identical re-download is recognized by fingerprint and not stored again.
Every output row carries `snapshot_id`, `snapshot_date` and `snapshot_version`. `changes_vs_previous.csv` lists
**new / changed (which fields) / no longer in file / became award notice**.

**Demo day (laptop may not be allowed):**
- Freeze the Thursday run: outputs, raw pages, logs and database go in the submission.
- **Record a screen video** of a full live run on Thursday (both sources + the failed-source test). This is the backup if no laptop is allowed.
- Ask Jonah ahead of time whether you can run it live on your laptop or a provided machine.
- A Friday live run is shown as a **separately dated run**, never replacing Thursday's results.

---

## 7. Traps found in the brief and data

- **Example row 1 (SPE7M926T0038) disagrees with the official record.** The old sheet says qty **4**, posted **01 Sep 2026**.
  DIBBS/PDF say qty **3** (+ a separate 1-unit first-article line) and issued **02 Sep 2026**. The old $70,302 estimate is
  consistent with 4 × the ~$17,576 management price, so it appears to have counted the FAT unit.
- **5th example link goes to cleat.ai**, a third-party site, not DIBBS. Always trace to official sources.
- **All example deadlines had passed** by Sept 28. The old sheet name "Live Bid Candidates" means nothing.
- **Fiscal-year characters:** "26" in SPE7M9**26**T0038 = fiscal year 2026 (started Oct 1, 2025). Never use it as the calendar year.
- **"LIGHT, BACKUP"** (example row 3) does **not** match any required keyword exactly. The brief says to reassess old tags.
  Decide whether to add a documented synonym (e.g., under C06 vehicle lighting) or leave it as unmatched.
- **SAM.gov CSV has no AMSC.** SAM.gov-only items without an NSN can't be confirmed G.
- **Boilerplate everywhere** (packaging "MARKING", "panel of palletized load", generic first-article / export / late-quote text).

---

## 8. Worked example: SPE7M926T0038 (NSN 6110-01-273-9759)

| Field | Value | Source |
|---|---|---|
| Item | PANEL, POWER DISTRIBUTION | PDF p.7 |
| Issued / deadline | 2026-09-02 / 2026-09-14 | PDF p.1 blocks 2 and 10; package page |
| Lines | CLIN 0001: 3 EA (real part). CLIN 0002: 1 EA FAT line (not valued). | PDF p.13–14 |
| DIBBS status | **Removed**: "search by PR number for replacement" | Package page |
| Replacement search | PR 7009053353: 0 hits in Text Search | Text Search, checked 2026-09-29 |
| AMSC | **Not confirmed** (not in PDF; removed item not in Text Search index). "FULL AND OPEN COMPETITION APPLY" on p.8 is supporting evidence only. | → review queue |
| Price history | Last buy 2019-08-28, 3 EA @ $12,553.00 (CAGE 67291, SPE7M119P7278P00003). 5 buys 2014–2019. | PDF p.6 |
| Screening value | 3 × $12,553 = **$37,659** (history 7 years old → compare with DLA management price) | Calculated |
| Gates | Total small business set-aside, first article testing, CMMC Level 2, export-controlled tech data (JCP), critical application item | PDF p.2, 3, 7, 8 |
| Status group | **Status unresolved** (removed, replacement not found, AMSC unconfirmed) | – |

---

## 9. Current coverage and known limits

- SAM.gov: active-notice CSV only so far. Archived 2026 file still to add. API blocked.
- DIBBS: Text Search route built and tested offline against a simulated page. **The first live run is still to do.**
  Selectors may need small fixes after that first run.
- Text Search: removed solicitations are not indexed. Earliest reachable date unknown. Boilerplate-heavy root words
  (MARKING, PLATE, BOX, LIGHT…) return huge result sets. Page caps are logged per term as incomplete coverage.
- AMSC for SAM.gov-only items: not yet looked up.
- DLA management price: source not yet investigated (PUB LOG).
- Award checks: SAM.gov award notices collected. DIBBS awards search not yet automated.

---

## 10. Roadmap to the finish

| Step | What | How |
|---|---|---|
| 1 | SAM.gov archived 2026 file | Download from Archived Data, run `sam_pipeline.py --file <active> --file <archived>` |
| 2 | First live DIBBS run | `dibbs_collector.py --terms PANEL --max-pages 2`, fix selectors if needed, then `--preset roots --pdfs` |
| 3 | Parse PDFs | `dibbs_pdf_parser.py` → lines, history prices, gates |
| 4 | AMSC for SAM.gov items | Collect their NSNs → run the DIBBS collector with those NSNs as `--terms` |
| 5 | Status checks | DIBBS package-page Status (Open/Removed), DIBBS awards search by solicitation number, SAM.gov award notices by `Sol#` |
| 6 | Merge + dedupe | Join DIBBS and SAM.gov on normalized solicitation number (cross-postings), one row per solicitation line, link amendments |
| 7 | Classify status | Open / late route verified / follow-up needed / unresolved / awarded-cancelled-excluded |
| 8 | Value | Line qty × selected price. Unpriced stays blank. Record price basis + source. |
| 9 | Workbook | Management summary first (brief's layout), G-only list sorted by value, then tabs: review queue, non-G, exclusions, carry-forward, date unresolved, changes, run log |
| 10 | Proof | 20–30 manual checks, independent completeness check (e.g., DIBBS "RFQs by Issue Date" pages for sample days), rerun with no duplicates, synthetic amendment/partial-award test (labelled synthetic), failed-source test |
| 11 | Deliverables | README/setup, run evidence, coverage statement, short deck, screen recording. Submit Thursday night. |

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **NSN** | National Stock Number, e.g. 6110-01-273-9759. First 4 digits = FSC (item class), last 9 = NIIN. |
| **AMSC** | Acquisition Method Suffix Code. **G** = government owns full drawings, so it's open to any qualified maker. |
| **DIBBS** | DLA Internet Bid Board System. |
| **SAM.gov** | System for Award Management, the federal contract opportunities site. |
| **RFQ** | Request for Quotation, the government asking suppliers for prices. |
| **CLIN** | Contract Line Item Number, one line in the solicitation. |
| **PR** | Purchase Request number. The same PR can move to a replacement solicitation. |
| **FAT** | First Article Test: a sample unit tested before production. Not a product line. |
| **CAGE** | 5-character company ID code. |
| **NAICS** | Industry classification code. |
| **Set-aside** | Solicitation restricted to certain businesses (e.g., small business). |
| **CMMC** | Cybersecurity Maturity Model Certification (DoD cybersecurity requirement). |
| **JCP** | Joint Certification Program, needed to access export-controlled drawings. |