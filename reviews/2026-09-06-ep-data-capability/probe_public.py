#!/usr/bin/env python3
"""Check direct public-source access without provider or personal credentials."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).parent / "evidence"
SOURCES = {
    "sec_submissions_gtlb": "https://data.sec.gov/submissions/CIK0001653482.json",
    "sec_companyfacts_gtlb": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001653482.json",
    "sec_8k_index_gtlb": "https://www.sec.gov/Archives/edgar/data/1653482/000162828026059820/index.json",
    "issuer_release_gtlb": "https://ir.gitlab.com/news/news-details/2026/GitLab-Reports-Second-Quarter-Fiscal-Year-2027-Financial-Results/default.aspx",
    "globenewswire_release_nyax": "https://www.globenewswire.com/news-release/2026/08/25/3350268/0/en/nayax-enters-into-definitive-agreement-to-acquire-ips-group-a-leading-smart-parking-technology-provider.html",
    "prnewswire_release_veev": "https://www.prnewswire.com/news-releases/veeva-announces-fiscal-2027-second-quarter-results-302860933.html",
    "businesswire_release_dell": "https://www.businesswire.com/news/home/20260901574850/en/Dell-Technologies-Delivers-Second-Quarter-Fiscal-2027-Financial-Results/",
}
EXPECTED_TOKENS = {
    "issuer_release_gtlb": ("286.3", "117%", "Second Quarter Fiscal Year 2027"),
    "globenewswire_release_nyax": ("350", "IPS Group", "2026"),
    "prnewswire_release_veev": ("928", "2.35", "Fiscal 2027"),
    "businesswire_release_dell": ("47.0", "7.04", "192.0"),
}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        target = ROOT / (name + ".json")
        if target.exists():
            print("CACHED " + name)
            continue
        record = {"name": name, "url": url, "requested_at_utc": datetime.now(timezone.utc).isoformat()}
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "QuantResearch/0.1 EP-capability-audit"})
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(20_000_001)
                record.update(http_status=response.status, response_bytes=len(raw),
                              content_type=response.headers.get("Content-Type"),
                              response_sha256=hashlib.sha256(raw).hexdigest())
                if len(raw) > 20_000_000:
                    raise ValueError("response exceeds size bound")
                if "json" in (response.headers.get("Content-Type") or ""):
                    value = json.loads(raw)
                    record["fields"] = sorted(value)
                    if "directory" in value:
                        record["directory_items"] = value["directory"].get("item", [])
                    if "filings" in value:
                        recent = value["filings"]["recent"]
                        record["recent_fields"] = sorted(recent)
                        record["relevant_filings"] = [
                            {field: recent[field][i] for field in ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument")}
                            for i, day in enumerate(recent["filingDate"]) if "2026-08-24" <= day <= "2026-09-06"
                        ]
                    if "facts" in value:
                        record["taxonomies"] = {key: len(items) for key, items in value["facts"].items()}
                        record["custom_kpi_keys"] = [key for items in value["facts"].values() for key in items
                                                     if any(token in key.lower() for token in ("retention", "annualrecurring", "booktobill"))]
                else:
                    text = raw.decode("utf-8", errors="replace")
                    record["expected_facts_present_in_html"] = {token: token in text for token in EXPECTED_TOKENS.get(name, ())}
                record["status"] = "OK"
        except urllib.error.HTTPError as exc:
            record.update(status="HTTP_ERROR", http_status=exc.code,
                          error_excerpt=exc.read(800).decode("utf-8", errors="replace"))
        except Exception as exc:
            record.update(status="TRANSPORT_OR_PARSE_ERROR", error_type=type(exc).__name__, error_message=str(exc))
        record["elapsed_seconds"] = round(time.monotonic() - started, 4)
        target.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
