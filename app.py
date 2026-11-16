"""OffCampus Engine web app. Run:  .venv\\Scripts\\python -m uvicorn app:app --port 8000"""
from dotenv import load_dotenv

load_dotenv()  # must run before the modules below read the environment

import io
import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pypdf import PdfReader

import db
import llm
import pipeline
from sources import SOURCES

app = FastAPI()
ROOT = Path(__file__).parent
EDITABLE = {"to_name", "to_title", "to_email", "to_conf", "subject", "body", "status", "role"}


def lead_or_404(lead_id):
    rows = db.leads("id=?", (lead_id,))
    if not rows:
        raise HTTPException(404, "Lead not found")
    return rows[0]


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def get_state():
    return {
        "keys": {"groq": bool(os.getenv("GROQ_API_KEY")),
                 "gmail": bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD")),
                 "apollo": bool(os.getenv("APOLLO_API_KEY")), "search": bool(os.getenv("SERPER_API_KEY"))},
        "resume": db.get_setting("resume_name"),
        "profile": db.get_setting("profile"),
        "prefs": pipeline.prefs(),
        "run": pipeline.state,
        "usage": [{"model": m, "used": llm.used_today(m), "cap": llm.DAILY_TOKENS} for m in (llm.SMART, llm.FAST)],
        "counts": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM leads GROUP BY status")},
        "sources": list(SOURCES),
    }


@app.post("/api/resume")
def upload_resume(file: UploadFile):
    data = file.file.read()
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        raise HTTPException(400, "Could not read that file as a PDF")
    if len(text.strip()) < 200:
        raise HTTPException(400, "No text found in the PDF (is it a scanned image?)")
    pipeline.RESUME_PATH.write_bytes(data)
    db.set_setting("resume_name", file.filename)
    try:
        return pipeline.build_profile(text)
    except Exception as e:
        raise HTTPException(502, f"Resume saved, but building the profile failed: {e}")


@app.put("/api/profile")
def save_profile(profile: dict = Body(...)):
    db.set_setting("profile", profile)


@app.put("/api/prefs")
def save_prefs(prefs: dict = Body(...)):
    db.set_setting("prefs", {"threshold": int(prefs["threshold"]), "per_source": int(prefs["per_source"]),
                             "notes": str(prefs.get("notes", ""))})


@app.post("/api/run")
def run(body: dict = Body(...)):
    try:
        if not pipeline.start(body.get("stages", []), [s for s in body.get("sources", []) if s in SOURCES]):
            raise HTTPException(409, "A run is already in progress")
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/leads")
def list_leads():
    return db.leads()


@app.patch("/api/leads/{lead_id}")
def edit_lead(lead_id: int, fields: dict = Body(...)):
    lead_or_404(lead_id)
    db.update_lead(lead_id, **{k: v for k, v in fields.items() if k in EDITABLE})
    return lead_or_404(lead_id)


@app.post("/api/leads/{lead_id}/draft")
def redraft(lead_id: int):
    lead = lead_or_404(lead_id)
    if not lead["to_email"]:
        raise HTTPException(400, "Add a recipient email first")
    try:
        pipeline.write_draft(lead, db.get_setting("profile") or {})
    except Exception as e:
        raise HTTPException(502, str(e))
    return lead_or_404(lead_id)


@app.post("/api/leads/{lead_id}/push")
def push(lead_id: int):
    lead = lead_or_404(lead_id)
    if not (lead["to_email"] and lead["subject"] and lead["body"]):
        raise HTTPException(400, "This lead needs a recipient, subject and body first")
    try:
        pipeline.push_one(lead)
    except Exception as e:
        raise HTTPException(502, str(e))
    return lead_or_404(lead_id)
