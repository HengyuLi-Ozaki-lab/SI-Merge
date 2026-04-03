"""
SI Merge — Web Application & REST API

Run locally:
    uvicorn app:app --reload --port 8000

Run with Docker:
    docker compose up --build
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from pydantic import BaseModel

from si_merge import MergeResult, download_article_pdf, run_batch_merge, run_merge

logger = logging.getLogger("si_merge")

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------

MAX_UPLOAD_MB = int(os.getenv("SI_MERGE_MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
TASK_TTL_SECONDS = int(os.getenv("SI_MERGE_TASK_TTL", "3600"))

STATIC_DIR = Path(__file__).parent / "static"
TASKS_DIR = Path(tempfile.gettempdir()) / "si_merge_tasks"
TASKS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Periodic cleanup of expired tasks
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            store.cleanup_old(TASK_TTL_SECONDS)
        except Exception:
            logger.exception("Cleanup error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SI Merge API",
    description="Automatically merge Supplementary Information into journal article PDFs",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Task state management
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStore:
    """In-memory store for merge tasks (swap for Redis/DB for production)."""

    def __init__(self):
        self._tasks: dict[str, dict] = {}

    def create(self, task_id: str, filename: str) -> dict:
        task = {
            "id": task_id,
            "status": TaskStatus.PENDING,
            "filename": filename,
            "events": [],
            "result": None,
            "error": None,
            "created_at": time.time(),
            "work_dir": str(TASKS_DIR / task_id),
        }
        self._tasks[task_id] = task
        os.makedirs(task["work_dir"], exist_ok=True)
        return task

    def get(self, task_id: str) -> dict | None:
        return self._tasks.get(task_id)

    def push_event(self, task_id: str, step: int, status: str, detail: str):
        task = self._tasks.get(task_id)
        if task:
            event = {"step": step, "status": status, "detail": detail, "ts": time.time()}
            task["events"].append(event)

    def set_complete(self, task_id: str, result: MergeResult):
        task = self._tasks.get(task_id)
        if task:
            task["status"] = TaskStatus.COMPLETED
            task["result"] = {
                "output_path": result.output_path,
                "doi": result.doi,
                "title": result.title,
                "article_pages": result.article_pages,
                "si_pages": result.si_pages,
                "forward_links": result.forward_links,
                "back_links": result.back_links,
                "si_files_found": result.si_files_found,
            }

    def set_failed(self, task_id: str, error: str):
        task = self._tasks.get(task_id)
        if task:
            task["status"] = TaskStatus.FAILED
            task["error"] = error

    def cleanup_old(self, max_age_seconds: int = 3600):
        now = time.time()
        expired = [tid for tid, t in self._tasks.items() if now - t["created_at"] > max_age_seconds]
        for tid in expired:
            task = self._tasks.pop(tid, None)
            if task:
                shutil.rmtree(task["work_dir"], ignore_errors=True)


store = TaskStore()


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------

def _process_task(task_id: str, pdf_path: str, doi: str | None, si_filter: str,
                   si_urls: list[str] | None = None,
                   si_files_local: list[str] | None = None):
    """Run the merge workflow in a background thread."""
    task = store.get(task_id)
    if not task:
        return

    task["status"] = TaskStatus.RUNNING
    work_dir = task["work_dir"]
    output_path = os.path.join(work_dir, "merged_output.pdf")

    def on_progress(step: int, status: str, detail: str = ""):
        store.push_event(task_id, step, status, detail)

    try:
        result = run_merge(
            pdf_path=pdf_path,
            output_path=output_path,
            doi_override=doi or None,
            si_filter=si_filter,
            on_progress=on_progress,
            si_urls=si_urls or None,
            si_files_local=si_files_local or None,
        )
        store.set_complete(task_id, result)
    except Exception as e:
        store.set_failed(task_id, str(e))
        store.push_event(task_id, 0, "error", str(e))


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "max_upload_mb": MAX_UPLOAD_MB}


@app.post("/api/tasks")
async def create_task(
    file: UploadFile = File(...),
    doi: str = Form(""),
    si_filter: str = Form("supplementary information"),
    si_urls: str = Form(""),
    si_files: list[UploadFile] = File(default=[]),
):
    """Upload a PDF and start a merge task. Returns a task_id for tracking progress.

    si_urls: newline- or comma-separated list of direct SI download URLs.
    si_files: uploaded SI files (PDF/DOCX). Use when the publisher blocks automated downloads.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    task_id = uuid.uuid4().hex[:12]
    task = store.create(task_id, file.filename)

    pdf_path = os.path.join(task["work_dir"], "input.pdf")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Maximum upload size is {MAX_UPLOAD_MB} MB.")
    with open(pdf_path, "wb") as f:
        f.write(content)

    parsed_urls = [u.strip() for u in si_urls.replace(",", "\n").split("\n") if u.strip()] or None

    # Save uploaded SI files
    local_si_paths = None
    real_si_files = [sf for sf in si_files if sf.filename and sf.size and sf.size > 0]
    if real_si_files:
        local_si_paths = []
        for i, sf in enumerate(real_si_files):
            ext = Path(sf.filename).suffix or ".pdf"
            si_path = os.path.join(task["work_dir"], f"si_upload_{i}{ext}")
            si_content = await sf.read()
            with open(si_path, "wb") as fh:
                fh.write(si_content)
            local_si_paths.append(si_path)

    thread = Thread(
        target=_process_task,
        args=(task_id, pdf_path, doi, si_filter, parsed_urls, local_si_paths),
        daemon=True,
    )
    thread.start()

    return {"task_id": task_id, "status": "pending"}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """Get the current status of a merge task."""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {
        "id": task["id"],
        "status": task["status"],
        "filename": task["filename"],
        "result": task["result"],
        "error": task["error"],
    }


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str):
    """SSE endpoint: stream progress events for a task in real time."""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    async def event_generator():
        last_idx = 0
        while True:
            task = store.get(task_id)
            if not task:
                break

            events = task["events"]
            while last_idx < len(events):
                yield {"event": "progress", "data": json.dumps(events[last_idx])}
                last_idx += 1

            if task["status"] == TaskStatus.COMPLETED:
                yield {"event": "complete", "data": json.dumps(task["result"])}
                break
            elif task["status"] == TaskStatus.FAILED:
                yield {"event": "error", "data": json.dumps({"error": task["error"]})}
                break

            await asyncio.sleep(0.3)

    return EventSourceResponse(event_generator())


@app.get("/api/tasks/{task_id}/download")
async def download_result(task_id: str):
    """Download the merged PDF for a completed task."""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] != TaskStatus.COMPLETED:
        raise HTTPException(400, f"Task is not complete (status: {task['status']})")

    output_path = task["result"]["output_path"]
    if not os.path.isfile(output_path):
        raise HTTPException(500, "Output file not found")

    original_stem = Path(task["filename"]).stem
    download_name = f"{original_stem}_with_SI.pdf"

    return FileResponse(output_path, media_type="application/pdf", filename=download_name)


@app.post("/api/batch")
async def create_batch_task(
    files: list[UploadFile] = File(...),
    si_filter: str = Form("supplementary information"),
):
    """
    Upload multiple PDFs and start a batch merge task.
    Returns a task_id with type='batch' for tracking overall progress.
    """
    pdf_files = [f for f in files if f.filename and f.filename.lower().endswith(".pdf")]
    if not pdf_files:
        raise HTTPException(400, "Please upload at least one PDF file.")

    task_id = uuid.uuid4().hex[:12]
    task = store.create(task_id, f"batch ({len(pdf_files)} files)")
    task["type"] = "batch"
    task["file_count"] = len(pdf_files)
    task["file_results"] = []

    work_dir = task["work_dir"]
    saved_paths = []
    filenames = []
    for i, f in enumerate(pdf_files):
        path = os.path.join(work_dir, f"input_{i}.pdf")
        content = await f.read()
        with open(path, "wb") as fh:
            fh.write(content)
        saved_paths.append(path)
        filenames.append(f.filename)

    task["filenames"] = filenames

    thread = Thread(
        target=_process_batch_task,
        args=(task_id, saved_paths, filenames, si_filter),
        daemon=True,
    )
    thread.start()

    return {"task_id": task_id, "status": "pending", "file_count": len(pdf_files)}


def _process_batch_task(
    task_id: str, pdf_paths: list[str], filenames: list[str], si_filter: str
):
    """Run batch merge workflow in a background thread."""
    task = store.get(task_id)
    if not task:
        return

    task["status"] = TaskStatus.RUNNING
    work_dir = task["work_dir"]
    total = len(pdf_paths)
    succeeded = 0

    for i, (pdf_path, filename) in enumerate(zip(pdf_paths, filenames)):
        store.push_event(task_id, 0, "batch_progress",
                         json.dumps({"index": i, "total": total, "filename": filename, "status": "started"}))

        output_path = os.path.join(work_dir, f"merged_{i}.pdf")

        def on_progress(step: int, status: str, detail: str = ""):
            store.push_event(task_id, step, status, f"[{i+1}/{total}] {detail}")

        try:
            result = run_merge(pdf_path, output_path, None, si_filter, on_progress)
            entry = {
                "index": i, "filename": filename, "status": "completed",
                "output_path": result.output_path,
                "doi": result.doi, "title": result.title,
                "article_pages": result.article_pages, "si_pages": result.si_pages,
                "forward_links": result.forward_links, "back_links": result.back_links,
            }
            task["file_results"].append(entry)
            succeeded += 1
            store.push_event(task_id, 0, "batch_progress",
                             json.dumps({"index": i, "total": total, "filename": filename, "status": "completed"}))
        except Exception as e:
            entry = {"index": i, "filename": filename, "status": "failed", "error": str(e)}
            task["file_results"].append(entry)
            store.push_event(task_id, 0, "batch_progress",
                             json.dumps({"index": i, "total": total, "filename": filename, "status": f"failed: {e}"}))

    task["status"] = TaskStatus.COMPLETED
    task["result"] = {
        "total": total, "succeeded": succeeded, "failed": total - succeeded,
        "files": task["file_results"],
    }


@app.get("/api/tasks/{task_id}/download/{file_index}")
async def download_batch_result(task_id: str, file_index: int):
    """Download a specific merged PDF from a batch task."""
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] != TaskStatus.COMPLETED:
        raise HTTPException(400, f"Task is not complete (status: {task['status']})")

    file_results = task.get("file_results", [])
    match = next((r for r in file_results if r["index"] == file_index), None)
    if not match or match.get("status") != "completed":
        raise HTTPException(404, "File not found or processing failed")

    output_path = match["output_path"]
    if not os.path.isfile(output_path):
        raise HTTPException(500, "Output file not found")

    stem = Path(match["filename"]).stem
    return FileResponse(output_path, media_type="application/pdf", filename=f"{stem}_with_SI.pdf")


@app.post("/api/merge")
async def merge_sync(
    file: UploadFile = File(...),
    doi: str = Form(""),
    si_filter: str = Form("supplementary information"),
):
    """
    Synchronous merge API: upload PDF, wait for processing, return merged PDF.
    Best for programmatic/scripting use.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    work_dir = tempfile.mkdtemp(prefix="si_merge_api_")
    pdf_path = os.path.join(work_dir, "input.pdf")
    output_path = os.path.join(work_dir, "merged_output.pdf")

    content = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(content)

    try:
        result = await asyncio.to_thread(
            run_merge, pdf_path, output_path, doi or None, si_filter,
        )
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(422, str(e))

    original_stem = Path(file.filename).stem
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=f"{original_stem}_with_SI.pdf",
        background=None,
    )


# ---------------------------------------------------------------------------
# DOI-based merge (for browser extension)
# ---------------------------------------------------------------------------

class MergeByDoiRequest(BaseModel):
    doi: str
    si_urls: list[str] | None = None


def _process_doi_task(task_id: str, doi: str, si_urls: list[str] | None):
    """Download article PDF by DOI, then run the merge workflow."""
    task = store.get(task_id)
    if not task:
        return

    task["status"] = TaskStatus.RUNNING
    work_dir = task["work_dir"]
    output_path = os.path.join(work_dir, "merged_output.pdf")

    def on_progress(step: int, status: str, detail: str = ""):
        store.push_event(task_id, step, status, detail)

    try:
        pdf_path, article_url = download_article_pdf(doi, work_dir, on_progress)
        result = run_merge(
            pdf_path=pdf_path,
            output_path=output_path,
            doi_override=doi,
            on_progress=on_progress,
            si_urls=si_urls or None,
        )
        store.set_complete(task_id, result)
    except Exception as e:
        store.set_failed(task_id, str(e))
        store.push_event(task_id, 0, "error", str(e))


@app.post("/api/merge-by-doi")
async def merge_by_doi(body: MergeByDoiRequest):
    """
    Start a merge task given a DOI. The backend downloads the article PDF,
    finds SI, and merges. Returns a task_id for tracking progress via SSE.

    Designed for the browser extension where the user doesn't upload a file.
    """
    doi = body.doi.strip()
    if not doi:
        raise HTTPException(400, "DOI is required.")

    task_id = uuid.uuid4().hex[:12]
    task = store.create(task_id, f"doi:{doi}")

    thread = Thread(
        target=_process_doi_task,
        args=(task_id, doi, body.si_urls),
        daemon=True,
    )
    thread.start()

    return {"task_id": task_id, "status": "pending", "doi": doi}
