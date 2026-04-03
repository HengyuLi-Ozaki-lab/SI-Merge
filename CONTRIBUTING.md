# Contributing to SI Merge

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/SI-Merge.git
cd SI-Merge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### System dependencies

For the DOCX → PDF conversion (WeasyPrint) to work locally, you need Cairo and Pango:

```bash
# macOS
brew install cairo pango gdk-pixbuf libffi

# Ubuntu / Debian
sudo apt install libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0

# Or just use Docker — all dependencies are bundled:
docker compose up --build
```

For OCR fallback (optional):
```bash
brew install tesseract        # macOS
sudo apt install tesseract-ocr  # Linux
```

## Project Structure

```
SI-Merge/
├── si_merge.py         # Core library: DOI extraction, SI discovery,
│                       #   download, conversion, merge, cross-linking
├── app.py              # FastAPI web app & REST API
├── static/
│   └── index.html      # Single-page frontend (vanilla HTML/CSS/JS)
├── requirements.txt    # Python dependencies
├── Dockerfile          # Production container image
├── docker-compose.yml  # Local Docker setup
├── fly.toml            # Fly.io deployment config
├── README.md           # User-facing documentation
└── CONTRIBUTING.md     # This file
```

## How It Works

The merge pipeline has 6 steps:

1. **DOI Extraction** (`extract_doi`) — reads PDF metadata (`/doi` XMP field), then falls back to regex on first-page text.
2. **Article URL Resolution** (`resolve_article_url`) — resolves DOI via `doi.org` redirect, with CrossRef API fallback.
3. **SI Discovery** (`find_si_links`) — fetches the article page and applies publisher-specific scrapers to locate SI file URLs.
4. **SI Download** (`download_si_files`) — downloads each SI file, validates content type, converts non-PDF formats.
5. **Text Extraction** (`_extract_text_blocks`) — extracts text with positions from both article and SI PDFs. Falls back to OCR if needed.
6. **Merge & Link** (`_merge_and_link`) — appends SI pages, creates forward/back links with colored annotations, and builds a TOC bookmark tree.

### Publisher-Specific Scrapers

Each publisher has quirks. The scrapers are in `si_merge.py`:

| Function | Publishers | Strategy |
|----------|-----------|----------|
| `_scrape_nature` | Springer Nature | Look for `data-test="supplementary-info"` sections |
| `_scrape_acs` | ACS Publications | Scan for `/doi/suppl/` link patterns |
| `_scrape_wiley` | Wiley Online Library | Find `supportingInformation` section, handle DOCX |
| `_scrape_elsevier` | Elsevier / ScienceDirect | Follow meta-refresh redirects, probe CDN for `mmc*.pdf` |
| `_scrape_pnas_science` | PNAS, Science/AAAS | Look for `/doi/suppl/.../suppl_file/` patterns |
| `_scrape_generic` | Everything else | Keyword-based link scanning with broad heuristics |

## Adding Support for a New Publisher

1. Identify the publisher's HTML structure for the SI section (inspect the article page).
2. Create a `_scrape_<publisher>` function in `si_merge.py` following the existing pattern.
3. Add domain detection in `find_si_links` to route to your scraper.
4. Test with 2–3 articles from that publisher.

## Running the Web App Locally

```bash
source venv/bin/activate
uvicorn app:app --reload --port 8000
# Open http://localhost:8000
```

## Code Style

- Python 3.10+ (type hints, `X | Y` union syntax)
- No external linter config — just keep it clean and consistent with existing code
- Prefer descriptive variable names over comments
- Comments should explain *why*, not *what*

## Submitting Changes

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-change`
3. Make your changes
4. Test locally with a few articles from different publishers
5. Submit a Pull Request with a clear description of the change
