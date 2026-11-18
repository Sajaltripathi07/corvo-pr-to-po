"""FastAPI app: thin HTTP layer over db / service / engine."""
from __future__ import annotations
import csv, io, json, os
from datetime import date
from pathlib import Path
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, Query
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, service, chat, llm, ingest, contracts_pdf
from .engine import policy as P, build_preferred_list
from .engine.util import parse_date

app = FastAPI(title="Corvo PR-to-PO", version="1.0")


@app.on_event("startup")
def startup():
    db.seed_policy()
    if not db.master_loaded():
        d = os.environ.get("DATA_DIR")
        if d and Path(d).exists():
            db.load_dir(d)
        url = os.environ.get("SEED_ZIP_URL")
        if url and not db.master_loaded():
            try:
                import httpx
                r = httpx.get(url, timeout=60, follow_redirects=True)
                db.load_master(db.files_from_upload("seed.zip", r.content))
            except Exception as e:  # noqa
                print("seed failed", e)


def who(x_user, x_role):
    return (x_user or "anonymous"), (x_role or "requester")


def need_role(role, *allowed):
    if role not in allowed:
        raise HTTPException(403, f"Role '{role}' cannot do this; needs one of: {', '.join(allowed)}")


def need_master():
    if not db.master_loaded():
        raise HTTPException(409, "Master data is not loaded yet. Upload it under Setup (item/supplier master, PO history, contracts).")


@app.get("/api/status")
def status():
    pol = db.approved_policies()
    return {"master_loaded": db.master_loaded(), "llm": llm.enabled(), "model": llm.MODEL if llm.enabled() else None,
            "counts": {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("items", "suppliers", "po_history", "contracts")},
            "active_policy": max(pol, key=lambda p: (p["effective_from"], p["version"]))["version"] if pol else None,
            "plants": db.q("SELECT * FROM plants")}


# ---------------- master data ---------------------------------------------------------------------------
@app.post("/api/master-data")
async def upload_master(files: list[UploadFile] = File(...), x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "procurement", "buyer")
    found = {}
    for f in files:
        found.update(db.files_from_upload(f.filename, await f.read()))
    if not found:
        raise HTTPException(400, "No recognised files. Expected item_master, supplier_master, plants, po_history, contract_register, contract_lines (.csv) or a .zip of them.")
    res = db.load_master(found)
    db.audit(u, r, "master.loaded", res)
    return res


@app.get("/api/items")
def items(): return db.q("SELECT * FROM items ORDER BY item_code")


@app.get("/api/suppliers")
def suppliers(): return db.q("SELECT * FROM suppliers ORDER BY supplier_id")


# ---------------- contracts -------------------------------------------------------------------------------
@app.get("/api/contracts")
def contracts():
    out = db.q("SELECT * FROM contracts ORDER BY contract_id")
    lines = db.q("SELECT * FROM contract_lines")
    for c in out:
        c["lines"] = [l for l in lines if l["contract_id"] == c["contract_id"]]
    return out


class ContractIn(BaseModel):
    contract_id: str
    supplier_id: str
    start_date: str
    end_date: str
    value_cap: float
    consumed_value: float = 0
    lines: list[dict]
    source: str = "manual"
    pdf_name: str | None = None


@app.post("/api/contracts/extract-pdf")
async def extract_contract(file: UploadFile = File(...)):
    return contracts_pdf.extract(await file.read())


@app.post("/api/contracts")
def add_contract(c: ContractIn, x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "buyer", "procurement")
    sup = db.q1("SELECT * FROM suppliers WHERE supplier_id=?", (c.supplier_id,))
    if not sup:
        raise HTTPException(400, f"Unknown supplier {c.supplier_id}")
    if parse_date(c.start_date) is None or parse_date(c.end_date) is None or parse_date(c.end_date) < parse_date(c.start_date):
        raise HTTPException(400, "Dates must be YYYY-MM-DD and end must not be before start")
    if not c.lines:
        raise HTTPException(400, "A contract needs at least one item line")
    for l in c.lines:
        if not db.q1("SELECT 1 FROM items WHERE item_code=?", (l["item_code"],)):
            raise HTTPException(400, f"Unknown item {l['item_code']}")
    if db.q1("SELECT 1 FROM contracts WHERE contract_id=?", (c.contract_id,)):
        raise HTTPException(409, f"{c.contract_id} already exists")
    db.x("INSERT INTO contracts(contract_id,supplier_id,supplier_name,start_date,end_date,value_cap,consumed_value,source,pdf_name,added_by,added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (c.contract_id, c.supplier_id, sup["name"], c.start_date, c.end_date, c.value_cap, c.consumed_value, c.source, c.pdf_name, u, db.now()))
    for l in c.lines:
        db.x("INSERT OR REPLACE INTO contract_lines VALUES(?,?,?,?,?)", (c.contract_id, l["item_code"], l.get("description", ""), l.get("uom", ""), float(l["rate"])))
    db.bump()
    db.audit(u, r, "contract.added", {"contract": c.contract_id, "source": c.source})
    return {"ok": True}


# ---------------- policies --------------------------------------------------------------------------------
def _pol_row(p, full=False):
    p = dict(p)
    p["changes"] = json.loads(p["changes"] or "[]")
    d = json.loads(p["doc"])
    p["rules_text"] = [P.describe_rule(r) for r in d["rules"]]
    if full:
        p["doc"] = d
    else:
        p.pop("doc"); p.pop("source_text", None)
    return p


@app.get("/api/policies")
def list_policies():
    return [_pol_row(p) for p in db.q("SELECT * FROM policies ORDER BY id DESC")]


@app.get("/api/policies/{pid}")
def get_policy(pid: int):
    p = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    if not p: raise HTTPException(404)
    return _pol_row(p, True)


@app.get("/api/policy-schema")
def policy_schema():
    return {"facts": {k: {"label": v[0], "type": v[1]} for k, v in P.FACTS.items()}, "operators": P.OPS, "modes": sorted(P.SUPPLIER_MODES)}


def _base_doc():
    pols = db.approved_policies()
    return max(pols, key=lambda p: (p["effective_from"], p["version"])), {p["version"] for p in db.q("SELECT version FROM policies")}


@app.post("/api/policies/propose")
async def propose_policy(file: UploadFile | None = File(None), instruction: str = Form(""), x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "procurement")
    active, taken = _base_doc()
    text, kind, obj, name = instruction or "", "chat", None, "chat instruction"
    if file is not None and file.filename:
        kind, t, obj = ingest.extract_text(file.filename, await file.read())
        text, name = (t + "\n" + text), file.filename
    if not text.strip() and obj is None:
        raise HTTPException(400, "Upload a policy/amendment document or describe the change.")
    res = ingest.propose(active, taken, text, obj)
    nd = res["new_doc"]
    changes = P.diff({k: v for k, v in active.items() if not k.startswith("_")}, nd)
    notes = res["notes"] + ([f"Needs a human: {s}" for s in res["unclear"]]) + res["errors"]
    pid = db.x("INSERT INTO policies(version,effective_from,status,doc,base_id,source_name,source_text,source_kind,changes,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (nd["version"], nd["effective_from"], "draft", json.dumps(nd), active["_policy_id"], name, text[:20000], f"{kind}/{res['method']}",
                json.dumps(changes), json.dumps(notes), u, db.now()))
    db.audit(u, r, "policy.proposed", {"id": pid, "version": nd["version"], "method": res["method"]})
    out = _pol_row(db.q1("SELECT * FROM policies WHERE id=?", (pid,)), True)
    out["unclear"], out["errors"] = res["unclear"], res["errors"]
    return out


class DraftEdit(BaseModel):
    doc: dict


@app.put("/api/policies/{pid}")
def edit_draft(pid: int, body: DraftEdit, x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "procurement")
    p = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    if not p or p["status"] != "draft": raise HTTPException(400, "Only drafts can be edited")
    errs = P.validate(body.doc)
    if errs: raise HTTPException(422, "; ".join(errs))
    base = json.loads(db.q1("SELECT doc FROM policies WHERE id=?", (p["base_id"],))["doc"])
    ch = P.diff(base, body.doc)
    db.x("UPDATE policies SET doc=?, version=?, effective_from=?, changes=? WHERE id=?",
         (json.dumps(body.doc), body.doc["version"], body.doc["effective_from"], json.dumps(ch), pid))
    db.audit(u, r, "policy.draft_edited", {"id": pid})
    return _pol_row(db.q1("SELECT * FROM policies WHERE id=?", (pid,)), True)


@app.get("/api/policies/{pid}/impact")
def impact(pid: int):
    """Re-run the most recent batch under the draft and report which decisions would change."""
    from .engine import process, ProcessOptions
    p = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    run = db.q1("SELECT id FROM runs WHERE kind='batch' ORDER BY id DESC LIMIT 1")
    if not p or not run: return {"available": False}
    r = service.get_run(run["id"])
    d = json.loads(p["doc"]); d["version"], d["effective_from"] = p["version"], p["effective_from"]
    base = [x for x in db.approved_policies() if x["version"] != p["version"]]
    new = process(r["lines"], db.reference(), base + [d], ProcessOptions(policy_mode="version", forced_version=p["version"]))
    old = {(x["pr_no"], x["pr_line"]): x for x in process(r["lines"], db.reference(), base, ProcessOptions()) ["rows"]}
    ch = []
    for x in new["rows"]:
        o = old[(x["pr_no"], x["pr_line"])]
        if (o["route"], o["supplier_id"], o["group_id"]) != (x["route"], x["supplier_id"], x["group_id"]) or o["suggested_supplier_ids"] != x["suggested_supplier_ids"]:
            ch.append({"pr": f"{x['pr_no']}/{x['pr_line']}", "from": o["route"], "to": x["route"], "supplier_from": o["supplier_id"], "supplier_to": x["supplier_id"]})
    return {"available": True, "run_id": run["id"], "lines": len(new["rows"]), "changed": len(ch), "examples": ch[:25]}


@app.post("/api/policies/{pid}/approve")
def approve_policy(pid: int, x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "procurement")
    p = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    if not p or p["status"] != "draft": raise HTTPException(400, "Only a draft can be approved")
    errs = P.validate(json.loads(p["doc"]))
    if errs: raise HTTPException(422, "; ".join(errs))
    if db.q1("SELECT 1 FROM policies WHERE version=? AND status='approved'", (p["version"],)):
        raise HTTPException(409, f"Version {p['version']} already exists; change the version number first")
    db.x("UPDATE policies SET status='approved', approved_by=?, approved_at=? WHERE id=?", (u, db.now(), pid))
    db.audit(u, r, "policy.approved", {"id": pid, "version": p["version"], "effective_from": p["effective_from"]})
    return {"ok": True, "version": p["version"], "effective_from": p["effective_from"]}


@app.post("/api/policies/{pid}/reject")
def reject_policy(pid: int, x_user: str = Header(None), x_role: str = Header(None)):
    u, r = who(x_user, x_role)
    need_role(r, "procurement")
    db.x("UPDATE policies SET status='rejected' WHERE id=? AND status='draft'", (pid,))
    db.audit(u, r, "policy.rejected", {"id": pid})
    return {"ok": True}


# ---------------- batches & runs ---------------------------------------------------------------------------
@app.post("/api/process-batch")
async def process_batch(file: UploadFile = File(...), policy_mode: str = Form("by_date"), policy_version: str = Form(""),
                        label: str = Form(""), x_user: str = Header(None), x_role: str = Header(None)):
    """Takes a PR batch CSV (sample format), returns one decision row per PR line."""
    need_master()
    u, r = who(x_user, x_role)
    try:
        lines = list(csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig"), newline="")))
    except Exception:
        raise HTTPException(400, "Could not read the file as CSV")
    need = {"pr_no", "pr_line", "created_at", "plant", "quantity", "need_by_date", "urgency"}
    if not lines or not need <= set(lines[0].keys()):
        raise HTTPException(400, f"CSV must have columns: {', '.join(sorted(need))}, item_code, description, estimated_unit_price")
    try:
        rid = service.run_batch(lines, policy_mode, policy_version or None, "batch", label or file.filename, u)
    except ValueError as e:
        raise HTTPException(400, str(e))
    run = service.get_run(rid)
    db.audit(u, r, "batch.processed", {"run": rid, "lines": len(lines), "versions": run["summary"]["policy_versions"]})
    return {"run_id": rid, "summary": run["summary"], "decisions": run["result"]["rows"], "warnings": run["result"]["warnings"],
            "export_url": f"/api/runs/{rid}/export.csv"}


@app.get("/api/runs")
def runs():
    out = db.q("SELECT id,kind,label,created_by,created_at,policy_mode,forced_version,summary_json FROM runs ORDER BY id DESC")
    for r in out: r["summary"] = json.loads(r.pop("summary_json") or "{}")
    return out


@app.get("/api/runs/{rid}")
def run_detail(rid: int):
    run, reqs = service.view_requirements(rid)
    return {"id": rid, "kind": run["kind"], "label": run["label"], "created_at": run["created_at"], "summary": run["summary"],
            "requirements": reqs, "pos": run["result"]["pos"], "contract_usage": run["result"].get("contract_usage", []),
            "unprocessed": [r for r in run["result"]["rows"] if r["route"] == "NOT_PROCESSED"]}


@app.get("/api/runs/{rid}/export.csv", response_class=PlainTextResponse)
def run_export(rid: int):
    run = service.get_run(rid)
    if not run: raise HTTPException(404)
    return PlainTextResponse(service.rows_to_csv(run["result"]["rows"]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="decisions_run{rid}.csv"'})


class ActIn(BaseModel):
    comment: str = ""
    override: dict | None = None


def _act(rid, gid, action, body, x_user, x_role):
    u, r = who(x_user, x_role)
    need_role(r, "buyer", "procurement")
    try:
        res = service.act(rid, gid, action, u, r, body.comment, body.override)
    except KeyError: raise HTTPException(404, "Requirement not found")
    except ValueError as e: raise HTTPException(400, str(e))
    if service.get_run(rid)["kind"] == "live":
        service.reprocess_live()
    return res


@app.post("/api/runs/{rid}/requirements/{gid:path}/approve")
def approve(rid: int, gid: str, body: ActIn = ActIn(), x_user: str = Header(None), x_role: str = Header(None)):
    return _act(rid, gid, "approve", body, x_user, x_role)


@app.post("/api/runs/{rid}/requirements/{gid:path}/override")
def override(rid: int, gid: str, body: ActIn, x_user: str = Header(None), x_role: str = Header(None)):
    return _act(rid, gid, "override", body, x_user, x_role)


@app.get("/api/live")
def live():
    return run_detail(service.live_run_id())


# ---------------- chat -------------------------------------------------------------------------------------
class ChatIn(BaseModel):
    session_id: str
    message: str


@app.post("/api/chat")
def chat_msg(b: ChatIn, x_user: str = Header(None), x_role: str = Header(None)):
    need_master()
    return chat.reply(b.session_id, b.message, (x_user or "requester"))


@app.get("/api/chat/{sid}")
def chat_hist(sid: str):
    return chat.history(sid)


# ---------------- preferred suppliers ---------------------------------------------------------------------
@app.get("/api/preferred")
def preferred(item_code: str, as_of: str = Query(None)):
    need_master()
    d = parse_date(as_of) or date.today()
    ref = db.reference()
    if item_code not in ref.items: raise HTTPException(404, "Unknown item")
    pols = db.approved_policies()
    pol = ([p for p in pols if parse_date(p["effective_from"]) <= d] or pols)[-1]
    return {"item": ref.items[item_code], "as_of": d.isoformat(), "policy_version": pol["version"],
            "weights": pol["preferred_list"]["weights"], "list": build_preferred_list(ref, item_code, d, pol)}


@app.get("/api/preferred/export.csv", response_class=PlainTextResponse)
def preferred_export(as_of: str, policy_version: str = ""):
    need_master()
    d = parse_date(as_of)
    if not d: raise HTTPException(400, "as_of must be YYYY-MM-DD")
    rows = service.preferred_rows(d, policy_version or None)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["item_code", "rank", "supplier_id", "reference_price"], extrasaction="ignore", lineterminator="\n")
    w.writeheader(); w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="preferred_{as_of}.csv"'})


@app.get("/api/audit")
def audit_log(limit: int = 200):
    return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/documents")
def documents():
    return db.q("SELECT * FROM documents ORDER BY id DESC LIMIT 300")


FRONT = Path(__file__).resolve().parents[2] / "frontend"
if FRONT.exists():
    app.mount("/static", StaticFiles(directory=FRONT), name="static")

    @app.get("/")
    def index(): return FileResponse(FRONT / "index.html")
