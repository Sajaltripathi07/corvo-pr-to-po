# APPROACH.md

## What it is
A FastAPI + SQLite service with a no-build vanilla-JS front end. Four ideas:

1. **Policy is a versioned JSON document** (`backend/app/data/policy_v5_0.json` is v5.0, transcribed from the PDF). It holds the rule list (ordered conditions and outcomes), limits, windows, consolidation settings, ranking weights and matching vocabularies. The engine (`backend/app/engine/`) is a generic interpreter with no business thresholds in it.
2. **Amendments go through the app**: upload a PDF/DOCX/text/JSON (or type an instruction) -> the reader turns it into structured patch operations -> the app shows a plain-language diff, an impact preview against the latest batch, and any clauses it could not read -> a Procurement head edits (rule builder / JSON) and approves -> the new version is active from its effective date. No deploy, no code change.
3. **Every decision is explainable**: each requirement stores the rules tried in order with the actual values, the contract/PO relied on, and the full preferred-supplier scoring.
4. **Buyers stay in control**: approve, or override with a mandatory reason; Blocked/Inactive suppliers are refused server-side; everything is in an audit log.

## Pipeline (`engine/decide.py`)
validate -> match items -> sort by creation time -> club (2) -> value (3) -> contracts and ceilings (4) -> rules (5) -> suppliers / suggestions (6) -> PO consolidation (7) -> one row per PR line.

## AI usage
- **Build**: I used Claude (Anthropic) as a coding assistant to write the code and tests, and to cross-check the engine against the worked examples. Item values, suppliers and suggestion lists reproduce the examples; the three preferred-supplier ranking examples reproduce exactly.
- **Inside the app (optional)**: with `ANTHROPIC_API_KEY` set, the app uses an Anthropic model (default `claude-sonnet-4-6`, override with `ANTHROPIC_MODEL`) to (a) turn amendment text into patch operations, validated and dry-run before use with one automatic retry, and (b) read chat messages and contract PDFs. **Without a key everything still works** through deterministic fallbacks (a clause reader for limits/windows/dates, a regex chat parser, a regex contract-PDF reader), and anything they cannot read is surfaced for a human rather than guessed. The decisions themselves never come from an LLM: they are made by the deterministic engine, so runs are repeatable.

## Verification
`cd backend && python -m pytest tests` runs 11 tests on synthetic data (clubbing windows, ceiling order, blocked suppliers, new/capex/bad rows, emergency, consolidation, "changing the policy changes decisions", a new plant/category rule added as data, patch/validation safety, matching). The sample batch and worked examples were checked locally against the kit but, per the brief, no task data is in this repo.

## Known limits
- The LLM path could not be exercised without a key in the build environment; the fallback path was tested.
- Heuristic amendment reading handles limits/windows/dates; scoped clauses ("for plant X ...") are deliberately sent to the rule builder instead of guessed.
- No authentication (see DECISIONS.md #20). SQLite on one disk: fine for this scale, not for multi-instance hosting.
