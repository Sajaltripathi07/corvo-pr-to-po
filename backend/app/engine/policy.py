"""Policy-as-data: schema, rule evaluation, plain-language rendering, diffing and patching.

A policy is a JSON document. The engine contains no business thresholds; everything a
procurement policy decides (limits, windows, rule order, conditions, outcomes, ranking
weights) is read from the active document. New rule *kinds* are expressed by combining the
fact catalogue below with `when` conditions and `then` outcomes, so adding e.g. a
plant-specific rule needs a new rule in the document, not a code change.
"""
from __future__ import annotations
import copy
import re
from .util import inr

# ---- fact catalogue (what rules can talk about) ----------------------------------------
FACTS = {
    "is_new_item": ("the item is not in the item master", "bool"),
    "item_type": ("item type", "str"),
    "category": ("item category", "str"),
    "item_code": ("item code", "str"),
    "plant": ("plant", "str"),
    "department": ("requesting department", "str"),
    "is_emergency": ("any line is marked Emergency", "bool"),
    "value_inr": ("requirement value (INR)", "num"),
    "quantity": ("total quantity", "num"),
    "line_count": ("number of PR lines clubbed", "num"),
    "need_by_lead_days": ("days from requirement date to need-by", "num"),
    "urgency": ("urgency of the requirement (Emergency or Normal)", "str"),
    "unit_price_inr": ("unit price used to value the requirement (INR)", "num"),
    "contract_applicable": ("a rate contract covers the item on the requirement date", "bool"),
    "contract_usable": ("an applicable rate contract has enough ceiling", "bool"),
    "contract_ceiling_exhausted": ("contracts apply but none has enough ceiling", "bool"),
    "bought_recently_active": ("item bought from an Active supplier within the recent-purchase window", "bool"),
    "days_since_last_active_purchase": ("days since the item was last bought from an Active supplier", "num"),
    "preferred_count": ("suppliers on the item's preferred list", "num"),
}
OPS = {"==": "is", "!=": "is not", "<": "is below", "<=": "is at most", ">": "is above", ">=": "is at least",
       "in": "is one of", "not_in": "is not one of"}
SUPPLIER_MODES = {"contract", "preferred", "suggest", "none", "fixed"}
PARAM_PATHS = {
    "clubbing.created_within_days", "clubbing.need_by_within_days", "clubbing.enabled", "clubbing.same_fields",
    "clubbing.never_club_new_items", "params.recent_purchase_days", "params.po_price_lookback_days",
    "params.supplier_statuses_allowed", "params.emergency_values", "contracts.check_ceiling",
    "contracts.ceiling_utilisation_pct", "contracts.supplier_statuses_allowed",
    "preferred_list.po_window_days", "preferred_list.include_contract_holders", "preferred_list.min_on_time_pct",
    "preferred_list.min_quality", "preferred_list.weights", "preferred_list.recency_tiers",
    "preferred_list.recency_default", "suggestions.topup_from_category", "suggestions.topup_weights",
    "consolidation.enabled", "consolidation.window_hours", "consolidation.match_on",
    "matching.aliases", "matching.noise_phrases", "effective_from", "version", "title",
}


# ---- evaluation --------------------------------------------------------------------------
def _cmp(a, op, b) -> bool:
    if a is None:
        return op == "!=" or (op == "not_in")
    try:
        if op == "==": return a == b
        if op == "!=": return a != b
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "in": return a in b
        if op == "not_in": return a not in b
    except TypeError:
        return False
    return False


def evaluate(when: dict | None, facts: dict, leaves: list | None = None) -> bool:
    """Evaluate a condition tree. `leaves` collects (text, passed) for the buyer-facing trace."""
    leaves = leaves if leaves is not None else []
    if not when:
        return True
    if "all" in when:
        results = [evaluate(c, facts, leaves) for c in when["all"]]
        return all(results)
    if "any" in when:
        results = [evaluate(c, facts, leaves) for c in when["any"]]
        return any(results)
    if "not" in when:
        return not evaluate(when["not"], facts, leaves)
    fact, op, val = when["fact"], when["op"], when.get("value")
    actual = facts.get(fact)
    ok = _cmp(actual, op, val)
    leaves.append({"text": describe_leaf(when), "actual": _fmt(fact, actual), "passed": ok})
    return ok


def _fmt(fact, v):
    if v is None:
        return "n/a"
    if fact == "value_inr":
        return inr(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def describe_leaf(c: dict) -> str:
    fact, op, val = c["fact"], c["op"], c.get("value")
    label = FACTS.get(fact, (fact,))[0]
    if isinstance(val, bool):
        if op == "==":
            return label if val else f"NOT ({label})"
        return f"NOT ({label})" if val else label
    shown = inr(val) if fact == "value_inr" and isinstance(val, (int, float)) else (
        ", ".join(map(str, val)) if isinstance(val, list) else str(val))
    return f"{label} {OPS.get(op, op)} {shown}"


def describe_when(when: dict | None) -> str:
    if not when:
        return "otherwise (nothing above applied)"
    if "all" in when:
        return " AND ".join(_wrap(c) for c in when["all"])
    if "any" in when:
        return " OR ".join(_wrap(c) for c in when["any"])
    if "not" in when:
        return "NOT (" + describe_when(when["not"]) + ")"
    return describe_leaf(when)


def _wrap(c):
    s = describe_when(c)
    return f"({s})" if ("all" in c or "any" in c) and " " in s else s


def describe_then(then: dict) -> str:
    sup = then.get("supplier", {"mode": "none"})
    m = sup.get("mode")
    who = {"contract": "contract supplier at the contract rate",
           "preferred": f"preferred supplier #{sup.get('rank', 1)} at its reference price",
           "suggest": f"suggest {sup.get('count', 3)} suppliers",
           "none": "no supplier suggestions",
           "fixed": f"supplier {sup.get('supplier_id')}"}.get(m, str(m))
    note = f" ({then['note']})" if then.get("note") else ""
    return f"{then.get('decision')} / {then.get('route')}: {who}{note}"


def describe_rule(r: dict) -> str:
    w = describe_when(r.get("when"))
    cond = w if not r.get("when") else "when " + w
    return f"{r.get('name', r['id'])} [{r.get('clause', r['id'])}]: {cond} → {describe_then(r['then'])}"


# ---- validation ---------------------------------------------------------------------------
def _validate_when(w, errs, where):
    if not w:
        return
    for k in ("all", "any"):
        if k in w:
            if not isinstance(w[k], list):
                errs.append(f"{where}: '{k}' must be a list")
            else:
                for c in w[k]:
                    _validate_when(c, errs, where)
            return
    if "not" in w:
        _validate_when(w["not"], errs, where)
        return
    if w.get("fact") not in FACTS:
        errs.append(f"{where}: unknown fact '{w.get('fact')}' (allowed: {', '.join(FACTS)})")
    if w.get("op") not in OPS:
        errs.append(f"{where}: unknown operator '{w.get('op')}'")
    if "value" not in w:
        errs.append(f"{where}: condition on {w.get('fact')} has no value")


def validate(doc: dict) -> list[str]:
    errs: list[str] = []
    for k in ("version", "effective_from", "rules"):
        if k not in doc:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(doc["effective_from"])):
        errs.append("effective_from must be YYYY-MM-DD")
    ids = set()
    for r in doc["rules"]:
        rid = r.get("id")
        if not rid or rid in ids:
            errs.append(f"rule id missing or duplicated: {rid}")
        ids.add(rid)
        _validate_when(r.get("when"), errs, f"rule {rid}")
        t = r.get("then") or {}
        if t.get("decision") not in ("PO", "SOURCING_REQUEST"):
            errs.append(f"rule {rid}: decision must be PO or SOURCING_REQUEST")
        if not t.get("route"):
            errs.append(f"rule {rid}: route is required")
        if (t.get("supplier") or {}).get("mode") not in SUPPLIER_MODES:
            errs.append(f"rule {rid}: supplier.mode must be one of {sorted(SUPPLIER_MODES)}")
        if t.get("decision") == "PO" and (t.get("supplier") or {}).get("mode") in ("none", "suggest"):
            errs.append(f"rule {rid}: a PO outcome needs supplier mode contract/preferred/fixed")
    return errs


# ---- patching -----------------------------------------------------------------------------
def _set_path(doc, path, value):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _find_rule(doc, rid):
    for i, r in enumerate(doc["rules"]):
        if r["id"] == rid:
            return i, r
    raise ValueError(f"rule {rid} not found")


def _set_leaf(when, fact, value, op=None) -> bool:
    if not when:
        return False
    for k in ("all", "any"):
        if k in when:
            return any([_set_leaf(c, fact, value, op) for c in when[k]])
    if "not" in when:
        return _set_leaf(when["not"], fact, value, op)
    if when.get("fact") == fact:
        when["value"] = value
        if op:
            when["op"] = op
        return True
    return False


def apply_patch(doc: dict, ops: list[dict]) -> dict:
    """Apply structured edit operations to a copy of `doc`. Used by the chat/LLM/heuristic readers."""
    d = copy.deepcopy(doc)
    for op in ops:
        kind = op.get("op")
        if kind == "set":
            if op["path"] not in PARAM_PATHS:
                raise ValueError(f"path '{op['path']}' is not an editable policy parameter")
            _set_path(d, op["path"], op["value"])
        elif kind == "set_limit":      # change the number in an existing rule condition
            _, r = _find_rule(d, op["rule_id"])
            if not _set_leaf(r.get("when"), op["fact"], op["value"], op.get("operator")):
                raise ValueError(f"rule {op['rule_id']} has no condition on {op['fact']}")
        elif kind == "add_rule":
            rule = op["rule"]
            if any(r["id"] == rule["id"] for r in d["rules"]):
                raise ValueError(f"rule id {rule['id']} already exists")
            if op.get("before"):
                i, _ = _find_rule(d, op["before"])
            elif op.get("after"):
                i = _find_rule(d, op["after"])[0] + 1
            elif op.get("position") == "end":
                i = len(d["rules"])
            else:
                i = 0
            d["rules"].insert(i, rule)
        elif kind == "update_rule":
            _, r = _find_rule(d, op["id"])
            for k, v in op["set"].items():
                r[k] = v
        elif kind == "move_rule":
            i, r = _find_rule(d, op["id"])
            d["rules"].pop(i)
            if op.get("before"):
                j, _ = _find_rule(d, op["before"])
            elif op.get("after"):
                j = _find_rule(d, op["after"])[0] + 1
            else:
                j = len(d["rules"]) if op.get("position") == "end" else 0
            d["rules"].insert(j, r)
        elif kind == "remove_rule":
            i, _ = _find_rule(d, op["id"])
            d["rules"].pop(i)
        else:
            raise ValueError(f"unknown patch operation {kind}")
    return d


# ---- diff in plain language -----------------------------------------------------------------
def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict) and k not in ("aliases",):
            out.update(_flat(v, p + "."))
        else:
            out[p] = v
    return out


PARAM_LABELS = {
    "clubbing.created_within_days": "Clubbing: lines created within N days of the first line",
    "clubbing.need_by_within_days": "Clubbing: need-by dates within N days of the first line",
    "clubbing.same_fields": "Clubbing: lines must share",
    "params.recent_purchase_days": "'Bought recently' window (days)",
    "params.po_price_lookback_days": "Valuation: last-PO price lookback (days)",
    "preferred_list.po_window_days": "Preferred list: PO history window (days)",
    "preferred_list.weights": "Preferred list: ranking weights",
    "consolidation.window_hours": "PO consolidation window (hours)",
    "consolidation.match_on": "PO consolidation: lines must share",
    "effective_from": "Effective date", "version": "Version",
}


def diff(old: dict, new: dict) -> list[dict]:
    """Return a list of {type, area, text} describing what changes between two policy documents."""
    changes = []
    fo, fn = _flat({k: v for k, v in old.items() if k != "rules"}), _flat({k: v for k, v in new.items() if k != "rules"})
    for k in sorted(set(fo) | set(fn)):
        if fo.get(k) != fn.get(k):
            if k in ("version", "title"):
                continue
            lab = PARAM_LABELS.get(k, k)
            changes.append({"type": "changed" if k in fo and k in fn else ("added" if k in fn else "removed"),
                            "area": "parameter", "text": f"{lab}: {fo.get(k, '—')} → {fn.get(k, '—')}"})
    ro, rn = {r["id"]: r for r in old["rules"]}, {r["id"]: r for r in new["rules"]}
    for rid, r in rn.items():
        if rid not in ro:
            changes.append({"type": "added", "area": "rule", "text": "New rule — " + describe_rule(r)})
        elif r != ro[rid]:
            parts = []
            if r.get("when") != ro[rid].get("when"):
                parts.append(f"condition was “{describe_when(ro[rid].get('when'))}”, now “{describe_when(r.get('when'))}”")
            if r.get("then") != ro[rid].get("then"):
                parts.append(f"outcome was “{describe_then(ro[rid]['then'])}”, now “{describe_then(r['then'])}”")
            if not parts:
                parts.append("wording changed")
            changes.append({"type": "changed", "area": "rule", "text": f"Rule {rid} ({r.get('name', '')}): " + "; ".join(parts)})
    for rid in ro:
        if rid not in rn:
            changes.append({"type": "removed", "area": "rule", "text": f"Rule {rid} ({ro[rid].get('name', '')}) removed"})
    oo, on = [r["id"] for r in old["rules"] if r["id"] in rn], [r["id"] for r in new["rules"] if r["id"] in ro]
    if oo != on:
        changes.append({"type": "changed", "area": "rule-order", "text": "Rule order changed: " + " → ".join(r["id"] for r in new["rules"])})
    return changes
