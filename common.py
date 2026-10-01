"""
Shared rules for Aeropanel Case Study B.
Keywords, normalization, matching, exclusions, run logging.
Used by both sam_pipeline.py and dibbs_collector.py so both sources
are judged by exactly the same rules.
"""
import re, json
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
RUN_LOG = DATA / "run_log.jsonl"

# Reporting window. CUTOFF is PROVISIONAL until Jonah confirms the common cutoff.
START_2026 = "2026-01-01"
CUTOFF = "2026-10-01 12:00"

# ---- Keywords: copied from the brief's Case B reference page ----------------
CATEGORIES = {
    "C01": ["PANEL", "PANEL ASSEMBLY", "CONTROL PANEL", "PANEL CONTROL", "COCKPIT PANEL",
            "INSTRUMENT PANEL", "OPERATOR INTERFACE PANEL", "CONTROL HEAD", "DISPLAY PANEL",
            "ANNUNCIATOR PANEL", "SWITCH PANEL", "HMI PANEL"],
    "C02": ["LIGHTPLATE", "LIGHT PLATE", "PANEL INDICATING LIGHT TRANSMITTING", "ILLUMINATED PANEL",
            "EDGE LIT PANEL", "NVIS PANEL", "HIDDEN UNTIL LIT", "ELECTROLUMINESCENT PANEL",
            "ACRYLIC PANEL", "LUCITE PANEL", "MIL DTL 7788"],
    "C03": ["BEZEL", "BEZEL INSTRUMENT MOUNTING", "DISPLAY BEZEL", "FACEPLATE", "FACE PLATE",
            "ESCUTCHEON", "PANEL OVERLAY", "GRAPHIC OVERLAY", "MEMBRANE OVERLAY", "DIAL SCALE"],
    "C04": ["LENS", "LENS LIGHT", "WINDOW", "DIAL WINDOW", "NVIS FILTER", "COVER ELECTRICAL SWITCH",
            "LAMP CAP", "PANEL HOUSING", "INSTRUMENT HOUSING", "PANEL FRAME", "PANEL MOUNTING PLATE"],
    "C05": ["INDICATOR LIGHT", "LIGHT INDICATOR", "LIGHT INSTRUMENT", "PANEL LIGHT", "ANNUNCIATOR LIGHT",
            "WARNING LIGHT", "LIGHTING ASSEMBLY METER", "CIRCUIT BOARD LAMP ASSEMBLY", "LAMP ASSEMBLY",
            "COCKPIT LIGHT"],
    "C06": ["FORMATION LIGHT", "NAVIGATION LIGHT", "LIGHT NAVIGATIONAL AIRCRAFT", "POSITION LIGHT",
            "ANTI COLLISION LIGHT", "EXTERIOR LIGHT", "BLACKOUT LIGHT", "VEHICLE LIGHT", "WINGTIP LIGHT",
            "DROGUE LIGHT"],
    "C07": ["SWITCH", "PUSHBUTTON", "PUSH BUTTON", "SWITCH PUSH LIGHT", "SWITCH PUSH PULL",
            "TOGGLE SWITCH", "ROTARY SWITCH", "SWITCH CAP", "KEYBOARD", "KEYPAD", "ACTUATOR",
            "SWITCH GUARD", "DIMMER CONTROL"],
    "C08": ["PLATE", "IDENTIFICATION PLATE", "INSTRUCTION PLATE", "NAMEPLATE", "LEGEND", "MARKING",
            "ILLUMINATED SIGN", "MOUNTING PLATE", "BACKING PLATE", "PANEL COMPONENT"],
    "C09": ["TACHOMETER", "TORQUEMETER", "FUEL QUANTITY INDICATOR", "FUEL GAUGE", "FUEL GAGE",
            "PRESSURE INDICATOR", "TEMPERATURE INDICATOR", "PRESSURE TRANSDUCER", "THERMOCOUPLE"],
    "C10": ["WIRING HARNESS", "WIRING HARNESS BRANCHED", "HARNESS ASSEMBLY", "CABLE ASSEMBLY",
            "ELECTRICAL CABLE ASSEMBLY", "CONTROL BOX", "SWITCH BOX", "DISTRIBUTION BOX",
            "INTERCONNECTING BOX", "TERMINAL BOX"],
}
CATEGORY_NAMES = {
    "C01": "Integrated Control / Indicator / HMI Panels", "C02": "Illuminated Panels / Lightplates",
    "C03": "Bezels / Faceplates / Escutcheons / Overlays", "C04": "Lenses / Windows / Covers / Housings",
    "C05": "Indicator / Annunciator / Panel Lighting", "C06": "Formation / Navigation / Exterior / Vehicle Lighting",
    "C07": "Switches / Pushbuttons / Keyboards / Controls", "C08": "Plates / Markings / Mounting / Misc",
    "C09": "Aircraft Instruments / Gauges / Sensors (INSCO)", "C10": "Harnesses / Cable Assemblies / Box Builds",
}
GROUP = {"C08": "core-adjacent", "C09": "INSCO", "C10": "adjacent"}      # others = core
BROAD = {"PANEL", "SWITCH", "PLATE", "WINDOW", "LENS", "ACTUATOR", "MARKING", "LEGEND"}
# Federal Supply Classes whose "panels" are structural/shelter/body parts, not control/HMI panels.
STRUCTURAL_FSC = {
    "1560": "Airframe structural components",
    "2510": "Vehicular cab, body and frame structural components",
    "5410": "Prefabricated and portable buildings",
    "5411": "Rigid wall shelters",
    "5419": "Collective modular support system",
    "5670": "Building components, prefabricated",
    "5680": "Miscellaneous construction materials",
}
STRUCTURAL = ["STRUCTURAL", "SKIN", "AIRFRAME", "FUSELAGE", "WING", "BODY", "ARMOR", "FLOOR", "SHELTER"]
NSN_RE = re.compile(r"(?<!\d)(\d{4})[- ]?(\d{2})[- ]?(\d{3})[- ]?(\d{4})(?!\d)")


def normalize(text) -> str:
    """Uppercase, punctuation -> space, ASSY -> ASSEMBLY. Padded with spaces."""
    t = str(text or "").upper()
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    t = re.sub(r"\bASSY\b", "ASSEMBLY", t)
    return f" {t.strip()} "


def kw_pattern(kw: str):
    """Whole-word match, optional plural S/ES on the last word."""
    return re.compile(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?:S|ES)?(?![A-Z0-9])")


_PATTERNS = [(cat, kw, kw_pattern(kw)) for cat, kws in CATEGORIES.items() for kw in kws]


def match(text_norm: str):
    """Return best (most specific = longest) category match, or None."""
    hits = [(cat, kw) for cat, kw, pat in _PATTERNS if pat.search(text_norm)]
    if not hits:
        return None
    cat, kw = max(hits, key=lambda h: len(h[1]))
    return {"category": cat, "category_name": CATEGORY_NAMES[cat], "primary_keyword": kw,
            "keyword_tags": "; ".join(sorted({k for _, k in hits})),
            "broad_only": all(k in BROAD for _, k in hits),
            "group": GROUP.get(cat, "core")}


def exclusion(title_norm: str, m, fsc: str = "") -> str | None:
    """Brief's exclusion rules, applied to the item description/title.
    fsc = first 4 digits of the NSN (DIBBS) or the PSC/ClassificationCode (SAM.gov)."""
    if kw_pattern("SKIRT").search(title_norm):
        return "Excluded: SKIRT"
    if kw_pattern("COVER").search(title_norm) and not kw_pattern("SWITCH").search(title_norm):
        return "Excluded: COVER without SWITCH"
    if "PANEL" in m["keyword_tags"] and any(kw_pattern(w).search(title_norm) for w in STRUCTURAL):
        return "Excluded: structural/airframe panel"
    fsc = str(fsc or "").strip()[:4]
    if "PANEL" in m["keyword_tags"] and fsc in STRUCTURAL_FSC:
        return f"Excluded: structural/shelter/body panel (FSC {fsc} = {STRUCTURAL_FSC[fsc]})"
    return None


def norm_nsn(text) -> str:
    """Any NSN format -> 1234-56-789-0123 (or '' if none)."""
    m = NSN_RE.search(str(text or ""))
    return "-".join(m.groups()) if m else ""


def norm_sol(sol) -> str:
    """Solicitation number without dashes/spaces, uppercase - used to join DIBBS and SAM."""
    return re.sub(r"[^A-Z0-9]", "", str(sol or "").upper())


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log_run(entry: dict):
    """Append one line per run to data/run_log.jsonl (success AND failure)."""
    DATA.mkdir(exist_ok=True)
    entry = {"logged_utc": datetime.now(timezone.utc).isoformat(), **entry}
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def strip_fsc_prefix(title) -> str:
    """DLA notices on SAM.gov are titled like '59--SWITCH,PUSH' (FSC group prefix). Drop the prefix."""
    return re.sub(r"^\s*\d{2}\s*-{1,2}\s*", "", str(title or ""))


def dla_context_check(item_name: str, m, fsc: str = "") -> str | None:
    """Same rules for DIBBS nomenclature and DLA-style SAM.gov titles:
    broad keyword must be the item noun (text before the first comma); then the brief's exclusions."""
    noun = normalize(str(item_name).split(",")[0])
    hits = [k for k in m["keyword_tags"].split("; ") if k]
    if m["broad_only"] and "," in str(item_name) and not any(kw_pattern(k).search(noun) for k in hits):
        return f"Broad keyword only in modifier, item noun is '{str(item_name).split(',')[0].strip()}'"
    return exclusion(normalize(item_name), m, fsc)