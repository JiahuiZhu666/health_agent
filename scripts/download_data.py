#!/usr/bin/env python3
"""Download NHIS 2024 public-use adult and all ten income imputations."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nhis_agent.data import download
if __name__ == "__main__":
    print("Downloading and validating the official CDC NHIS 2024 adult file and all 10 income imputations. Only real data are used.", flush=True)
    result = download(ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ready") and result.get("income_ready") else 1)
