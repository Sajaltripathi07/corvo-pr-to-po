"""Decision engine: match -> club -> value -> contracts -> rules -> suppliers -> consolidate.

Pure functions over in-memory reference data. All thresholds, windows, rule order and ranking
weights come from the policy documents passed in; nothing business-specific is hard-coded here
(only the invariant "Blocked suppliers never get a PO or a suggestion", policy 5.7).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from collections import defaultdict

from .util import parse_dt, parse_date, num, inr
from .matching import ItemMatcher
from . import policy as P

NEVER_ALLOWED = {"Blocked"}  # policy 5.7 floor; Inactive can only be re-allowed by policy params


# ------------------------------------------------------------------------------------------
class Reference:
    """Master data + contracts + PO history, indexed for the engine."""

    def __init__(self, items, suppliers, plants, po_history, contracts):
        self.items = {i["item_code"]: {**i, "standard_price": num(i.get("standard_price"), 0.0)} for i in items}
        self.suppliers = {}
        for s in suppliers:
            cats = s.get("categories") or []
            if isinstance(cats, str):
                cats = [c.strip() for c in cats.split(";") if c.strip()]
            self.suppliers[s["supplier_id"]] = {**s, "categories": cats,
                                                 "on_time_pct": num(s.get("on_time_pct"), 0.0),
                                                 "quality_rating": num(s.get("quality_rating"), 0.0)}
        self.plants = set(p if isinstance(p, str) else p["plant"] for p in plants)
        self.po_by_item = defaultdict(list)
        for p in po_history:
            d = parse_date(p["po_date"])
            if d is None:
                continue
            self.po_by_item[p["item_code"]].append({
                "po_number": str(p["po_number"]), "date": d, "supplier_id": p["supplier_id"],
                "price": num(p["unit_price"], 0.0), "qty": num(p.get("quantity"), 0.0), "plant": p.get("plant"),
                "on_time": str(p.get("delivered_on_time")).lower() in ("true", "1", "yes")})
        for v in self.po_by_item.values():
            v.sort(key=lambda x: (x["date"], x["po_number"]))
        self.contracts = []
        for c in contracts:
            self.contracts.append({
                "contract_id": c["contract_id"], "supplier_id": c["supplier_id"],
                "start": parse_date(c["start_date"]), "end": parse_date(c["end_date"]),
                "cap": num(c["value_cap"], 0.0), "consumed": num(c.get("consumed_value"), 0.0) or 0.0,
                "lines": {k: num(v) for k, v in (c.get("lines") or {}).items()},
                "source": c.get("source", "register")})

    def supplier_ok(self, sid, allowed) -> bool:
        s = self.suppliers.get(sid)
        return bool(s) and s["status"] in allowed and s["status"] not in NEVER_ALLOWED


@dataclass
class ProcessOptions:
    policy_mode: str = "by_date"          # by_date | latest | version
    forced_version: str | None = None
    extra_consumed: dict = field(default_factory=dict)   # contract_id -> value already consumed (live pool)
    id_mode: str = "seq"                  # seq | stable
    run_label: str = ""


# ------------------------------------------------------------------------------------------
def _allowed(pol_block, pol) -> set:
    st = set((pol_block or {}).get("supplier_statuses_allowed") or pol.get("params", {}).get("supplier_statuses_allowed") or ["Active"])
    return st - NEVER_ALLOWED


def build_preferred_list(ref: Reference, item_code: str, as_of: date, pol: dict) -> list[dict]:
    """Policy section 6. Returns ranked list with every scoring component (for buyer explanation)."""
    cfg = pol["preferred_list"]
    allowed = _allowed(pol.get("params"), pol)
    window = cfg.get("po_window_days", 548)
    lo = as_of - timedelta(days=window)
    by_sup = defaultdict(list)
    for p in ref.po_by_item.get(item_code, []):
        if lo <= p["date"] < as_of:
            by_sup[p["supplier_id"]].append(p)
    cont = defaultdict(list)
    if cfg.get("include_contract_holders", True):
        for c in ref.contracts:
            if item_code in c["lines"] and c["start"] and c["start"] <= as_of:
                cont[c["supplier_id"]].append(c)
    sids = set(by_sup) | set(cont)
    sids = [s for s in sids if ref.supplier_ok(s, allowed)
            and ref.suppliers[s]["on_time_pct"] >= cfg.get("min_on_time_pct", 0)
            and ref.suppliers[s]["quality_rating"] >= cfg.get("min_quality", 0)]
    maxc = max([len(by_sup.get(s, [])) for s in sids] or [0])
    w = cfg["weights"]
    wsum = sum(w.values()) or 1.0
    tiers = sorted(cfg.get("recency_tiers", []), key=lambda t: t["within_days"])
    out = []
    for s in sids:
        sup = ref.suppliers[s]
        pos = by_sup.get(s, [])
        last = max(pos, key=lambda p: (p["date"], p["po_number"])) if pos else None
        rec = cfg.get("recency_default", 0.0)
        days_since = None
        if last:
            days_since = (as_of - last["date"]).days
            for t in tiers:
                if days_since <= t["within_days"]:
                    rec = t["score"]
                    break
        freq = (len(pos) / maxc) if maxc else 0.0
        has_c = 1.0 if cont.get(s) else 0.0
        comp = {"frequency": freq, "recency": rec, "contract": has_c,
                "on_time": sup["on_time_pct"] / 100.0, "quality": sup["quality_rating"] / 5.0}
        score = sum(w.get(k, 0.0) * v for k, v in comp.items()) / wsum
        if last:
            ref_price, ref_src = last["price"], f"PO {last['po_number']} dated {last['date'].isoformat()}"
        else:
            lc = max(cont[s], key=lambda c: c["start"])
            ref_price, ref_src = lc["lines"][item_code], f"contract {lc['contract_id']} rate"
        out.append({"supplier_id": s, "name": sup["name"], "score": round(score, 4), "components": comp,
                    "po_count": len(pos), "last_po_date": last["date"].isoformat() if last else None,
                    "days_since_last_po": days_since, "contract_ids": [c["contract_id"] for c in cont.get(s, [])],
                    "on_time_pct": sup["on_time_pct"], "quality": sup["quality_rating"],
                    "reference_price": ref_price, "reference_source": ref_src})
    out.sort(key=lambda r: (-r["score"], -r["on_time_pct"], -r["quality"], r["supplier_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def suggest_suppliers(ref: Reference, item: dict | None, plist: list[dict], count: int, as_of: date, pol: dict) -> list[dict]:
    """Policy 6.4: preferred list in order, then top up from the item's category by delivery+quality."""
    sel = [{"supplier_id": p["supplier_id"], "source": "preferred", "rank": p["rank"]} for p in plist[:count]]
    if len(sel) < count and item and pol.get("suggestions", {}).get("topup_from_category", True):
        allowed = _allowed(pol.get("params"), pol)
        have = {p["supplier_id"] for p in plist}
        tw = pol.get("suggestions", {}).get("topup_weights", {"on_time": 0.5, "quality": 0.5})
        ts = sum(tw.values()) or 1.0
        pool = []
        for sid, s in ref.suppliers.items():
            if sid in have or not ref.supplier_ok(sid, allowed) or item["category"] not in s["categories"]:
                continue
            sc = (tw.get("on_time", 0) * s["on_time_pct"] / 100 + tw.get("quality", 0) * s["quality_rating"] / 5) / ts
            pool.append((-sc, sid))
        pool.sort()
        for _, sid in pool[: count - len(sel)]:
            sel.append({"supplier_id": sid, "source": "category top-up", "rank": None})
    return sel


# ------------------------------------------------------------------------------------------
class _Selector:
    def __init__(self, policies, opts):
        self.pols = sorted(policies, key=lambda p: (p["effective_from"], p["version"]))
        self.opts = opts
        if not self.pols:
            raise ValueError("no policy available")

    def pick(self, d: date) -> dict:
        o = self.opts
        if o.policy_mode == "version":
            for p in self.pols:
                if p["version"] == o.forced_version:
                    return p
            raise ValueError(f"policy version {o.forced_version} not found")
        if o.policy_mode == "latest" or d is None:
            return self.pols[-1]
        eff = [p for p in self.pols if parse_date(p["effective_from"]) <= d]
        return eff[-1] if eff else self.pols[0]


def _field(ln, f):
    return ln.get(f) if f in ln else None


def process(lines: list[dict], ref: Reference, policies: list[dict], opts: ProcessOptions | None = None) -> dict:
    opts = opts or ProcessOptions()
    sel = _Selector(policies, opts)
    warnings: list[str] = []
    matchers: dict[str, ItemMatcher] = {}

    def matcher_for(pol):
        v = pol["version"]
        if v not in matchers:
            matchers[v] = ItemMatcher(ref.items, pol.get("matching"))
        return matchers[v]

    # ---- 1. normalise + validate + match -------------------------------------------------
    recs, seen = [], set()
    for idx, raw in enumerate(lines):
        r = {"raw": raw, "idx": idx, "pr_no": str(raw.get("pr_no", "")).strip(), "pr_line": str(raw.get("pr_line", "")).strip(),
             "flags": []}
        r["created"] = parse_dt(raw.get("created_at"))
        r["plant"] = str(raw.get("plant") or "").strip()
        r["qty"] = num(raw.get("quantity"))
        r["need_by"] = parse_date(raw.get("need_by_date"))
        r["urgency"] = str(raw.get("urgency") or "Normal").strip()
        r["department"] = raw.get("department")
        r["est"] = num(raw.get("estimated_unit_price"))
        key = (r["pr_no"], r["pr_line"])
        problem = None
        if key in seen:
            problem = "Duplicate PR number/line in the batch; first occurrence was processed"
        elif r["created"] is None:
            problem = "created_at missing or unreadable"
        elif r["plant"] not in ref.plants:
            problem = f"Unknown plant '{r['plant']}' (known: {', '.join(sorted(ref.plants))})"
        elif r["qty"] is None or r["qty"] <= 0:
            problem = f"Invalid quantity ({raw.get('quantity')}): must be greater than zero"
        elif not (str(raw.get("item_code") or "").strip() or str(raw.get("description") or "").strip()):
            problem = "Neither item code nor description given"
        seen.add(key)
        r["problem"] = problem
        recs.append(r)

    for r in recs:
        if r["problem"]:
            continue
        pol0 = sel.pick(r["created"].date())
        code = str(r["raw"].get("item_code") or "").strip()
        desc = str(r["raw"].get("description") or "").strip()
        r["match_note"] = ""
        if code and code in ref.items:
            r["item_code"], r["match_status"] = code, "code"
            r["match_note"] = "item code given"
        else:
            if code:
                r["flags"].append(f"item code {code} is not in the item master; matched by description")
            m = matcher_for(pol0).match(desc) if desc else {"item_code": None, "status": "new", "candidates": [], "note": "no description"}
            r["item_code"] = m["item_code"]
            r["match_status"] = m["status"]
            r["match_note"] = m["note"]
            r["candidates"] = m["candidates"]
            if m["status"] == "ambiguous":
                r["flags"].append("item match is ambiguous: " + m["note"])
        r["is_new"] = r["item_code"] is None
        if r["is_new"] and r["est"] is None:
            r["flags"].append("new item without an estimated unit price: valued at 0, buyer must estimate")
        if "urgent" in str(r["raw"].get("description", "")).lower() and r["urgency"].lower() != "emergency":
            r["flags"].append("description says 'urgent' but urgency field is not Emergency; field followed")

    ok = sorted([r for r in recs if not r["problem"]], key=lambda r: (r["created"], r["pr_no"], _num_key(r["pr_line"])))

    # ---- 2. clubbing (policy 2) -----------------------------------------------------------
    reqs: list[dict] = []
    open_by_key = defaultdict(list)   # (item_code, plant) shortlist, used when clubbing is by item

    def can_join(r, R) -> bool:
        first = R["lines"][0]
        cl = sel.pick(first["created"].date()).get("clubbing", {})
        if not cl.get("enabled", True):
            return False
        if not _same_fields(r, first, cl.get("same_fields", ["item_code", "plant"]), ref):
            return False
        if (r["created"].date() - first["created"].date()).days > cl.get("created_within_days", 14):
            return False
        if r["need_by"] and first["need_by"] and abs((r["need_by"] - first["need_by"]).days) > cl.get("need_by_within_days", 7):
            return False
        return True

    for r in ok:
        target = None
        if not r["is_new"] or not sel.pick(r["created"].date()).get("clubbing", {}).get("never_club_new_items", True):
            pool = open_by_key[(r["item_code"], r["plant"])] if r["item_code"] else []
            for R in pool:
                if can_join(r, R):
                    target = R
                    break
            if target is None:      # policies that club on other fields (e.g. category) need a wider scan
                for R in reqs:
                    if R["is_new"] or R in pool:
                        continue
                    if "item_code" not in sel.pick(R["lines"][0]["created"].date()).get("clubbing", {}).get("same_fields", ["item_code"]) and can_join(r, R):
                        target = R
                        break
        if target is not None:
            target["lines"].append(r)
        else:
            R = {"lines": [r], "is_new": r["is_new"], "item_code": r["item_code"], "plant": r["plant"]}
            reqs.append(R)
            if r["item_code"]:
                open_by_key[(r["item_code"], r["plant"])].append(R)

    # ids in creation order
    for i, R in enumerate(reqs, 1):
        f = R["lines"][0]
        R["first"] = f
        R["req_date"] = f["created"].date()
        R["group_id"] = f"REQ-{i:04d}" if opts.id_mode == "seq" else f"REQ-{f['pr_no']}/{f['pr_line']}"
        R["qty"] = sum(l["qty"] for l in R["lines"])
        R["policy"] = sel.pick(R["req_date"])
        emerg = {str(x).lower() for x in R["policy"].get("params", {}).get("emergency_values", ["emergency"])}
        R["emergency"] = any(l["urgency"].lower() in emerg for l in R["lines"])
        R["policy"] = sel.pick(R["req_date"])

    # ---- 3. contract state, process requirements in requirement-date order -------------------
    consumed = {c["contract_id"]: c["consumed"] + opts.extra_consumed.get(c["contract_id"], 0.0) for c in ref.contracts}
    start_consumed = dict(consumed)
    contract_by_id = {c["contract_id"]: c for c in ref.contracts}
    plcache: dict = {}

    def plist_for(code, d, pol):
        k = (code, d, pol["version"])
        if k not in plcache:
            plcache[k] = build_preferred_list(ref, code, d, pol)
        return plcache[k]

    for R in sorted(reqs, key=lambda R: (R["req_date"], R["first"]["created"], R["first"]["pr_no"], _num_key(R["first"]["pr_line"]))):
        _decide_requirement(R, ref, consumed, contract_by_id, plist_for)

    # ---- 4. PO consolidation (policy 7) ----------------------------------------------------
    pos: list[dict] = []
    for R in reqs:                       # reqs are already in creation-time order
        R["po_id"] = None
        if R["decision"] != "PO":
            continue
        cfg = R["policy"].get("consolidation", {"enabled": True, "window_hours": 24, "match_on": ["supplier_id", "plant", "route"]})
        placed = None
        if cfg.get("enabled", True):
            for po in pos:
                if all(po["key"].get(f) == _req_key(R).get(f) for f in cfg.get("match_on", [])) and \
                        (R["first"]["created"] - po["first_created"]) <= timedelta(hours=cfg.get("window_hours", 24)):
                    placed = po
                    break
        if placed is None:
            n = len(pos) + 1
            placed = {"po_id": f"PO-{n:04d}", "key": _req_key(R), "first_created": R["first"]["created"],
                      "supplier_id": R["supplier_id"], "plant": R["plant"], "route": R["route"], "groups": []}
            if opts.id_mode == "stable":
                placed["po_id"] = f"PO-{R['first']['pr_no']}/{R['first']['pr_line']}"
            pos.append(placed)
        placed["groups"].append(R["group_id"])
        R["po_id"] = placed["po_id"]
        R["reason"] += (f" Placed on {placed['po_id']}." if len(placed["groups"]) == 1 else
                        f" Consolidated into {placed['po_id']} with {', '.join(placed['groups'][:-1])} (policy 7.1).")

    # ---- 5. output rows ---------------------------------------------------------------------
    rows = []
    by_line = {}
    for R in reqs:
        for l in R["lines"]:
            by_line[l["idx"]] = R
    for r in recs:
        raw = r["raw"]
        base = {"pr_no": r["pr_no"], "pr_line": r["pr_line"]}
        if r["problem"]:
            rows.append({**base, "resolved_item_code": "", "group_id": "", "po_id": "", "decision": "", "route": "NOT_PROCESSED",
                         "supplier_id": "", "unit_price": "", "group_value_inr": "", "suggested_supplier_ids": "",
                         "policy_version": "", "reason": "NOT_PROCESSED: " + r["problem"], "flags": [], "created_at": str(raw.get("created_at", ""))})
            continue
        R = by_line[r["idx"]]
        rows.append({**base, "resolved_item_code": R["item_code"] or "NEW", "group_id": R["group_id"], "po_id": R["po_id"] or "",
                     "decision": R["decision"], "route": R["route"], "supplier_id": R["supplier_id"] or "",
                     "unit_price": R["unit_price"] if R["unit_price"] is not None else "",
                     "group_value_inr": round(R["value"], 2),
                     "suggested_supplier_ids": ";".join(R["suggested"]),
                     "policy_version": R["policy"]["version"], "reason": R["reason"], "flags": r["flags"] + R["flags"],
                     "created_at": str(raw.get("created_at", ""))})

    contract_usage = [{"contract_id": cid, "start_consumed": start_consumed[cid], "end_consumed": consumed[cid],
                       "cap": contract_by_id[cid]["cap"]} for cid in consumed if consumed[cid] != start_consumed[cid]]
    return {"rows": rows, "requirements": [_pub(R) for R in reqs], "pos": [{**p, "first_created": p["first_created"].strftime("%Y-%m-%d %H:%M")} for p in pos], "warnings": warnings,
            "contract_usage": contract_usage, "unprocessed": [r["pr_no"] + "/" + r["pr_line"] for r in recs if r["problem"]],
            "policy_versions_used": sorted({R["policy"]["version"] for R in reqs})}


def _num_key(x):
    try:
        return (0, int(x))
    except (TypeError, ValueError):
        return (1, str(x))


def _same_fields(a, b, fields, ref):
    for f in fields:
        if f == "category":
            ca = ref.items[a["item_code"]]["category"] if a.get("item_code") else None
            cb = ref.items[b["item_code"]]["category"] if b.get("item_code") else None
            if ca != cb:
                return False
        elif a.get(f) != b.get(f):
            return False
    return True


def _req_key(R):
    return {"supplier_id": R["supplier_id"], "plant": R["plant"], "route": R["route"], "category": (R.get("item") or {}).get("category")}


def _pub(R):
    keep = ["group_id", "item_code", "plant", "qty", "emergency", "decision", "route", "supplier_id", "unit_price", "value",
            "suggested", "rule_id", "rule_name", "reason", "trace", "contract_id", "valuation", "flags", "po_id",
            "contract_options", "preferred", "suggestions", "req_date", "needs_note"]
    d = {k: R.get(k) for k in keep}
    d["req_date"] = R["req_date"].isoformat()
    d["policy_version"] = R["policy"]["version"]
    d["lines"] = [{"pr_no": l["pr_no"], "pr_line": l["pr_line"], "created_at": l["created"].strftime("%Y-%m-%d %H:%M"),
                   "qty": l["qty"], "need_by": l["need_by"].isoformat() if l["need_by"] else None, "urgency": l["urgency"],
                   "description": l["raw"].get("description"), "requester": l["raw"].get("requester"),
                   "match_status": l.get("match_status"), "match_note": l.get("match_note")} for l in R["lines"]]
    d["item_description"] = (R.get("item") or {}).get("description") or R["lines"][0]["raw"].get("description")
    d["category"] = (R.get("item") or {}).get("category")
    d["item_type"] = (R.get("item") or {}).get("item_type")
    return d


def _decide_requirement(R, ref, consumed, contract_by_id, plist_for):
    pol = R["policy"]
    d = R["req_date"]
    code = R["item_code"]
    item = ref.items.get(code) if code else None
    R["item"] = item
    flags = []
    first = R["first"]
    allowed_p = _allowed(pol.get("params"), pol)
    allowed_c = _allowed(pol.get("contracts"), pol)
    qty = R["qty"]
    recent_days = pol.get("params", {}).get("recent_purchase_days", 365)

    # --- contracts (policy 4) ---
    options, chosen = [], None
    if item:
        for c in ref.contracts:
            if code not in c["lines"] or not (c["start"] and c["end"] and c["start"] <= d <= c["end"]):
                continue
            sup_ok = ref.supplier_ok(c["supplier_id"], allowed_c)
            rate = c["lines"][code]
            val = qty * rate
            cons = consumed[c["contract_id"]]
            room = c["cap"] - cons
            util = pol.get("contracts", {}).get("ceiling_utilisation_pct", 100) / 100.0
            fits = (not pol.get("contracts", {}).get("check_ceiling", True)) or cons + val <= c["cap"] * util + 1e-6
            options.append({"contract_id": c["contract_id"], "supplier_id": c["supplier_id"], "rate": rate, "value_at_rate": val,
                            "consumed": cons, "ceiling": c["cap"], "room": room, "supplier_active": sup_ok, "fits": fits,
                            "source": c["source"], "usable": sup_ok and fits})
        applicable = [o for o in options if o["supplier_active"]]
        usable = sorted([o for o in applicable if o["fits"]], key=lambda o: (o["rate"], o["contract_id"]))
        chosen = usable[0] if usable else None
    else:
        applicable, usable = [], []

    # --- valuation (policy 3) ---
    last_po = None
    val_info = {}
    if R["is_new"]:
        est = first["est"] or 0.0
        up, basis, src = est, "estimate", "requester's estimated unit price"
    elif chosen:
        up, basis, src = chosen["rate"], "contract", f"contract {chosen['contract_id']} rate"
    elif applicable:
        low = min(applicable, key=lambda o: (o["rate"], o["contract_id"]))
        up, basis, src = low["rate"], "contract (ceiling exhausted)", f"lowest applicable contract rate ({low['contract_id']}): every applicable contract is out of ceiling"
    else:
        lb = pol.get("params", {}).get("po_price_lookback_days", 365)
        cands = [p for p in ref.po_by_item.get(code, []) if d - timedelta(days=lb) <= p["date"] < d]
        if cands:
            last_po = cands[-1]
            up, basis, src = last_po["price"], "last PO", f"PO {last_po['po_number']} ({last_po['date']}, {last_po['supplier_id']}) price"
        else:
            up, basis, src = item["standard_price"], "standard price", "item master standard price (no PO in lookback)"
    value = qty * up
    val_info = {"unit_price": up, "basis": basis, "source": src, "qty": qty}

    # --- recent purchase facts ---
    act = None
    if item:
        for p in ref.po_by_item.get(code, []):
            if d - timedelta(days=recent_days) <= p["date"] < d and ref.supplier_ok(p["supplier_id"], allowed_p):
                act = p
    plist = plist_for(code, d, pol) if item else []
    facts = {
        "is_new_item": R["is_new"], "item_type": item["item_type"] if item else None, "category": item["category"] if item else None,
        "item_code": code, "plant": R["plant"], "department": first.get("department"),
        "is_emergency": R["emergency"], "urgency": "Emergency" if R["emergency"] else "Normal",
        "unit_price_inr": round(up, 4), "value_inr": round(value, 2), "quantity": qty, "line_count": len(R["lines"]),
        "need_by_lead_days": (first["need_by"] - d).days if first["need_by"] else None,
        "contract_applicable": bool(applicable), "contract_usable": bool(chosen),
        "contract_ceiling_exhausted": bool(applicable) and not chosen,
        "bought_recently_active": act is not None,
        "days_since_last_active_purchase": (d - act["date"]).days if act else None,
        "preferred_count": len(plist),
    }

    # --- decision rules in order (policy 5) ---
    trace, outcome, hit = [], None, None
    for rule in pol["rules"]:
        leaves = []
        ok = P.evaluate(rule.get("when"), facts, leaves)
        if not ok:
            trace.append({"rule": rule["id"], "name": rule.get("name"), "matched": False, "conditions": leaves})
            continue
        out = _resolve(rule["then"], chosen, plist, ref, item, d, pol, qty)
        if out is None:
            trace.append({"rule": rule["id"], "name": rule.get("name"), "matched": False, "conditions": leaves,
                          "note": "conditions met but no eligible Active supplier for this outcome; next rule tried"})
            continue
        trace.append({"rule": rule["id"], "name": rule.get("name"), "matched": True, "conditions": leaves})
        outcome, hit = out, rule
        break
    if hit is None:
        outcome = {"decision": "SOURCING_REQUEST", "route": "RFP", "supplier_id": None, "unit_price": None, "suggested": [],
                   "suggestions": [], "relied": "none"}
        hit = {"id": "default", "clause": "default", "name": "No rule matched; defaulted to RFP"}
        flags.append("no policy rule matched; defaulted to RFP")
        trace.append({"rule": "default", "name": hit["name"], "matched": True, "conditions": []})

    # contract consumption (policy 4.2): requirements consume ceiling in requirement-date order
    if outcome.get("relied_contract"):
        consumed[outcome["relied_contract"]] += value

    R.update({"decision": outcome["decision"], "route": outcome["route"], "supplier_id": outcome["supplier_id"],
              "unit_price": outcome["unit_price"], "value": value, "suggested": outcome["suggested"],
              "suggestions": outcome.get("suggestions", []), "rule_id": hit.get("clause", hit["id"]),
              "rule_name": hit.get("name"), "trace": trace, "contract_id": outcome.get("relied_contract"),
              "valuation": val_info, "flags": flags, "contract_options": options, "preferred": plist[:6],
              "needs_note": (hit.get("then") or {}).get("note")})

    # reason (policy 8.1: clause, version, contract/PO relied on)
    why = [f"Rule {hit.get('clause', hit['id'])} ({hit.get('name', '')}) of policy v{pol['version']}"]
    cond = [l for t in trace if t["matched"] for l in t["conditions"]]
    if cond:
        why[0] += ": " + "; ".join(f"{c['text']} [{c['actual']}]" for c in cond)
    why.append(f"Requirement {inr(value)} = {qty:g} × {inr(up)} ({src}).")
    if outcome["relied"] and outcome["relied"] != "none":
        why.append(f"Relied on {outcome['relied']}.")
    if R["decision"] == "SOURCING_REQUEST" and outcome["suggested"]:
        why.append("Suggested: " + ", ".join(outcome["suggested"]) + ".")
    if hit.get("then", {}).get("note"):
        why.append(hit["then"]["note"] + ".")
    if R["is_new"]:
        why.append("Item not in item master; buyer to find suppliers.")
    elif options and not chosen:
        bad = [o for o in options if not o["usable"]]
        why.append("Contracts not usable: " + "; ".join(
            f"{o['contract_id']} " + ("supplier not Active" if not o["supplier_active"] else f"ceiling room {inr(o['room'])} < {inr(o['value_at_rate'])}")
            for o in bad) + ".")
    R["reason"] = " ".join(why)


def _resolve(then, chosen, plist, ref, item, d, pol, qty):
    sup = then.get("supplier", {"mode": "none"})
    mode = sup.get("mode")
    base = {"decision": then["decision"], "route": then["route"], "supplier_id": None, "unit_price": None,
            "suggested": [], "suggestions": [], "relied": "none"}
    if mode == "contract":
        if not chosen:
            return None
        return {**base, "supplier_id": chosen["supplier_id"], "unit_price": chosen["rate"],
                "relied": f"contract {chosen['contract_id']} ({chosen['source']} record)", "relied_contract": chosen["contract_id"]}
    if mode == "preferred":
        rank = sup.get("rank", 1)
        if len(plist) < rank:
            return None
        p = plist[rank - 1]
        return {**base, "supplier_id": p["supplier_id"], "unit_price": p["reference_price"],
                "relied": f"preferred supplier #{rank} {p['supplier_id']} {p['name']} (score {p['score']}); reference price from {p['reference_source']}"}
    if mode == "suggest":
        s = suggest_suppliers(ref, item, plist, sup.get("count", 3), d, pol)
        return {**base, "suggested": [x["supplier_id"] for x in s], "suggestions": s,
                "relied": "preferred list" + (" topped up from category" if any(x["source"] != "preferred" for x in s) else "") if item else "none"}
    if mode == "fixed":
        sid = sup.get("supplier_id")
        if not ref.supplier_ok(sid, _allowed(pol.get("params"), pol)):
            return None
        return {**base, "supplier_id": sid, "unit_price": None, "relied": f"policy-named supplier {sid}"}
    return base
