#!/usr/bin/env python3
"""
SI Merge MCP Server — Exposes SI merging tools via the Model Context Protocol.

Run standalone:   python mcp_server.py
MCP Inspector:    fastmcp dev mcp_server.py
"""

import logging
import os
import tempfile
from pathlib import Path

import fitz
from fastmcp import FastMCP

import si_merge

log = logging.getLogger("si-merge-mcp")

mcp = FastMCP(
    "SI Merge",
    instructions=(
        "Tools for merging Supplementary Information (SI) into journal article PDFs. "
        "Discovers SI files from publisher websites, downloads them, and creates a "
        "merged PDF with bidirectional cross-reference links."
    ),
)


def _progress_logger(step: int, status: str, detail: str) -> None:
    log.info("[step %d] %s — %s", step, status, detail)


def _result_to_dict(r: si_merge.MergeResult) -> dict:
    return {
        "output_path": r.output_path,
        "doi": r.doi,
        "title": r.title,
        "article_pages": r.article_pages,
        "si_pages": r.si_pages,
        "forward_links": r.forward_links,
        "back_links": r.back_links,
        "si_files_found": r.si_files_found,
    }


@mcp.tool()
def merge_si(
    pdf_path: str,
    output_path: str | None = None,
    doi: str | None = None,
    si_urls: list[str] | None = None,
    si_files: list[str] | None = None,
) -> dict:
    """Merge Supplementary Information into a journal article PDF.

    Automatically discovers and downloads SI from the publisher website,
    then produces a merged PDF with bidirectional cross-reference links
    between the main text and SI content.

    Args:
        pdf_path: Absolute path to the article PDF file.
        output_path: Where to write the merged PDF. Defaults to <name>_with_SI.pdf
                     next to the original.
        doi: Article DOI. Auto-extracted from the PDF if omitted.
        si_urls: Direct SI download URLs (skips automatic discovery).
        si_files: Local SI file paths (PDF/DOCX) to use directly.

    Returns:
        A dict with output_path, doi, title, page counts, and link counts.
    """
    pdf_path = os.path.abspath(pdf_path)
    result = si_merge.run_merge(
        pdf_path=pdf_path,
        output_path=output_path,
        doi_override=doi,
        on_progress=_progress_logger,
        si_urls=si_urls,
        si_files_local=si_files,
    )
    return _result_to_dict(result)


@mcp.tool()
def find_si(doi: str) -> list[dict]:
    """Discover Supplementary Information files for a given DOI.

    Resolves the DOI to the publisher landing page, scrapes it for
    SI download links, and returns a list of discovered files.

    Args:
        doi: The article DOI (e.g. "10.1038/s41586-024-07472-3").

    Returns:
        A list of dicts, each with 'url' and 'label' keys.
    """
    article_url = si_merge.resolve_article_url(doi)
    if not article_url:
        return [{"error": f"Could not resolve DOI {doi} to an article URL."}]

    si_files = si_merge.find_si_links(article_url, on_progress=_progress_logger)
    return [{"url": sf.url, "label": sf.label} for sf in si_files]


@mcp.tool()
def extract_doi(pdf_path: str) -> dict:
    """Extract the DOI from a PDF file.

    Checks PDF metadata, hyperlink annotations, and text content
    (including the last pages where some publishers place the DOI).

    Args:
        pdf_path: Absolute path to the PDF file.

    Returns:
        A dict with 'doi' (string or null) and 'title' from metadata.
    """
    pdf_path = os.path.abspath(pdf_path)
    doc = fitz.open(pdf_path)
    doi = si_merge.extract_doi(doc)
    title = (doc.metadata or {}).get("title", "")
    doc.close()
    return {"doi": doi, "title": title}


@mcp.tool()
def download_and_merge_by_doi(
    doi: str,
    output_dir: str | None = None,
) -> dict:
    """Download an article PDF by DOI and merge its SI — fully automated.

    Downloads the article from the publisher, discovers and downloads the
    SI files, then produces a merged PDF with cross-reference links.

    Args:
        doi: The article DOI (e.g. "10.1038/s41586-024-07472-3").
        output_dir: Directory for the output file. Defaults to a temp directory.

    Returns:
        A dict with output_path, doi, title, page counts, and link counts.
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="si_merge_mcp_")
    else:
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

    work_dir = tempfile.mkdtemp(prefix="si_merge_dl_")
    _progress_logger(0, "started", f"Downloading article for DOI: {doi}")

    pdf_path, article_url = si_merge.download_article_pdf(
        doi, work_dir, on_progress=_progress_logger
    )

    output_path = str(Path(output_dir) / f"{doi.replace('/', '_')}_with_SI.pdf")

    result = si_merge.run_merge(
        pdf_path=pdf_path,
        output_path=output_path,
        doi_override=doi,
        on_progress=_progress_logger,
    )
    return _result_to_dict(result)


if __name__ == "__main__":
    mcp.run()
