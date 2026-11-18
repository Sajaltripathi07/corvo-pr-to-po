#!/usr/bin/env python3
"""Produce the three blind-test output files by calling the running app's own API (no engine code is touched).

  run1: process the batch under the policy version that was active before the amendment
  run2: process the same batch after the amendment was uploaded + approved in the app
  preferred: top-3 preferred suppliers per item as of a date

Example:
  python scripts/make_submission.py --base https://your-app.example --batch new_batch.csv --name jane_doe --phase run1 --policy-version 5.0
  python scripts/make_submission.py --base https://your-app.example --batch new_batch.csv --name jane_doe --phase run2
  python scripts/make_submission.py --base https://your-app.example --name jane_doe --phase preferred --as-of 2026-11-19
"""
import argparse, pathlib, sys
import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True); ap.add_argument("--name", required=True)
ap.add_argument("--phase", choices=["run1", "run2", "preferred"], required=True)
ap.add_argument("--batch"); ap.add_argument("--policy-version", default=""); ap.add_argument("--as-of")
ap.add_argument("--out", default="submission")
a = ap.parse_args()
out = pathlib.Path(a.out); out.mkdir(exist_ok=True)
H = {"X-User": a.name, "X-Role": "buyer"}
with httpx.Client(base_url=a.base, timeout=180, headers=H) as c:
    if a.phase == "preferred":
        r = c.get("/api/preferred/export.csv", params={"as_of": a.as_of}); r.raise_for_status()
        (out / f"{a.name}__preferred.csv").write_text(r.text)
    else:
        data = {"policy_version": a.policy_version} if (a.phase == "run1" and a.policy_version) else {"policy_mode": "by_date"}
        r = c.post("/api/process-batch", data=data, files={"file": open(a.batch, "rb")}); r.raise_for_status()
        j = r.json(); print(j["summary"])
        csvr = c.get(j["export_url"]); csvr.raise_for_status()
        (out / f"{a.name}__{a.phase}.csv").write_text(csvr.text)
print("written to", out)
