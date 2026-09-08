#!/usr/bin/env python3
"""Bounded official attachment audit; no FMP content requests, LLM, SG or delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.data.public_articles import PublicArticleClient
from src.data.sec_attachments import SecAttachmentClient

PDF_URL = "https://investors.affirm.com/static-files/f853cf71-ea63-4fbb-a1e8-6429beba46d0"
IR_INDEX = "https://investors.affirm.com/financial-information/quarterly-results"
SEC = [
    ("AFRM_8K", "https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/afrm-20260825.htm"),
    ("AFRM_EX99_1", "https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/affirmfq426shareholderle.htm"),
    ("PLAB_EX99_1", "https://www.sec.gov/Archives/edgar/data/810136/000081013626000006/plabQ32026EarningsEx99-1PR.htm"),
    ("ANF_EX99_1", "https://www.sec.gov/Archives/edgar/data/1018840/000101884026000041/q22026pressrelease.htm"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.archive_dir.exists() or args.output.exists():
        parser.error("New paths required; do not overwrite prior audits")
    args.archive_dir.mkdir(parents=True, mode=0o700)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "archive_dir": str(args.archive_dir.resolve()),
              "scope": "KNOWN_EXPLICIT_ATTACHMENTS_NOT_AUTOMATIC_DISCOVERY", "llm_calls": 0,
              "fmp_requests": 0, "discord_messages": 0, "production_changes": False,
              "source_discovery": "OFFICIAL_LINKS_VERIFIED_USING_WEB_NOT_PROJECT_CRAWLER"}

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    http = PublicArticleClient(allowed_hosts={"investors.affirm.com"}, max_requests=6,
                               max_bytes=5_000_000, deadline_seconds=90, timeout_seconds=25)
    result = http.fetch_pdf(PDF_URL)
    raw = result.pop("pdf", None)
    result.update(url=PDF_URL, official_parent_url=IR_INDEX, http_requests=http.requests,
                  financial_facts_verified=False, historical_availability_verified=False,
                  ep_integration="NOT_IMPORTED_ATTACHMENT_EVIDENCE_ONLY")
    report["afrm_pdf"] = result
    save()
    if raw is not None:
        path = args.archive_dir / "afrm-fy2026-q4-shareholder-letter.pdf"
        path.write_bytes(raw)
        parsed_path = args.archive_dir / "afrm-pdf-parsed.json"
        result["raw_path"] = str(path.resolve())
        try:
            worker = subprocess.run([sys.executable, str(Path(__file__).with_name("pdf_worker.py")), str(path), str(parsed_path)],
                                    timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if worker.returncode != 0:
                raise ValueError("PDF worker failed")
            parsed = json.loads(parsed_path.read_text())
            result["parsed"] = {k: v for k, v in parsed.items() if k != "paragraphs"}
            result["keyword_pages_not_verified_facts"] = {term: sorted({p["page"] for p in parsed["paragraphs"]
                if term in p["text"].casefold()}) for term in ("revenue", "valuation allowance", "diluted", "outlook")}
            result["parsed_path"] = str(parsed_path.resolve())
        except subprocess.TimeoutExpired:
            result["parse_status"] = "PDF_WORKER_TIMEOUT"
        except Exception as exc:
            result["parse_status"] = "PDF_WORKER_FAILED"
            result["parse_error_type"] = type(exc).__name__
    save()
    print(json.dumps({"pdf_status": result["status"], "parsed": result.get("parsed", {}).get("status")}), flush=True)
    contact = os.getenv("SEC_CONTACT_EMAIL")
    report["sec"] = {"status": "CONTACT_REQUIRED", "http_requests": 0, "attachments": []}
    if contact:
        try:
            sec = SecAttachmentClient(contact_email=contact, max_requests=6, deadline_seconds=90, timeout_seconds=15)
        except ValueError:
            report["sec"]["status"] = "CONTACT_CONFIGURATION_INVALID"
        else:
            report["sec"]["status"] = "ATTEMPTED"
            for name, url in SEC:
                entry = sec.fetch(url)
                html = entry.pop("html", None)
                entry.update(name=name, url=url, financial_facts_verified=False, historical_availability_verified=False)
                if html is not None:
                    path = args.archive_dir / (name + ".html")
                    path.write_bytes(html)
                    from lxml import html as lhtml
                    root = lhtml.fromstring(html)
                    entry.update(raw_path=str(path.resolve()), image_count=len(root.xpath("//img")),
                                 text_characters=len(" ".join(root.text_content().split())),
                                 evidence_status="RAW_ATTACHMENT_ONLY_NOT_VERIFIED_TEXT")
                report["sec"]["attachments"].append(entry)
                report["sec"]["http_requests"] = sec.requests
                save()
                if entry["status"] in {"SOURCE_HTTP_401", "SOURCE_HTTP_403", "SOURCE_HTTP_429", "SOURCE_TIMEOUT"}:
                    report["sec"]["stop_reason"] = "NO_RETRY_AFTER_HOST_REFUSAL_OR_TIMEOUT"
                    break
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({"sec_status": report["sec"]["status"], "sec_requests": report["sec"]["http_requests"]}), flush=True)


if __name__ == "__main__":
    main()
