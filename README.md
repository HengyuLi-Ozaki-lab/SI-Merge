# SI Merge

> Automatically find, download, and merge Supplementary Information (SI) into journal article PDFs — with bidirectional cross-reference links.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](Dockerfile)

## The Problem

Reading journal articles means constantly switching between the main paper and its Supplementary Information — separate files, separate windows, no linking. When the paper says "see Figure S3", you have to manually open the SI PDF and scroll to find it.

## The Solution

SI Merge takes a journal article PDF and:

1. **Extracts the DOI** from PDF metadata or text
2. **Finds SI files** by scraping the publisher's article page
3. **Downloads** the supplementary file(s) (PDF, DOCX, DOC)
4. **Converts** non-PDF formats using pure-Python tools (no MS Office needed)
5. **Merges** SI after the main article into a single PDF
6. **Creates bidirectional cross-reference links**:
   - **Forward links** (blue underline): click "Figure S1" in the article → jump to the figure in SI
   - **Back links** (orange underline): click "Supplementary Figure 1" in the SI → jump back to where it was cited
7. **Generates a Table of Contents** with navigable bookmarks for all SI figures and tables

The result is a single, self-contained PDF where every SI reference is a clickable link.

## Supported Publishers

| Publisher | SI Formats | Status |
|-----------|-----------|--------|
| **Springer Nature** (Nature, Nat. Commun., Nat. Catal., ...) | PDF | Fully automatic |
| **ACS Publications** (JACS, ACS Catal., ACS Nano, ...) | PDF | Fully automatic |
| **Wiley** (Angew. Chem., Adv. Mater., Adv. Energy Mater., ...) | PDF, DOCX | Fully automatic |
| **Elsevier** (Joule, Cell, Matter, ...) | PDF | Fully automatic |
| **AAAS** (Science, Science Advances) | PDF | Fully automatic |
| **PNAS** | PDF | Fully automatic |
| **RSC** (EES, JMCA, Chem. Sci., ...) | PDF | Fully automatic |
| **APS** (PRL, PRB, PRX, ...) | PDF | Manual SI required (aggressive Cloudflare) |
| **Taylor & Francis**, other Atypon-based | PDF | Generic scraper fallback |

## Quick Start

### Web App

```bash
# With Docker (recommended)
docker compose up --build
# Open http://localhost:8000

# Without Docker
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8000
```

### Command Line

```bash
# Basic usage — auto-detect DOI, find SI, merge
python si_merge.py article.pdf

# Specify output path
python si_merge.py article.pdf -o merged.pdf

# Override DOI if auto-detection fails
python si_merge.py article.pdf --doi 10.1038/s41467-023-44674-1

# Provide SI URL manually (when auto-discovery is blocked)
python si_merge.py article.pdf --si-url https://example.com/si.pdf

# Provide local SI file (when even the URL download is blocked)
python si_merge.py article.pdf --si-file ~/Downloads/supplemental.pdf

# Batch processing
python si_merge.py paper1.pdf paper2.pdf paper3.pdf
python si_merge.py ./papers/             # entire directory
python si_merge.py ./papers/ -o ./merged/
```

### Python Library

```python
from si_merge import run_merge, run_batch_merge

result = run_merge(
    pdf_path="article.pdf",
    on_progress=lambda step, status, detail: print(f"[{step}] {status}: {detail}")
)
print(f"Output: {result.output_path}")
print(f"Links: {result.forward_links} forward, {result.back_links} back")

# Batch
batch = run_batch_merge(["paper1.pdf", "paper2.pdf"], output_dir="./merged/")
print(f"{batch.succeeded}/{batch.total} succeeded")
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Web Browser                          │
│           (drag & drop upload, real-time progress)       │
└──────────────┬───────────────────────────┬───────────────┘
               │ Upload PDF                │ SSE events
               ▼                           ▲
┌──────────────────────────────────────────────────────────┐
│                    FastAPI (app.py)                       │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───────────┐  │
│  │ /api/    │ │ /api/     │ │ /api/    │ │ /api/     │  │
│  │ tasks    │ │ batch     │ │ merge    │ │ health    │  │
│  └────┬─────┘ └─────┬─────┘ └────┬─────┘ └───────────┘  │
│       │             │            │                        │
│       ▼             ▼            ▼                        │
│  ┌─────────────────────────────────────┐                 │
│  │       TaskStore (in-memory)         │                 │
│  │  task state, events, file paths     │                 │
│  └─────────────────────────────────────┘                 │
└──────────────────────┬───────────────────────────────────┘
                       │ Background Thread
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  si_merge.py (core)                       │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │ 1. Extract │  │ 2. Find SI   │  │ 3. Download SI  │  │
│  │    DOI     │──▶  (scrape     │──▶  (curl_cffi /   │  │
│  │ (PyMuPDF)  │  │   publisher) │  │   requests)     │  │
│  └────────────┘  └──────────────┘  └────────┬────────┘  │
│                                              │           │
│  ┌────────────┐  ┌──────────────┐  ┌────────▼────────┐  │
│  │ 6. Merge   │  │ 5. Analyze   │  │ 4. Extract      │  │
│  │  & Link    │◀──  references  │◀──    text         │  │
│  │ (PyMuPDF)  │  │   (regex)    │  │ (PyMuPDF/OCR)   │  │
│  └────────────┘  └──────────────┘  └─────────────────┘  │
│                                                          │
│  Publisher Scrapers:                                     │
│  Nature │ ACS │ Wiley │ Elsevier │ PNAS │ Science │ RSC │
└──────────────────────────────────────────────────────────┘
```

## Web App Features

- **Drag & drop** single or multiple PDFs
- **Real-time progress** via Server-Sent Events (6-step pipeline visualization)
- **Batch processing** with per-file progress and results
- **Manual SI options** — paste SI URLs or upload SI files when auto-discovery fails
- **Contextual error guidance** — when a publisher blocks access, the UI explains how to provide SI manually
- **REST API** with Swagger docs at `/docs`

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/tasks` | Create async merge task (single file) |
| `POST` | `/api/batch` | Create async batch merge task |
| `GET` | `/api/tasks/{id}` | Get task status |
| `GET` | `/api/tasks/{id}/events` | SSE stream of progress events |
| `GET` | `/api/tasks/{id}/download` | Download merged PDF |
| `GET` | `/api/tasks/{id}/download/{idx}` | Download file from batch |
| `POST` | `/api/merge` | Synchronous merge (upload → merged PDF) |

Full interactive docs: `http://localhost:8000/docs`

## Deployment

The app ships as a Docker container deployable to any platform with Docker support.

| Platform | Setup | Free Tier |
|----------|-------|-----------|
| [**Render**](https://render.com) | Connect GitHub repo → auto-detect Dockerfile → deploy | 750h/month |
| [**Railway**](https://railway.app) | Connect GitHub repo → auto-deploy | $5/month credit |
| [**Fly.io**](https://fly.io) | `fly launch && fly deploy` (uses included `fly.toml`) | 3 shared VMs |
| [**Google Cloud Run**](https://cloud.google.com/run) | Build trigger from GitHub | 2M req/month |

> **Note:** Cloudflare Pages/Workers is not supported — this project requires a full Python runtime with native C libraries (Cairo, Pango, MuPDF), file system access, and long-running background tasks.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SI_MERGE_MAX_UPLOAD_MB` | `50` | Maximum upload file size |
| `SI_MERGE_TASK_TTL` | `3600` | Seconds before completed tasks are cleaned up |

See the [Deployment section in CONTRIBUTING.md](CONTRIBUTING.md) for detailed instructions.

## Technical Details

### Text Extraction Strategy

SI references like "Figure S3" must be located precisely in the PDF. The tool uses a cascading approach:

1. **Direct extraction** via PyMuPDF — works for most well-formed PDFs
2. **Re-download** from the publisher — fixes corrupted or truncated local copies
3. **OCR fallback** — renders pages to images and runs Tesseract (for scanned PDFs)

### Document Format Conversion

Non-PDF SI files (DOCX, DOC) are converted automatically:

1. **mammoth + WeasyPrint** — pure Python, no external software. Primary strategy for DOCX files.
2. **LibreOffice headless** — fallback for DOC files or when the above fails.

### Cross-Reference Linking

The tool uses regex pattern matching to find SI references in the article text (e.g., "Figure S1", "Table S2", "Supplementary Note 3") and corresponding anchors in the SI text. Links are created as PDF annotations with colored underlines (blue for forward, orange for back) and organized into a bookmark tree.

### HTTP Client

Publisher websites often employ anti-bot measures. The tool uses:
- **curl_cffi** with browser-like TLS fingerprinting to bypass Cloudflare challenges
- Persistent sessions with cookies and Referer headers
- Publisher-specific scraping strategies

## Installation

### Requirements

- Python 3.10+
- System libraries for WeasyPrint: Cairo, Pango, GDK-Pixbuf (bundled in Docker image)
- (Optional) Tesseract OCR for scanned PDFs
- (Optional) LibreOffice for DOC format conversion

### Local Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# macOS system dependencies
brew install cairo pango gdk-pixbuf libffi
brew install tesseract    # optional, for OCR

# Or use Docker (all dependencies included)
docker compose up --build
```

### Docker

```bash
docker compose up --build
# App available at http://localhost:8000
```

## License

[MIT](LICENSE)
