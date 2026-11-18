"""SQLite persistence (stdlib only). One file DB; path from DATABASE_PATH."""
from __future__ import annotations
import csv, io, json, os, sqlite3, threading, zipfile
from datetime import datetime
from pathlib import Path

DB_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "corvo.db"))
_local = threading.local()
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(item_code TEXT PRIMARY KEY, description TEXT, uom TEXT, category TEXT, item_type TEXT, standard_price REAL);
CREATE TABLE IF NOT EXISTS suppliers(supplier_id TEXT PRIMARY KEY, name TEXT, city TEXT, categories TEXT, status TEXT, on_time_pct REAL, quality_rating REAL);
CREATE TABLE IF NOT EXISTS plants(plant TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS po_history(po_number TEXT, po_date TEXT, supplier_id TEXT, item_code TEXT, quantity REAL, uom TEXT, unit_price REAL, plant TEXT, delivered_on_time TEXT);
CREATE INDEX IF NOT EXISTS ix_po_item ON po_history(item_code);
CREATE TABLE IF NOT EXISTS contracts(contract_id TEXT PRIMARY KEY, supplier_id TEXT, supplier_name TEXT, start_date TEXT, end_date TEXT,
  value_cap REAL, consumed_value REAL DEFAULT 0, source TEXT DEFAULT 'register', pdf_name TEXT, added_by TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS contract_lines(contract_id TEXT, item_code TEXT, description TEXT, uom TEXT, rate REAL, PRIMARY KEY(contract_id,item_code));
CREATE TABLE IF NOT EXISTS policies(id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT, effective_from TEXT, status TEXT,
  doc TEXT, base_id INTEGER, source_name TEXT, source_text TEXT, source_kind TEXT, changes TEXT, notes TEXT,
  created_by TEXT, created_at TEXT, approved_by TEXT, approved_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, label TEXT, created_by TEXT, created_at TEXT,
  policy_mode TEXT, forced_version TEXT, lines_json TEXT, result_json TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, group_id TEXT, action TEXT, actor TEXT,
  role TEXT, comment TEXT, at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, doc_no TEXT, doc_type TEXT, route TEXT,
  supplier_id TEXT, plant TEXT, status TEXT, groups TEXT, created_by TEXT, created_at TEXT, UNIQUE(run_id, doc_no));
CREATE TABLE IF NOT EXISTS chat_sessions(id TEXT PRIMARY KEY, state TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, role TEXT, event TEXT, detail TEXT);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _local.c = c
    return c


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def audit(actor, role, event, detail=""):
    x("INSERT INTO audit(at,actor,role,event,detail) VALUES(?,?,?,?,?)", (now(), actor, role, event, detail if isinstance(detail, str) else json.dumps(detail)))


# ---------------- master data ----------------------------------------------------------------
FILES = {"item_master": "items", "supplier_master": "suppliers", "plants": "plants", "po_history": "po_history",
         "contract_register": "contract_register", "contract_lines": "contract_lines"}


def _rows(data: bytes):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def load_master(files: dict[str, bytes]) -> dict:
    """files: basename (without .csv) -> bytes. Replaces the tables it receives; keeps PDF/manual contracts."""
    out = {}
    with _lock:
        c = conn()
        if "item_master" in files:
            c.execute("DELETE FROM items")
            r = _rows(files["item_master"])
            c.executemany("INSERT INTO items VALUES(?,?,?,?,?,?)", [(i["item_code"], i["description"], i["uom"], i["category"], i["item_type"], float(i["standard_price"] or 0)) for i in r])
            out["items"] = len(r)
        if "supplier_master" in files:
            c.execute("DELETE FROM suppliers")
            r = _rows(files["supplier_master"])
            c.executemany("INSERT INTO suppliers VALUES(?,?,?,?,?,?,?)", [(s["supplier_id"], s["name"], s["city"], s["categories"], s["status"], float(s["on_time_pct"] or 0), float(s["quality_rating"] or 0)) for s in r])
            out["suppliers"] = len(r)
        if "plants" in files:
            c.execute("DELETE FROM plants")
            r = _rows(files["plants"])
            c.executemany("INSERT INTO plants VALUES(?,?)", [(p["plant"], p["name"]) for p in r])
            out["plants"] = len(r)
        if "po_history" in files:
            c.execute("DELETE FROM po_history")
            r = _rows(files["po_history"])
            c.executemany("INSERT INTO po_history VALUES(?,?,?,?,?,?,?,?,?)", [(p["po_number"], p["po_date"], p["supplier_id"], p["item_code"], float(p["quantity"] or 0), p["uom"], float(p["unit_price"] or 0), p["plant"], p["delivered_on_time"]) for p in r])
            out["po_history"] = len(r)
        if "contract_register" in files:
            c.execute("DELETE FROM contracts WHERE source='register'")
            c.execute("DELETE FROM contract_lines WHERE contract_id NOT IN (SELECT contract_id FROM contracts)")
            r = _rows(files["contract_register"])
            c.executemany("INSERT OR REPLACE INTO contracts(contract_id,supplier_id,supplier_name,start_date,end_date,value_cap,consumed_value,source,added_by,added_at) VALUES(?,?,?,?,?,?,?,'register','import',?)",
                          [(k["contract_id"], k["supplier_id"], k["supplier_name"], k["start_date"], k["end_date"], float(k["value_cap"]), float(k["consumed_value"] or 0), now()) for k in r])
            out["contracts"] = len(r)
        if "contract_lines" in files:
            ids = {x_["contract_id"] for x_ in _rows(files["contract_lines"])}
            c.execute("DELETE FROM contract_lines WHERE contract_id IN (%s)" % ",".join("?" * len(ids)), list(ids)) if ids else None
            r = _rows(files["contract_lines"])
            c.executemany("INSERT OR REPLACE INTO contract_lines VALUES(?,?,?,?,?)", [(k["contract_id"], k["item_code"], k["description"], k["uom"], float(k["rate"])) for k in r])
            out["contract_lines"] = len(r)
        c.commit()
    bump()
    return out


def files_from_upload(name: str, data: bytes) -> dict[str, bytes]:
    found = {}
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                b = Path(n).stem
                if n.lower().endswith(".csv") and b in FILES:
                    found[b] = z.read(n)
    else:
        b = Path(name).stem
        if b in FILES:
            found[b] = data
    return found


def load_dir(path: str) -> dict:
    p = Path(path)
    files = {}
    for f in p.rglob("*.csv"):
        if f.stem in FILES:
            files[f.stem] = f.read_bytes()
    return load_master(files) if files else {}


# ---------------- cached reference ------------------------------------------------------------
_version = {"n": 0, "ref": None, "n_ref": -1}


def bump():
    _version["n"] += 1


def master_loaded() -> bool:
    return (q1("SELECT COUNT(*) n FROM items") or {"n": 0})["n"] > 0


def reference():
    from .engine import Reference
    if _version["ref"] is None or _version["n_ref"] != _version["n"]:
        lines: dict = {}
        for l in q("SELECT * FROM contract_lines"):
            lines.setdefault(l["contract_id"], {})[l["item_code"]] = l["rate"]
        contracts = [{**c, "lines": lines.get(c["contract_id"], {})} for c in q("SELECT * FROM contracts")]
        _version["ref"] = Reference(q("SELECT * FROM items"), q("SELECT * FROM suppliers"), q("SELECT * FROM plants"),
                                    q("SELECT * FROM po_history"), contracts)
        _version["n_ref"] = _version["n"]
    return _version["ref"]


# ---------------- policies ---------------------------------------------------------------------
def approved_policies() -> list[dict]:
    out = []
    for p in q("SELECT * FROM policies WHERE status='approved'"):
        d = json.loads(p["doc"])
        d["version"], d["effective_from"] = p["version"], p["effective_from"]
        d["_policy_id"] = p["id"]
        out.append(d)
    return out


def seed_policy():
    if q1("SELECT 1 FROM policies LIMIT 1"):
        return
    doc = json.loads((Path(__file__).parent / "data" / "policy_v5_0.json").read_text())
    x("INSERT INTO policies(version,effective_from,status,doc,source_name,source_kind,created_by,created_at,approved_by,approved_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (doc["version"], doc["effective_from"], "approved", json.dumps(doc), "Corvo_Procurement_Policy.pdf (v5.0)", "seed", "system", now(), "system", now(), "Initial policy v5.0 transcribed into the rule schema"))
