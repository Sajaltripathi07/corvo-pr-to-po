"""Chat intake: free text -> a complete, confirmed PR line. Deterministic slot filling (works offline);
if an LLM key exists it is used first to read the message, with the deterministic parser as fallback.
The assistant never invents an item: unknown items are only raised as NEW after the requester confirms."""
from __future__ import annotations
import json, re
from datetime import date, timedelta
from . import db, llm, service
from .engine.matching import ItemMatcher

UNITS = r"nos|no|pcs|pc|pieces|piece|units|unit|kg|kgs|ltr|ltrs|litres|liters|litre|mtr|mtrs|meters|metres|pair|pairs|set|sets|roll|rolls|bkt|brl|job|jobs|ream|reams|cyl|mon|qtr"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _today():
    return date.today()


def parse_need_by(text: str, today: date | None = None):
    today = today or _today()
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])), m.group(0)
    if re.search(r"\b(today)\b", t): return today, "today"
    if re.search(r"\btomorrow\b", t): return today + timedelta(days=1), "tomorrow"
    m = re.search(r"\bin\s+(\d+)\s*(day|week)s?\b", t)
    if m: return today + timedelta(days=int(m[1]) * (7 if m[2] == "week" else 1)), m.group(0)
    m = re.search(r"\b(next|this)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        wd = WEEKDAYS.index(m[2])
        d = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        if m[1] == "next" and (d - today).days <= 7 and wd > today.weekday():
            d += timedelta(days=7)
        return d, m.group(0).strip()
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+(\d{4}))?\b", t)
    if m:
        y = int(m[3]) if m[3] else today.year
        d = date(y, MONTHS.index(m[2]) + 1, int(m[1]))
        if not m[3] and d < today: d = date(y + 1, d.month, d.day)
        return d, m.group(0)
    m = re.search(r"\b(?:end of (?:the )?month|month[- ]end)\b", t)
    if m:
        n = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return n - timedelta(days=1), m.group(0)
    return None, None


def _plant_aliases():
    al = {}
    for p in db.q("SELECT * FROM plants"):
        al[p["plant"].lower()] = p["plant"]
        for w in re.findall(r"[A-Za-z]{4,}", p["name"]):
            if w.lower() not in ("plant",):
                al[w.lower()] = p["plant"]
    return al


def extract(text: str, sess_slots: dict, awaiting: str | None) -> dict:
    """Deterministic reader. Returns only slots found in this message."""
    out, rest = {}, " " + text + " "
    m = re.search(r"\bITM-\d{4}\b", text, re.I)
    if m:
        out["item_code"] = m.group(0).upper(); rest = rest.replace(m.group(0), " ")
    for alias, code in sorted(_plant_aliases().items(), key=lambda kv: -len(kv[0])):
        mm = re.search(r"(?:\bfor\s+|\bat\s+|\bin\s+|\bto\s+)?\b" + re.escape(alias) + r"\b(?:\s+plant)?", rest, re.I)
        if mm:
            out["plant"] = code; rest = rest.replace(mm.group(0), " ", 1); break
    d, frag = parse_need_by(rest)
    if d:
        out["need_by"] = d.isoformat()
        rest = re.sub(r"\b(by|before|on|until|within)?\s*" + re.escape(frag), " ", rest, count=1, flags=re.I)
    if re.search(r"\b(emergency|breakdown|machine (is )?down|line (is )?stopped|production stopped)\b", text, re.I):
        out["urgency"] = "Emergency"
    elif re.search(r"\b(normal|not urgent|routine|regular)\b", text, re.I):
        out["urgency"] = "Normal"
    elif re.search(r"\b(urgent|asap|immediately|rush)\b", text, re.I):
        out["urgent_hint"] = True
    m = re.search(r"(?:\b(?:need|want|require|order|buy|get|qty|quantity|raise(?: a)? pr for|pr for|purchase)\b[:\s]+)(\d[\d,]*(?:\.\d+)?)(?!\d)\s*(" + UNITS + r")?\b", rest, re.I) \
        or re.match(r"\s*(\d[\d,]*(?:\.\d+)?)\s*(" + UNITS + r")?\b(?!\s*(?:mm|inch|kw|hp|ton|a\b))", rest, re.I)
    if m:
        out["quantity"] = float(m.group(1).replace(",", ""))
        if m.group(2): out["uom_hint"] = m.group(2).lower()
        rest = rest.replace(m.group(0), " ", 1)
    elif awaiting == "quantity":
        mm = re.search(r"\d[\d,]*(?:\.\d+)?", text)
        if mm: out["quantity"] = float(mm.group(0).replace(",", ""))
    if awaiting == "est_price":
        mm = re.search(r"\d[\d,]*(?:\.\d+)?", text)
        if mm: out["est_price"] = float(mm.group(0).replace(",", ""))
        return out
    if awaiting in ("quantity", "plant", "need_by", "urgency"):
        return out
    rest = re.sub(r"\b(i|we)?\s*(need|want|require|order|buy|get|please|pls|raise|a|an|the|pr|purchase request|request for|of|some|urgent|asap|for|by|to)\b", " ", rest, flags=re.I)
    rest = re.sub(r"\b(" + UNITS + r")\b(?=\s)", " ", rest, count=1, flags=re.I) if "quantity" in out else rest
    item_text = re.sub(r"[\s,.;:]+", " ", rest).strip(" -")
    if item_text and not out.get("item_code") and len(item_text) > 2:
        out["item_text"] = item_text
    return out


def llm_extract(text, slots, awaiting):
    plants = [p["plant"] for p in db.q("SELECT plant FROM plants")]
    j = llm.complete_json(
        "Read a purchase-request chat message from an employee. Return keys (omit what is not stated): item_text (the thing wanted, "
        "in the user's words, no quantity/plant/date), item_code (ITM-####), quantity (number), plant (one of %s; Chakan=CHK1, Bhosari/Pune=PUN1), "
        "need_by (YYYY-MM-DD; today is %s), urgency (Emergency only if stated as emergency/breakdown; else Normal if stated), "
        "est_price (number), intent (confirm|cancel|new_item|none). Never invent values." % (plants, _today().isoformat()),
        json.dumps({"message": text, "known": slots, "awaiting": awaiting}), max_tokens=400)
    return {k: v for k, v in j.items() if v not in (None, "")}


def _load(sid):
    r = db.q1("SELECT state FROM chat_sessions WHERE id=?", (sid,))
    return json.loads(r["state"]) if r else {"slots": {}, "stage": "collect", "awaiting": None, "messages": [], "cands": []}


def _save(sid, st):
    db.x("INSERT OR REPLACE INTO chat_sessions VALUES(?,?,?)", (sid, json.dumps(st), db.now()))


def history(sid):
    return _load(sid)["messages"]


def _summary(s, ref):
    it = ref.items.get(s.get("item_code"))
    name = f"{it['description']} ({it['item_code']})" if it else f"{s.get('item_text')} (new item, est. ₹{s.get('est_price', 0):,.0f} each)"
    plant = ref and next((p["name"] for p in db.q("SELECT * FROM plants WHERE plant=?", (s["plant"],))), s["plant"])
    return f"**{s['quantity']:g} × {name}** for {plant}, needed by {s['need_by']}, urgency {s['urgency']}"


def reply(sid: str, text: str, user: str = "requester") -> dict:
    ref = db.reference()
    st = _load(sid)
    s = st["slots"]
    text = text.strip()
    st["messages"].append({"role": "user", "text": text})
    low = text.lower()
    out = {"chips": [], "pr": None}

    def say(msg, chips=None, **kw):
        st["messages"].append({"role": "assistant", "text": msg, "chips": chips or []})
        _save(sid, st)
        return {"reply": msg, "chips": chips or [], "stage": st["stage"], "slots": s, **kw}

    if st["stage"] == "done":
        st.update(slots={}, stage="collect", awaiting=None, cands=[]); s = st["slots"]
    if re.fullmatch(r"(cancel|start over|reset|never mind|no thanks)\.?", low):
        st.update(slots={}, stage="collect", awaiting=None, cands=[]); s = st["slots"]
        return say("Okay, cleared. What do you need to buy?")

    # --- confirmation step
    if st["stage"] == "confirm":
        if re.fullmatch(r"(yes|y|confirm|confirmed|ok|okay|correct|go ahead|create( it)?|submit)[.! ]*", low):
            return _create(sid, st, s, ref, user, say)
        if low in ("change something", "change", "no", "edit"):
            st["stage"] = "collect"
            return say("What should I change? You can say e.g. “make it 300”, “Pune plant” or “need by 30 Nov”.")
        st["stage"] = "collect"          # treat as a correction and fall through

    # --- picking among item candidates
    if st["stage"] == "pick_item":
        pick = re.fullmatch(r"\s*(?:option\s*)?([1-3])\s*", low)
        m = re.search(r"ITM-\d{4}", text, re.I)
        if pick and int(pick.group(1)) <= len(st["cands"]):
            s["item_code"] = st["cands"][int(pick.group(1)) - 1]["item_code"]
        elif m and m.group(0).upper() in ref.items:
            s["item_code"] = m.group(0).upper()
        elif re.search(r"\bnew\b|none|not (in|listed)|something else", low):
            s["new_item"] = True
        if s.get("item_code") or s.get("new_item"):
            st["stage"] = "collect"; st["cands"] = []

    # --- read the message
    found = {}
    if llm.enabled():
        try: found = llm_extract(text, s, st["awaiting"])
        except Exception: found = {}
    det = extract(text, s, st["awaiting"])
    for k, v in det.items():
        found.setdefault(k, v)
    if st["awaiting"] == "urgency":
        if re.search(r"emerg|breakdown|down", low): found["urgency"] = "Emergency"
        elif re.search(r"normal|no\b|routine|not", low): found["urgency"] = "Normal"
    if st["awaiting"] == "plant" and "plant" not in found:
        return say("I only know the plants below. Which one is this for?", [p["name"] for p in db.q("SELECT * FROM plants")])
    for k in ("quantity", "plant", "need_by", "urgency", "est_price", "uom_hint"):
        if k in found: s[k] = found[k]
    if found.get("item_code") and found["item_code"] in ref.items:
        s["item_code"] = found["item_code"]; s.pop("item_text", None); s.pop("new_item", None)
    elif found.get("item_text") and not s.get("item_code"):
        s["item_text"] = found["item_text"]; s.pop("new_item", None)
    if found.get("urgent_hint") and "urgency" not in s: s["urgent_hint"] = True
    if st["awaiting"] == "est_price" and "est_price" in s: s["new_item"] = True

    # --- resolve the item
    if not s.get("item_code") and not s.get("new_item"):
        if not s.get("item_text"):
            st["awaiting"] = None
            return say("What would you like to buy? A short description or an item code (ITM-0000) is fine.")
        m = ItemMatcher(ref.items, (db.approved_policies()[-1].get("matching"))).match(s["item_text"])
        if m["status"] == "matched":
            s["item_code"] = m["item_code"]; s["matched_from"] = s["item_text"]
        else:
            cands = m["candidates"][:3] if m["candidates"] and m["candidates"][0]["score"] >= 0.45 else []
            if cands:
                st["stage"], st["cands"], st["awaiting"] = "pick_item", cands, None
                lines = "\n".join(f"{i}. {c['description']} ({c['item_code']})" for i, c in enumerate(cands, 1))
                return say(f"“{s['item_text']}” isn’t an exact match. Is it one of these?\n{lines}\nOr say “new item” if it isn’t any of them.",
                           [str(i) for i in range(1, len(cands) + 1)] + ["new item"])
            s["new_item"] = True
    if s.get("new_item") and "est_price" not in s:
        st["awaiting"] = "est_price"
        desc = s.get("item_text") or "that"
        return say(f"I can’t find “{desc}” in the item master, so I won’t guess. I can raise it as a **new item** for sourcing. "
                   "What is your estimated price per unit (₹)?", ["Not sure"])
    if s.get("new_item") and not s.get("item_text"):
        s["item_text"] = "New item"

    # --- ask for what is missing, one thing at a time
    plants = db.q("SELECT * FROM plants")
    if "quantity" not in s:
        st["awaiting"] = "quantity"; return say("How many do you need?")
    if "plant" not in s:
        st["awaiting"] = "plant"; return say("Which plant is this for?", [p["name"] for p in plants])
    if "need_by" not in s:
        st["awaiting"] = "need_by"; return say("By when do you need it? (e.g. “next Friday” or “30 Nov”)")
    if "urgency" not in s:
        st["awaiting"] = "urgency"
        why = " You mentioned it’s urgent." if s.get("urgent_hint") else ""
        return say(f"Is this a normal request or a genuine emergency (e.g. a breakdown)?{why} Emergency is policy-controlled, so please only use it when it applies.", ["Normal", "Emergency"])
    st["awaiting"], st["stage"] = None, "confirm"
    if s.get("est_price") is None and s.get("new_item"): s["est_price"] = 0
    extra = f"\nI matched “{s['matched_from']}” to {ref.items[s['item_code']]['description']}." if s.get("matched_from") and s.get("item_code") else ""
    return say(f"Please confirm: {_summary(s, ref)}.{extra}", ["Yes, create it", "Change something"])


def _create(sid, st, s, ref, user, say):
    it = ref.items.get(s.get("item_code"))
    pr_no = service.next_pr_no()
    line = {"pr_no": pr_no, "pr_line": 1, "created_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
            "requester": user, "department": "", "plant": s["plant"], "item_code": it["item_code"] if it else "",
            "description": it["description"] if it else s.get("item_text", ""), "quantity": s["quantity"],
            "uom": it["uom"] if it else (s.get("uom_hint") or "NOS").upper(), "estimated_unit_price": s.get("est_price") if not it else "",
            "need_by_date": s["need_by"], "urgency": s["urgency"]}
    rid = service.add_live_line(line)
    _, reqs = service.view_requirements(rid)
    R = next((r for r in reqs if any(l["pr_no"] == pr_no for l in r["lines"])), None)
    db.audit(user, "requester", "pr.created", {"pr_no": pr_no, "chat": True})
    st.update(stage="done", slots={}, awaiting=None, cands=[])
    sl = ""
    if R:
        who = f" to {R['supplier_id']}" if R["decision"] == "PO" else (f" (suggested: {', '.join(R['suggested'])})" if R["suggested"] else "")
        sl = (f"\n\nProcurement view: **{R['route'].replace('_', ' ')}**{who}, requirement value ₹{R['value']:,.0f}"
              f"{' (clubbed with ' + str(len(R['lines']) - 1) + ' other line(s))' if len(R['lines']) > 1 else ''}. A buyer will review it.")
    return say(f"Created **{pr_no}**.{sl}", ["Raise another request"], pr={"pr_no": pr_no, "group_id": R["group_id"] if R else None}, decision=R)
