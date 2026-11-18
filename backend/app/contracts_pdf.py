"""Extract a rate contract from its signed PDF (the kit's template). Deterministic regex first;
if that finds no lines and an LLM key exists, the LLM reads the text. The buyer always reviews before saving."""
from __future__ import annotations
import io, re
from pypdf import PdfReader
from . import llm


def pdf_text(data: bytes) -> str:
    r = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in r.pages)


def _n(s):
    return float(s.replace(",", ""))


def parse_contract_text(t: str) -> dict:
    out = {"contract_id": None, "supplier_id": None, "supplier_name": None, "start_date": None, "end_date": None,
           "value_cap": None, "lines": [], "warnings": []}
    m = re.search(r"Contract\s*No\.?\s*:?\s*(RC-\d{4}-\d+)", t) or re.search(r"(RC-\d{4}-\d+)", t)
    out["contract_id"] = m.group(1) if m else None
    m = re.search(r"\(?\s*Supplier\s*ID\s*(S\d+)", t)
    out["supplier_id"] = m.group(1) if m else None
    m = re.search(r"and\s+(.+?),\s+[A-Za-z ]+\s*\(?\s*Supplier\s*ID", t, re.S)
    out["supplier_name"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    m = re.search(r"valid\s+from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", t)
    if m:
        out["start_date"], out["end_date"] = m.group(1), m.group(2)
    m = re.search(r"(?:shall\s+not\s+exceed|ceiling[^.]{0,40}?)\s*(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)", t, re.I)
    if m:
        out["value_cap"] = _n(m.group(1))
    for m in re.finditer(r"^\s*\d+\s+(ITM-\d+)\s+(.+?)\s+([A-Z]{2,4})\s+([\d,]+\.\d+)\s*$", t, re.M):
        out["lines"].append({"item_code": m.group(1), "description": m.group(2).strip(), "uom": m.group(3), "rate": _n(m.group(4))})
    return out


def extract(data: bytes) -> dict:
    text = pdf_text(data)
    res = parse_contract_text(text)
    res["method"] = "regex"
    missing = [k for k in ("contract_id", "supplier_id", "start_date", "end_date", "value_cap") if not res[k]] + (["lines"] if not res["lines"] else [])
    if missing and llm.enabled():
        try:
            j = llm.complete_json("Extract a rate contract. Keys: contract_id, supplier_id, supplier_name, start_date (YYYY-MM-DD), end_date (YYYY-MM-DD), value_cap (number, INR), "
                                  "lines: [{item_code, description, uom, rate}].", text[:12000])
            for k in ("contract_id", "supplier_id", "supplier_name", "start_date", "end_date", "value_cap", "lines"):
                if not res.get(k) and j.get(k):
                    res[k] = j[k]
            res["method"] = "regex+llm"
        except Exception as e:  # noqa
            res["warnings"].append(f"LLM extraction failed: {e}")
    still = [k for k in ("contract_id", "supplier_id", "start_date", "end_date", "value_cap") if not res[k]] + (["lines"] if not res["lines"] else [])
    if still:
        res["warnings"].append("Could not read: " + ", ".join(still) + ". Please fill these in before saving.")
    res["text_preview"] = text[:600]
    return res
