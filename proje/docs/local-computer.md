# Local computer bridge (open a file where the browser can't)

Lets the backend open a just-created artifact (PDF, DOCX, XLSX, ...) in its
default app **on the user's own machine** — not the Docker container, which
has no GUI and can't see the host filesystem. This is same-machine only:
it assumes the backend and the browser are running on the same computer.
Not real MCP — see `scripts/local_computer_bridge.py`'s docstring for why a
minimal HTTP bridge was chosen over hand-rolling MCP's async wire protocol.

## How it works

```mermaid
flowchart LR
  subgraph host [Your machine]
    BR[Browser :8081]
    BRIDGE[local_computer_bridge.py :8765]
    APP[Default app: Preview / Word / Excel...]
    BRIDGE --> APP
  end
  subgraph docker [Docker]
    BE[FastAPI backend :8001]
  end
  BR -->|1. asks a question| BE
  BE -->|2. POST /save-and-open (real bytes) host.docker.internal:8765| BRIDGE
```

1. The bridge (`scripts/local_computer_bridge.py`) runs **natively on the
   host**, not in Docker — only a host process can launch a host GUI app.
   It only writes/opens files inside an allow-listed directory
   (`~/Downloads` by default), never an arbitrary path.
2. The backend container reaches it through Docker's
   `host.docker.internal` DNS name, which always resolves to whichever
   machine is running the containers — so this only works when the
   browser and the backend are on the same computer.
3. `backend/local_computer.py` is the backend-side HTTP client
   (`LocalComputerTools.open_local_file`). When it auto-resolves the file
   from a job's workspace (the common "open what I/we just made" case),
   it reads the artifact's actual bytes and POSTs them (base64) to
   `/save-and-open`, which writes them into the allowed directory
   (overwriting any stale same-named file) and opens THAT — never just a
   bare filename hoping something already matches. `/open-file` (by name
   only, no content) still exists as a fallback for an explicit filename
   with no known disk path, a file over the size cap (~30MB), or an
   older bridge process that predates `/save-and-open`. Either way, only
   a bare filename ever describes a *path* — the container's own path is
   meaningless on the host's OS and is never sent as one.

## Cross-job artifact lookup

Each job gets its own, freshly-created workspace directory keyed by
`job_id` (see `main._resolve_workspace_root`), even within the same
conversation. So "create a PDF" (job A) and a later follow-up "open it on
my computer" (job B) do **not** share a workspace on disk — job B starts
empty. To find what job A produced, `_open_local_file_step` (in
`execution/langgraph_runtime/workforce.py`) falls back to
`db.workspace_roots_for_conversation(conversation_id)`, which walks every
past job in the same conversation, most recent first, and returns the
ones that still exist on disk. `LocalComputerTools` tries the current
job's own workspace first, then each fallback root, until it finds an
artifact.

## Enable it

Host machine — start the bridge before testing:

```bash
python3 scripts/local_computer_bridge.py
# optional: --port 8765 --allow /some/other/dir
curl http://127.0.0.1:8765/health
```

`.env` (repo root, gitignored):

```env
LOCAL_COMPUTER_ENABLED=true
LOCAL_COMPUTER_URL=http://host.docker.internal:8765
```

Rebuild backend:

```bash
docker compose build backend
docker compose up -d backend
```

## What this does NOT do yet

- **Multi-machine / company-wide use** (backend on one machine, browser on
  another) is explicitly out of scope for this design — it would need a
  reversed-connection registry (each machine's bridge dials out to the
  backend, e.g. over WebSocket) instead of `host.docker.internal`.
  Deferred; same-machine is the only supported mode right now.
- **Guaranteed freshness for an explicit filename.** `/save-and-open`
  (the default, auto-resolved path) removes the old staleness risk
  entirely — the real bytes are sent, so the file that gets opened is
  always the one that was actually just produced, regardless of what
  else shares its name in `~/Downloads`. The narrower fallback path
  (`/open-file`, by name only) still matches whatever already exists in
  an allowed directory — this only applies when the model is given an
  *explicit* filename with no known disk path, a file exceeds the
  ~30MB cap, or an older bridge process predates `/save-and-open`.
- `mode: "single"` (the CAMEL direct-chat path) is not wired to this
  fast-path at all yet — only `mode: "multi"` (Workforce) is.

## Manual end-to-end verification

No unit test spins up a real bridge or launches a real app — that needs
an actual machine with a display. To verify by hand:

1. `python3 scripts/local_computer_bridge.py` on your machine.
2. Submit a job that produces an artifact, e.g. `POST /run-pipeline` with
   `{"topic": "kısa bir PDF hazırla", "mode": "multi"}`. Note the
   returned `workspace_id`/`conversation_id`.
3. Poll `GET /jobs/{job_id}` until `status: done`; confirm an artifact is
   listed.
4. Submit a follow-up in the **same** `workspace_id`/`conversation_id`:
   `{"topic": "bunu bilgisayarımda aç", "mode": "multi", "workspace_id":
   ..., "conversation_id": ...}`.
5. Confirm the result is `Açıldı: ...` (not `no_filename`), the job's
   `task_plan` has no Research/Document tasks (the fast-path skipped
   them), and the app actually opened (e.g. `ps aux | grep -i preview`
   on macOS).
6. To specifically verify `/save-and-open` sent real bytes (not just a
   name): before step 4, overwrite the artifact's filename in
   `~/Downloads` with obviously different placeholder content, then run
   step 4. The opened file should show the real artifact's content, and
   `ls -la` on it should match the artifact's real `size_bytes`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Bilgisayarda dosya açma özelliği bu kurulumda kapalı` | `LOCAL_COMPUTER_ENABLED=true` and backend rebuilt |
| `köprüye ulaşılamadı` | Bridge running on the host, `curl http://127.0.0.1:8765/health` from the host |
| `no_filename` | Nothing on disk yet in this job's or any prior job's workspace in the conversation — has anything actually been created? |
| Opens the wrong (stale) file | Only possible via the `/open-file` by-name fallback (explicit filename, oversized file, or an old bridge process) — see "guaranteed freshness" above. Restart the bridge to pick up `/save-and-open` if it predates this. |
