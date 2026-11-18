"""Engine tests on a tiny synthetic data set (no task data files are used or committed)."""
import copy, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine import process, Reference, ProcessOptions, build_preferred_list
from app.engine import policy as P
from app.engine.util import parse_date

POL = json.loads((Path(__file__).resolve().parents[1] / "app/data/policy_v5_0.json").read_text())
ITEMS = [dict(item_code="ITM-1", description="Hex bolt M10x40 GR 8.8", uom="NOS", category="Fasteners", item_type="Consumable", standard_price=7),
         dict(item_code="ITM-2", description="Hydraulic oil ISO 68", uom="LTR", category="Lubricants", item_type="Consumable", standard_price=170),
         dict(item_code="ITM-3", description="EOT crane 5 ton", uom="NOS", category="Capital Equipment", item_type="Capex", standard_price=1500000)]
SUP = [dict(supplier_id=s, name=n, city="x", categories=c, status=st, on_time_pct=ot, quality_rating=q) for s, n, c, st, ot, q in [
    ("S1", "Alpha", "Fasteners;Lubricants", "Active", 90, 4.5), ("S2", "Beta", "Fasteners", "Active", 80, 4.0),
    ("S3", "Gamma", "Lubricants;Capital Equipment", "Blocked", 99, 5.0), ("S4", "Delta", "Lubricants", "Active", 70, 3.0),
    ("S5", "Eps", "Capital Equipment", "Active", 85, 4.0)]]
PO = [dict(po_number="1", po_date="2026-09-01", supplier_id="S1", item_code="ITM-2", quantity=10, unit_price=166, plant="PUN1", delivered_on_time="True"),
      dict(po_number="2", po_date="2026-08-01", supplier_id="S3", item_code="ITM-2", quantity=10, unit_price=150, plant="PUN1", delivered_on_time="True"),
      dict(po_number="3", po_date="2026-09-10", supplier_id="S1", item_code="ITM-1", quantity=10, unit_price=7.1, plant="PUN1", delivered_on_time="True")]
CON = [dict(contract_id="RC-1", supplier_id="S1", start_date="2026-01-01", end_date="2026-12-31", value_cap=1000, consumed_value=0, lines={"ITM-1": 6.5}),
       dict(contract_id="RC-B", supplier_id="S3", start_date="2026-01-01", end_date="2026-12-31", value_cap=10**9, consumed_value=0, lines={"ITM-2": 100})]


def ref(contracts=CON):
    return Reference(ITEMS, SUP, ["PUN1", "CHK1"], PO, copy.deepcopy(contracts))


def line(pr, item="ITM-1", qty=10, created="2026-11-02 10:00", need="2026-11-20", plant="PUN1", urg="Normal", desc="", est=""):
    return dict(pr_no=pr, pr_line=1, created_at=created, plant=plant, item_code=item, description=desc, quantity=qty, uom="NOS",
                estimated_unit_price=est, need_by_date=need, urgency=urg)


def run(lines, pols=None, **kw):
    res = process(lines, ref(), pols or [POL], ProcessOptions(**kw))
    return res, {(r["pr_no"]): r for r in res["rows"]}


def test_clubbing_stops_splitting_below_limit():
    # 4 x 60k lines each look like spot buys; together they cross the contract/spot thresholds
    res, rows = run([line("A", "ITM-2", 400, "2026-11-02 09:00"), line("B", "ITM-2", 400, "2026-11-03 09:00", need="2026-11-22")])
    assert rows["A"]["group_id"] == rows["B"]["group_id"]
    assert rows["A"]["group_value_inr"] == 800 * 166 and rows["A"]["route"] == "SPOT_PO"


def test_clubbing_respects_windows():
    _, rows = run([line("A"), line("B", created="2026-11-20 09:00", need="2026-11-20")])      # 18 days apart
    assert rows["A"]["group_id"] != rows["B"]["group_id"]
    _, rows = run([line("A"), line("B", need="2026-12-05")])                                 # need-by 15 days apart
    assert rows["A"]["group_id"] != rows["B"]["group_id"]


def test_contract_ceiling_consumed_in_order_then_falls_through():
    # ceiling 1000 at rate 6.5: first need 100 (650) fits, second 100 (650) does not
    res, rows = run([line("A", qty=100, created="2026-11-02 09:00", need="2026-11-03"), line("B", qty=100, created="2026-11-09 09:00", need="2026-11-30")])
    assert rows["A"]["route"] == "CONTRACT_PO" and rows["A"]["supplier_id"] == "S1" and rows["A"]["unit_price"] == 6.5
    assert rows["B"]["route"] == "SPOT_PO" and "ceiling" in rows["B"]["reason"]


def test_blocked_supplier_contract_ignored_and_never_suggested():
    res, rows = run([line("A", "ITM-2", 100)])
    assert rows["A"]["supplier_id"] != "S3"
    pl = build_preferred_list(ref(), "ITM-2", parse_date("2026-11-02"), POL)
    assert [p["supplier_id"] for p in pl] == ["S1"]


def test_new_item_and_capex_and_bad_rows():
    res, rows = run([line("N", item="", desc="Ultrasonic thickness gauge", est=45000), line("C", "ITM-3", 1),
                     line("Z", qty=0), line("P", plant="XXX")])
    assert rows["N"]["route"] == "NEW_ITEM_SOURCING" and rows["C"]["route"] == "CAPEX_SOURCING"
    assert rows["Z"]["route"] == "NOT_PROCESSED" and rows["P"]["route"] == "NOT_PROCESSED"
    assert "S5" in rows["C"]["suggested_supplier_ids"] and "S3" not in rows["C"]["suggested_supplier_ids"]


def test_emergency_needs_recent_active_purchase():
    _, rows = run([line("E", "ITM-2", 2000, urg="Emergency")])              # 332k, bought from S1 recently
    assert rows["E"]["route"] == "EMERGENCY_PO"
    _, rows = run([line("E", "ITM-2", 4000, urg="Emergency")])              # 664k > emergency limit
    assert rows["E"]["route"] == "RFQ"


def test_po_consolidation_within_24h():
    _, rows = run([line("A", "ITM-2", 10, "2026-11-02 09:00"), line("B", "ITM-2", 10, "2026-11-02 20:00", need="2026-12-20"),
                   line("C", "ITM-2", 10, "2026-11-02 09:30", plant="CHK1")])
    assert rows["A"]["po_id"] == rows["B"]["po_id"] != rows["C"]["po_id"]


def test_policy_is_data_changing_it_changes_decisions_without_code():
    lines = [line("A", "ITM-2", 1500)]                                      # 249k
    _, rows = run(lines)
    assert rows["A"]["route"] == "RFQ"
    new = P.apply_patch(POL, [{"op": "set_limit", "rule_id": "5.4", "fact": "value_inr", "value": 300000}])
    new["version"], new["effective_from"] = "5.1", "2026-11-01"
    res, rows = run(lines, [POL, new])
    assert rows["A"]["route"] == "SPOT_PO" and rows["A"]["policy_version"] == "5.1"
    # a version that is not yet effective does not apply
    new["effective_from"] = "2026-12-01"
    _, rows = run(lines, [POL, new])
    assert rows["A"]["route"] == "RFQ" and rows["A"]["policy_version"] == "5.0"


def test_new_rule_kind_added_as_data():
    rule = {"id": "5.3A", "clause": "5.3A", "name": "CHK1 oil always spot", "when": {"all": [
        {"fact": "plant", "op": "==", "value": "CHK1"}, {"fact": "category", "op": "==", "value": "Lubricants"},
        {"fact": "value_inr", "op": "<", "value": 500000}]},
        "then": {"decision": "PO", "route": "SPOT_PO", "supplier": {"mode": "preferred", "rank": 1}}}
    new = P.apply_patch(POL, [{"op": "add_rule", "rule": rule, "before": "5.4"}])
    assert P.validate(new) == []
    new["version"], new["effective_from"] = "5.1", "2026-11-01"
    lines = [line("A", "ITM-2", 2500, plant="CHK1")]                       # 415k, would be RFQ
    assert run(lines)[1]["A"]["route"] == "RFQ"
    assert run(lines, [POL, new])[1]["A"]["route"] == "SPOT_PO"


def test_patch_rejects_unknown_parameter_and_validate_catches_bad_rules():
    import pytest
    with pytest.raises(ValueError):
        P.apply_patch(POL, [{"op": "set", "path": "rules", "value": []}])
    bad = copy.deepcopy(POL); bad["rules"][0]["when"] = {"all": [{"fact": "nope", "op": "==", "value": 1}]}
    assert P.validate(bad)


def test_item_matching_abbreviations_and_noise():
    ref_ = ref()
    from app.engine.matching import ItemMatcher
    m = ItemMatcher(ref_.items, POL["matching"])
    assert m.match("hyd oil iso 68 urgent")["item_code"] == "ITM-2"
    assert m.match("hex bolt m10x40 gr 8.8 as per sample")["item_code"] == "ITM-1"
    assert m.match("hex bolt m12x40 gr 8.8")["status"] == "new"               # a different size is not the same item
