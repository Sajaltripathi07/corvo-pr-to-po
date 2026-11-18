"""Policy ingestion: read an uploaded policy/amendment document (PDF, DOCX, text, JSON) or a chat
instruction and turn it into structured edit operations on the active policy.

Two readers share one output format (a list of patch operations, see engine.policy.apply_patch):
  * LLM reader (when ANTHROPIC_API_KEY is set): understands arbitrary clauses, new rules, re-ordering.
  * Heuristic reader (always available): limits, windows, effective date, version.
Whatever it cannot interpret is returned in `unclear` so a Procurement user can finish it in the
rule editor. Nothing is activated until a human approves the previewed change (see main.py).
"""
from __future__ import annotations
import io, json, re
from datetime import date, datetime
from . import llm
from .engine import policy as P

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def extract_text(name: str, data: bytes):
    n = name.lower()
    if n.endswith(".json"):
        obj = json.loads(data.decode("utf-8-sig"))
        return "json", json.dumps(obj, indent=1)[:20000], obj
    if n.endswith(".pdf"):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return "pdf", "\n".join((p.extract_text() or "") for p in r.pages), None
    if n.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "docx", "\n".join(parts), None
    return "text", data.decode("utf-8-sig", errors="replace"), None


# ---------------- dates / amounts -----------------------------------------------------------
def find_dates(text: str):
    out = []
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", text):
        out.append((m.start(), date(int(m[1]), int(m[2]), int(m[3]))))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]+([A-Za-z]{3,9})[a-z]*\.?,?[\s\-/.]+(\d{4})\b", text):
        mo = MONTHS.get(m[2][:3].lower())
        if mo:
            try: out.append((m.start(), date(int(m[3]), mo, int(m[1]))))
            except ValueError: pass
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        mo = MONTHS.get(m[1][:3].lower())
        if mo:
            try: out.append((m.start(), date(int(m[3]), mo, int(m[2]))))
            except ValueError: pass
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text):
        try: out.append((m.start(), date(int(m[3]), int(m[2]), int(m[1]))))
        except ValueError: pass
    return sorted(out)


def find_effective(text: str):
    for m in re.finditer(r"(effective|w\.e\.f\.?|with effect from|applicable from|comes? into (?:force|effect))", text, re.I):
        seg = text[m.start(): m.start() + 80]
        ds = find_dates(seg)
        if ds:
            return ds[0][1]
    ds = find_dates(text)
    return ds[0][1] if ds else None


def amounts(s: str) -> list[float]:
    vals = []
    for m in re.finditer(r"(?:₹|rs\.?|inr)?\s*([\d][\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|k)?", s, re.I):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        has_cur = bool(re.match(r"\s*(₹|rs\.?|inr)", m.group(0), re.I))
        v = float(raw.replace(",", ""))
        if unit.startswith(("lakh", "lac")): v *= 1e5
        elif unit.startswith("cr"): v *= 1e7
        elif unit == "k": v *= 1e3
        if has_cur or unit or v >= 10000:
            vals.append(v)
    return vals


def next_version(active: str, taken: set) -> str:
    try:
        major, minor = active.split(".")[:2]
        v = f"{major}.{int(minor) + 1}"
    except Exception:
        v = active + ".1"
    while v in taken:
        major, minor = v.split(".")[:2]
        v = f"{major}.{int(minor) + 1}"
    return v


# ---------------- heuristic reader ------------------------------------------------------------
def heuristic(text: str, active: dict):
    ops, unclear, notes = [], [], []
    rules = active["rules"]
    sentences = [s.strip() for s in re.split(r"(?<=[.;:])\s+|\n+", text) if len(s.strip()) > 8]
    for s in sentences:
        low = s.lower()
        handled = False
        # a clause scoped to a plant / category / item type is a NEW rule, not a change to a global limit
        scoped = bool(re.search(r"\b(for|at|in)\s+(plant|category|department)\b|\bonly\s+(for|at|in|to)\b|\b(CHK1|PUN1|CHK|PUN)\b|\bcategory\b|\bitems?\b.*\b(always|never)\b|\b(always|never)\b", s, re.I))
        if scoped:
            if amounts(s) or re.search(r"\d+\s*(days?|hours?)", low):
                unclear.append(s)
            continue
        # ---- limits on rules ---------------------------------------------------------
        target = None
        for cm in re.finditer(r"\b(\d\.\d+[A-Za-z]?)\b", s):
            for r in rules:
                if cm.group(1) in (r["id"], r.get("clause")):
                    target = r
        if target is None:
            for r in rules:
                nm = (r.get("name") or "").lower()
                route = r["then"]["route"].lower().replace("_po", "").replace("_", " ")
                keys = {nm, route}
                if any(k and re.search(r"\b" + re.escape(k) + r"\b", low) for k in keys):
                    target = r
                    break
        am = amounts(s)
        if target and am and re.search(r"\b(limit|threshold|ceiling|cap|below|up to|upto|not exceed|raise|increase|reduce|lower|revise|change|set|amend)", low):
            leaf_fact = "value_inr"
            if _has_leaf(target.get("when"), leaf_fact):
                m_to = re.search(r"\bto\s+(?:₹|rs\.?|inr)?\s*([\d][\d,]*(?:\.\d+)?\s*(?:lakhs?|lacs?|crores?)?)", s, re.I)
                new = amounts(m_to.group(1))[0] if m_to and amounts(m_to.group(1)) else am[-1]
                ops.append({"op": "set_limit", "rule_id": target["id"], "fact": leaf_fact, "value": new})
                notes.append(f"Rule {target['id']} ({target.get('name')}): limit set to ₹{new:,.0f} from “{s[:110]}”")
                handled = True
        # ---- windows ------------------------------------------------------------------
        mm = re.search(r"(\d+)\s*(?:calendar\s+)?days?", low)
        mh = re.search(r"(\d+)\s*hours?", low)
        if not handled and mm and re.search(r"club|created within|same requirement|splitting", low) and not re.search(r"need[- ]?by", low):
            ops.append({"op": "set", "path": "clubbing.created_within_days", "value": int(mm.group(1))}); handled = True
        if not handled and mm and re.search(r"need[- ]?by", low):
            ops.append({"op": "set", "path": "clubbing.need_by_within_days", "value": int(mm.group(1))}); handled = True
        if not handled and mh and re.search(r"consolidat|same po|combined|one po", low):
            ops.append({"op": "set", "path": "consolidation.window_hours", "value": int(mh.group(1))}); handled = True
        if not handled and mm and re.search(r"(preferred|history).*(days)|548", low):
            ops.append({"op": "set", "path": "preferred_list.po_window_days", "value": int(mm.group(1))}); handled = True
        if not handled and mm and re.search(r"bought|purchased|last .*days|recent", low) and re.search(r"365|year|active supplier", low):
            ops.append({"op": "set", "path": "params.recent_purchase_days", "value": int(mm.group(1))}); handled = True
        if not handled and re.search(r"\b(raise|increase|reduce|lower|change|revise|replace|new rule|add|plant|only for|category|never|always)\b", low) and (am or mm or mh):
            unclear.append(s)
    # de-duplicate ops
    seen, uniq = set(), []
    for o in ops:
        k = json.dumps(o, sort_keys=True)
        if k not in seen:
            seen.add(k); uniq.append(o)
    return uniq, unclear, notes


def _has_leaf(when, fact):
    if not when: return False
    for k in ("all", "any"):
        if k in when: return any(_has_leaf(c, fact) for c in when[k])
    if "not" in when: return _has_leaf(when["not"], fact)
    return when.get("fact") == fact


# ---------------- LLM reader --------------------------------------------------------------------
LLM_SYSTEM = f"""You maintain a procurement policy stored as JSON. You are given the ACTIVE policy JSON and a new
amendment/instruction text. Produce the minimal list of patch operations that makes the policy match the text.

Facts usable in rule conditions: {json.dumps({k: v[0] for k, v in P.FACTS.items()})}
Operators: ==, !=, <, <=, >, >=, in, not_in. Condition tree: {{"all":[...]}}, {{"any":[...]}}, {{"not":{{...}}}} or a leaf
{{"fact":..,"op":..,"value":..}}. Values are INR numbers (1 lakh = 100000, 1 crore = 10000000). Plants are codes like PUN1, CHK1.
Rules run in array order; the first whose conditions match decides. A rule's outcome:
{{"decision":"PO"|"SOURCING_REQUEST","route":"<ROUTE_NAME>","supplier":{{"mode":"contract|preferred|suggest|none|fixed","rank":1,"count":3,"supplier_id":"..."}},"note":"optional"}}
PO outcomes must use supplier mode contract, preferred or fixed. Sourcing outcomes use suggest or none.

Patch operations (JSON objects):
 {{"op":"set","path":"<dotted parameter path>","value":...}}   allowed paths: {sorted(P.PARAM_PATHS)}
 {{"op":"set_limit","rule_id":"5.4","fact":"value_inr","value":300000}}   changes the number of an existing condition
 {{"op":"update_rule","id":"5.3","set":{{"when":{{...}},"then":{{...}},"name":"..."}}}}
 {{"op":"add_rule","rule":{{"id":"5.3A","clause":"5.3A","name":"...","when":{{...}},"then":{{...}}}},"before":"5.4"}}  (or "after":"5.2" / "position":"end")
 {{"op":"remove_rule","id":"5.6"}}   {{"op":"move_rule","id":"5.3","before":"5.2"}}
Preserve existing rule ids; use the clause numbers of the amendment for new rules. Do not change anything the text does not mention.
Return: {{"version":"<new version e.g. 5.1>","effective_from":"YYYY-MM-DD","summary":"one sentence",
"ops":[...],"unclear":["clauses you could not express"]}}"""


def llm_propose(text: str, active: dict, existing_versions: set, feedback: str = ""):
    user = f"ACTIVE POLICY JSON:\n{json.dumps({k: v for k, v in active.items() if not k.startswith('_')})}\n\nAMENDMENT / INSTRUCTION TEXT:\n{text[:15000]}\n{feedback}"
    return llm.complete_json(LLM_SYSTEM, user, max_tokens=4000)


# ---------------- public entry --------------------------------------------------------------------
def propose(active: dict, existing_versions: set, text: str = "", doc_json=None, today: date | None = None) -> dict:
    """Return {new_doc, ops, method, notes, unclear, errors}. Never activates anything."""
    today = today or date.today()
    res = {"ops": [], "unclear": [], "notes": [], "method": "", "errors": []}
    version, eff = None, None
    if isinstance(doc_json, dict) and "rules" in doc_json:           # a complete policy document
        new_doc = doc_json
        new_doc.setdefault("version", next_version(active["version"], existing_versions))
        res.update(method="json-document", new_doc=new_doc)
        res["errors"] = P.validate(new_doc)
        return res
    if isinstance(doc_json, dict) and "ops" in doc_json:
        res["ops"], res["method"] = doc_json["ops"], "json-patch"
        version, eff = doc_json.get("version"), doc_json.get("effective_from")
    else:
        used_llm = False
        if llm.enabled():
            fb = ""
            for attempt in range(2):
                try:
                    j = llm_propose(text, active, existing_versions, fb)
                    P.apply_patch(active, j.get("ops", []))   # dry-run validation
                    res["ops"], res["unclear"] = j.get("ops", []), j.get("unclear", [])
                    res["notes"].append(j.get("summary", ""))
                    version, eff, used_llm = j.get("version"), j.get("effective_from"), True
                    res["method"] = "llm"
                    break
                except Exception as e:  # retry once with the error, then fall back
                    fb = f"\nYour previous answer was rejected: {e}. Fix it."
                    res["errors"].append(f"LLM attempt {attempt + 1}: {e}")
        if not used_llm:
            ops, unclear, notes = heuristic(text, active)
            res.update(ops=ops, unclear=unclear, notes=notes, method="heuristic")
            if not llm.enabled():
                res["notes"].append("No LLM key configured: used the built-in clause reader (limits, windows, dates). "
                                    "Anything it could not read is listed under 'Needs a human'; add new rules in the rule editor.")
    if not eff:
        d = find_effective(text) if text else None
        eff = d.isoformat() if d else today.isoformat()
    if not version:
        m = re.search(r"\bversion\s*(?:no\.?|number)?\s*[:\-]?\s*(\d+(?:\.\d+)+)", text, re.I)
        version = m.group(1) if m and m.group(1) != active["version"] else next_version(active["version"], existing_versions)
    try:
        new_doc = P.apply_patch(active, res["ops"])
        new_doc = {k: v for k, v in new_doc.items() if not k.startswith("_")}
        new_doc["version"], new_doc["effective_from"] = version, str(eff)[:10]
        res["new_doc"] = new_doc
        res["errors"] += P.validate(new_doc)
    except Exception as e:
        res["errors"].append(str(e))
        res["new_doc"] = {**{k: v for k, v in active.items() if not k.startswith("_")}, "version": version, "effective_from": str(eff)[:10]}
    return res
