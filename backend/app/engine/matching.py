"""Item matching: free-text PR descriptions -> item master (policy 1.1 / 1.2).

Deterministic, explainable fuzzy matcher. Abbreviations / noise phrases come from the
policy document (`matching` block) so Procurement can extend them without code changes.
Never invents an item: below the confidence bar the result is NEW (policy 1.2).
"""
from __future__ import annotations
import re
from difflib import SequenceMatcher

DEFAULT_CFG = {
    "aliases": {"brg": "bearing", "pneu": "pneumatic", "hyd": "hydraulic", "rod": "electrode",
                "elec": "electrical", "ctr": "centre", "cyl": "cylinder"},
    "noise_phrases": ["as per sample", "for maintenance", "urgent", "urgently", "asap", "repeat",
                      "please", "pls", "required", "need", "needed", "immediately"],
    "min_precision": 0.75,   # share of requester tokens found in the item description
    "min_coverage": 0.60,    # share of item-description tokens covered by the requester
    "ambiguity_margin": 0.06,
}

_UNITS = r"(mm|cm|inch|in|cum|kg|ltr|l|kw|hp|ton|t|gsm|micron|lph|a|v|w|sqmm|kva|mtr|m)"


def _norm(text: str, cfg: dict, strip_noise: bool) -> list[str]:
    s = str(text or "").lower()
    s = s.replace("”", '"').replace("″", '"')
    s = re.sub(r'(\d)\s*"', r"\1 inch ", s)
    s = re.sub(r"(\d)\s*'", r"\1 ft ", s)
    if strip_noise:
        for ph in sorted(cfg["noise_phrases"], key=len, reverse=True):
            s = re.sub(r"(?<![a-z0-9])" + re.escape(ph.lower()) + r"(?![a-z0-9])", " ", s)
    s = re.sub(r"(\d)\s+" + _UNITS + r"\b", r"\1\2", s)          # '24 inch' -> '24inch'
    s = s.replace("&", " and ")
    toks = re.findall(r"[a-z0-9]+(?:[./][a-z0-9]+)*", s)
    out = []
    for t in toks:
        t = cfg["aliases"].get(t, t)
        if len(t) > 3 and t.endswith("s") and not t[-2] in "su" and not re.search(r"\d", t):
            t = t[:-1]                                         # gloves -> glove
        out.append(t)
    return out


class ItemMatcher:
    def __init__(self, items: dict[str, dict], cfg: dict | None = None):
        self.items = items
        self.cfg = {**DEFAULT_CFG, **(cfg or {})}
        self.cfg["aliases"] = {**DEFAULT_CFG["aliases"], **(cfg or {}).get("aliases", {})}
        self._toks = {c: _norm(i["description"], self.cfg, False) for c, i in items.items()}

    def candidates(self, text: str, top: int = 3) -> list[dict]:
        q = _norm(text, self.cfg, True)
        if not q:
            return []
        qset = set(q)
        qdig = {t for t in qset if re.search(r"\d", t)}
        res = []
        for code, d in self._toks.items():
            dset = set(d)
            common = qset & dset
            if not common:
                continue
            prec = len(common) / len(qset)
            cov = len(common) / len(dset)
            f = 2 * prec * cov / (prec + cov)
            seq = SequenceMatcher(None, " ".join(sorted(qset)), " ".join(sorted(dset))).ratio()
            score = 0.65 * f + 0.35 * seq
            ddig = {t for t in dset if re.search(r"\d", t)}
            digit_conflict = bool((qdig - dset) or (ddig - qset))
            if digit_conflict:
                score *= 0.6
            res.append({"item_code": code, "description": self.items[code]["description"],
                        "score": round(score, 3), "precision": round(prec, 3), "coverage": round(cov, 3),
                        "digit_conflict": digit_conflict})
        res.sort(key=lambda r: (-r["score"], r["item_code"]))
        return res[:top]

    def match(self, text: str) -> dict:
        """Returns {item_code|None, status: matched|ambiguous|new, candidates, note}."""
        c = self.candidates(text, 3)
        if not c:
            return {"item_code": None, "status": "new", "candidates": [], "note": "no similar item in master"}
        best = c[0]
        strict = best["precision"] >= self.cfg["min_precision"] and best["coverage"] >= self.cfg["min_coverage"] \
            and not best["digit_conflict"]
        # allow a short but exact-subset description ("laptop") only when it is unambiguous
        subset = best["precision"] == 1.0 and best["coverage"] >= 0.25 and not best["digit_conflict"] \
            and (len(c) == 1 or best["score"] - c[1]["score"] >= 0.15)
        if not (strict or subset):
            return {"item_code": None, "status": "new", "candidates": c,
                    "note": f"closest was {best['item_code']} but too different (score {best['score']})"}
        if len(c) > 1 and best["score"] - c[1]["score"] < self.cfg["ambiguity_margin"] and c[1]["precision"] >= 0.75:
            return {"item_code": best["item_code"], "status": "ambiguous", "candidates": c,
                    "note": f"{best['item_code']} vs {c[1]['item_code']} are close"}
        return {"item_code": best["item_code"], "status": "matched", "candidates": c,
                "note": f"matched {best['item_code']} (score {best['score']})"}
