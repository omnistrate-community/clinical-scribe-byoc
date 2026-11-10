"""HTTP API and web UI for the clinical scribe agent."""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import db

STT_URL = os.environ.get("STT_URL", "http://localhost:8002").rstrip("/")
HOSPITAL_NAME = os.environ.get("HOSPITAL_NAME", "Example General Hospital")
DEFAULT_CLINICIAN = os.environ.get("DEFAULT_CLINICIAN", "Dr. Demo Clinician")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    yield
    await db.pool.close()


app = FastAPI(title="clinical-scribe-agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DraftRequest(BaseModel):
    patient_id: str
    transcript: str
    clinician: str | None = None


class ApproveRequest(BaseModel):
    note: str
    clinician: str | None = None
    title: str = "Clinic visit note"


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {"hospital": HOSPITAL_NAME, "clinician": DEFAULT_CLINICIAN, "provider": agent.LLM_PROVIDER, "model": agent.LLM_MODEL}


@app.get("/api/patients")
async def patients(q: str = ""):
    return json.loads(await agent.call_tool("search_patients", {"name": q, "limit": 25}))


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{STT_URL}/v1/audio/transcriptions",
            files={"file": (file.filename or "audio.wav", await file.read(), file.content_type or "audio/wav")},
            data={"model": "whisper"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"speech-to-text failed: {resp.text[:300]}")
    return resp.json()


@app.post("/api/notes/draft")
async def draft(req: DraftRequest):
    """Stream agent progress as server-sent events, then the finished draft."""
    actor = req.clinician or DEFAULT_CLINICIAN
    queue: asyncio.Queue = asyncio.Queue()

    async def on_tool_call(entry: dict):
        await db.audit(actor, "chart_read", req.patient_id, {"tool": entry["tool"], "arguments": entry["arguments"]})
        await queue.put(("tool_call", entry))

    async def run():
        try:
            note, calls = await agent.draft_note(req.patient_id, req.transcript, on_tool_call)
            draft_id = await db.create_draft(req.patient_id, req.transcript, note, calls)
            await db.audit(actor, "draft_created", req.patient_id, {"draft_id": draft_id, "tool_calls": len(calls)})
            await queue.put(("done", {"draft_id": draft_id, "note": note}))
        except Exception as exc:
            await queue.put(("error", {"message": str(exc)}))

    async def events():
        task = asyncio.create_task(run())
        try:
            while True:
                kind, payload = await queue.get()
                yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
                if kind in ("done", "error"):
                    break
        finally:
            await task

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/notes/{draft_id}/approve")
async def approve(draft_id: str, req: ApproveRequest):
    existing = await db.get_draft(draft_id)
    if not existing:
        raise HTTPException(404, "draft not found")
    if existing["status"] == "signed":
        raise HTTPException(409, "draft already signed")
    clinician = req.clinician or DEFAULT_CLINICIAN
    result = json.loads(await agent.call_tool("create_clinical_note", {
        "patient_id": existing["patient_id"],
        "title": req.title,
        "markdown": req.note,
        "author": clinician,
    }))
    await db.approve_draft(draft_id, req.note, clinician, result["reference"])
    await db.audit(clinician, "note_signed", existing["patient_id"], {
        "draft_id": draft_id,
        "fhir_reference": result["reference"],
        "edited": req.note.strip() != existing["note"].strip(),
    })
    return {"draft_id": draft_id, "fhir_reference": result["reference"]}


@app.get("/api/audit")
async def audit(patient_id: str | None = None):
    return await db.list_audit(patient_id)
