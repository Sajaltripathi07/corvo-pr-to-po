"""Application services: runs, live pool, buyer actions, preferred-supplier export."""
from __future__ import annotations
import csv, io, json
from datetime import date
from . import db
from .engine import process, ProcessOptions, build_preferred_list
from .engine.util import parse_date

CSV_COLUMNS = ["pr_no", "pr_line", "resolved_item_code", "group_id", "po_id", "decision", "route", "supplier_id", "unit_price",
               "group_value_inr", "suggested_supplier_ids", "policy_version", "reason"]
PO_ROUTES = {"CONTRACT_PO", "EMERGENCY_PO", "SPOT_PO"}


def policies():
    p = db.approved_policies()
    if not p:
        raise RuntimeError("No approved policy")
    return p


def run_batch(lines, mode="by_date", version=None, kind="batch", label="", user="system") -> int:
    opts = ProcessOptions(policy_mode=mode if not version else "version", forced_version=version)
    res = process(lines, db.reference(), policies(), opts)
    summ = _summary(res)
    return db.x("INSERT INTO runs(kind,label,created_by,created_at,policy_mode,forced_version,lines_json,result_json,summary_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, label, user, db.now(), opts.policy_mode, version, json.dumps(lines), json.dumps(res), json.dumps(summ)))


def _summary(res):
    routes = {}
    for r in res["rows"]:
        routes[r["route"]] = routes.get(r["route"], 0) + 1
    return {"lines": len(res["rows"]), "requirements": len(res["requirements"]), "pos": len(res["pos"]), "routes": routes,
            "policy_versions": res["policy_versions_used"], "not_processed": len(res["unprocessed"])}


def get_run(run_id):
    r = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    if not r:
        return None
    r["result"] = json.loads(r.pop("result_json"))
    r["summary"] = json.loads(r.pop("summary_json"))
    r["lines"] = json.loads(r.pop("lines_json"))
    return r


def statuses(run_id):
    """group_id -> latest action (approve/override)."""
    out = {}
    for a in db.q("SELECT * FROM actions WHERE run_id=? ORDER BY id", (run_id,)):
        a["payload"] = json.loads(a["payload"] or "{}")
        out.setdefault(a["group_id"], []).append(a)
    return out


def view_requirements(run_id):
    run = get_run(run_id)
    acts = statuses(run_id)
    docs = {}
    for d in db.q("SELECT * FROM documents WHERE run_id=?", (run_id,)):
        for g in json.loads(d["groups"]):
            docs[g] = d["doc_no"]
    out = []
    for R in run["result"]["requirements"]:
        R = dict(R)
        a = acts.get(R["group_id"], [])
        R["original"] = {k: R.get(k) for k in ("decision", "route", "supplier_id", "unit_price", "suggested")}
        R["status"] = "PENDING"
        for x in a:
            if x["action"] == "approve":
                R["status"] = "APPROVED"
            elif x["action"] == "override":
                R["status"] = "OVERRIDDEN"
                R.update(x["payload"].get("after", {}))
        R["history"] = a
        R["document_no"] = docs.get(R["group_id"])
        out.append(R)
    return run, out


def _make_document(run_id, R, actor):
    route = R["route"]
    is_po = R["decision"] == "PO"
    doc_no = R.get("po_id") if (is_po and R.get("po_id")) else f"{'PO' if is_po else route}-{R['group_id']}"
    ex = db.q1("SELECT * FROM documents WHERE run_id=? AND doc_no=?", (run_id, doc_no))
    if ex:
        groups = json.loads(ex["groups"])
        if R["group_id"] not in groups:
            groups.append(R["group_id"])
            db.x("UPDATE documents SET groups=? WHERE id=?", (json.dumps(groups), ex["id"]))
    else:
        db.x("INSERT INTO documents(run_id,doc_no,doc_type,route,supplier_id,plant,status,groups,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
             (run_id, doc_no, "PO" if is_po else "SOURCING", route, R.get("supplier_id"), R["plant"], "CREATED", json.dumps([R["group_id"]]), actor, db.now()))
    return doc_no


def act(run_id, group_id, action, actor, role, comment="", override=None):
    run, reqs = view_requirements(run_id)
    R = next((r for r in reqs if r["group_id"] == group_id), None)
    if not R:
        raise KeyError("requirement not found")
    if R["status"] != "PENDING":
        raise ValueError(f"already {R['status'].lower()}")
    payload = {"before": R["original"]}
    if action == "override":
        if not (comment or "").strip():
            raise ValueError("An override needs a written reason")
        ov = override or {}
        route = ov.get("route") or R["route"]
        dec = "PO" if route in PO_ROUTES or ov.get("decision") == "PO" else "SOURCING_REQUEST"
        after = {"decision": dec, "route": route, "supplier_id": ov.get("supplier_id") or (R["supplier_id"] if dec == "PO" else None),
                 "unit_price": ov.get("unit_price", R["unit_price"] if dec == "PO" else None),
                 "suggested": ov.get("suggested") or (R["suggested"] if dec != "PO" else [])}
        if dec == "PO":
            sup = db.reference().suppliers.get(after["supplier_id"])
            if not sup or sup["status"] in ("Blocked", "Inactive"):
                raise ValueError("Blocked or Inactive suppliers cannot receive a PO (policy 5.7)")
        else:
            bad = [s for s in after["suggested"] if (db.reference().suppliers.get(s) or {"status": "?"})["status"] != "Active"]
            if bad:
                raise ValueError(f"Cannot suggest non-Active suppliers: {', '.join(bad)} (policy 5.7)")
        payload["after"] = after
        R.update(after)
    aid = db.x("INSERT INTO actions(run_id,group_id,action,actor,role,comment,at,payload) VALUES(?,?,?,?,?,?,?,?)",
               (run_id, group_id, action, actor, role, comment, db.now(), json.dumps(payload)))
    doc_no = _make_document(run_id, R, actor)
    db.audit(actor, role, f"requirement.{action}", {"run": run_id, "group": group_id, "document": doc_no, "comment": comment})
    return {"action_id": aid, "document_no": doc_no}


# ---------------- live pool (chat-created PRs) -------------------------------------------------------
def live_run_id() -> int:
    r = db.q1("SELECT id FROM runs WHERE kind='live'")
    if r:
        return r["id"]
    return db.x("INSERT INTO runs(kind,label,created_by,created_at,policy_mode,lines_json,result_json,summary_json) VALUES('live','Chat requests (live pool)','system',?,'by_date','[]',?,'{}')",
                (db.now(), json.dumps({"rows": [], "requirements": [], "pos": [], "warnings": [], "unprocessed": [], "policy_versions_used": []})))


def next_pr_no() -> str:
    run = get_run(live_run_id())
    return f"PR-C-{len(set(l['pr_no'] for l in run['lines'])) + 1:04d}"


def add_live_line(line: dict) -> int:
    rid = live_run_id()
    run = get_run(rid)
    run["lines"].append(line)
    db.x("UPDATE runs SET lines_json=? WHERE id=?", (json.dumps(run["lines"]), rid))
    reprocess_live()
    return rid


def reprocess_live():
    rid = live_run_id()
    run = get_run(rid)
    _, reqs = view_requirements(rid)
    frozen = [r for r in reqs if r["status"] != "PENDING"]
    fkeys = {(l["pr_no"], l["pr_line"]) for r in frozen for l in r["lines"]}
    open_lines = [l for l in run["lines"] if (str(l["pr_no"]), str(l["pr_line"])) not in fkeys]
    consumed = {}
    for r in frozen:
        if r.get("contract_id") and r["decision"] == "PO" and r["route"] == "CONTRACT_PO":
            consumed[r["contract_id"]] = consumed.get(r["contract_id"], 0) + r["value"]
    res = process(open_lines, db.reference(), policies(), ProcessOptions(id_mode="stable", extra_consumed=consumed))
    old = {r["group_id"]: r for r in run["result"]["requirements"] if r["group_id"] in {f["group_id"] for f in frozen}}
    res["requirements"] = [old.get(f["group_id"], f) for f in frozen] + res["requirements"]
    db.x("UPDATE runs SET result_json=?, summary_json=? WHERE id=?", (json.dumps(res), json.dumps(_summary(res)), rid))


# ---------------- exports ------------------------------------------------------------------------------
def rows_to_csv(rows) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def preferred_rows(as_of: date, version=None, all_ranks=False):
    ref = db.reference()
    pols = policies()
    pol = next((p for p in pols if p["version"] == version), None) if version else \
        ([p for p in pols if parse_date(p["effective_from"]) <= as_of] or pols)[-1]
    out = []
    for code in sorted(ref.items):
        for p in build_preferred_list(ref, code, as_of, pol)[: (None if all_ranks else 3)]:
            out.append({"item_code": code, "rank": p["rank"], "supplier_id": p["supplier_id"], "reference_price": p["reference_price"],
                        "score": p["score"], "policy_version": pol["version"]})
    return out
