# Corvo PR-to-PO: chat purchase requests decided by a policy stored as data

Employees raise requests in chat; the engine decides **PO vs sourcing request** by following the active procurement policy. Procurement uploads a new policy version and the decisions change, with no code change.

## Run it

```bash
docker compose up --build            # http://localhost:8000
# or
cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload
```

Optional: copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (better amendment/chat reading; everything works without it).

**First start**: open **Setup & audit** and upload the master data (the six CSVs or the kit zip). The repo deliberately contains no task data. Then add the PDF-only contracts under **Contracts** (RC-2026-031, RC-2026-033 in the sample kit). On a host with an ephemeral disk, mount a persistent volume at `/data` (see `render.yaml`) or set `SEED_ZIP_URL`/`DATA_DIR` to auto-load on first start.

## Using it

| Page                | Who              | What                                                          |
| ------------------- | ---------------- | ------------------------------------------------------------- |
| Raise a request     | requester        | chat; asks only for what is missing, confirms, creates the PR |
| Upload batch        | buyer            | CSV in -> decisions out, CSV download                         |
| Buyer workspace     | buyer            | requirement list, "why" trace, approve / override             |
| Preferred suppliers | any              | ranked list with score components, CSV export for a date      |
| Contracts           | buyer            | add contracts from signed PDFs                                |
| Policy              | procurement head | upload amendment -> diff + impact -> approve                  |

Pick a role in the header (no login; see DECISIONS.md #20).

## API

`POST /api/process-batch` (multipart `file`, optional `policy_mode=by_date|latest`, `policy_version=5.0`) returns one decision per PR line; `GET /api/runs/{id}/export.csv` downloads it. Other endpoints are under `/docs`.

## Layout

`backend/app/engine` (pure decision logic: matching, policy language, decide) · `backend/app/{main,service,db,chat,ingest}.py` · `frontend/` · `backend/tests` · `DECISIONS.md` · `APPROACH.md`
