"""Download and verify public NHIS 2024 files from CDC. No sample/demo fallback."""
import csv
import hashlib
import json
import os
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE_PAGE = "https://www.cdc.gov/nchs/nhis/documentation/2024-nhis.html"
DATA_BASE = "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Datasets/NHIS/2024/"
DOC_BASE = "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Dataset_Documentation/NHIS/2024/"
SOURCES = {
    "adult": {"url": DATA_BASE + "adult24csv.zip", "file": "adult24.csv", "expected_rows": 32629,
              "columns": {"SRVY_YR", "RECTYPE", "HHX", "PHSTAT_A", "AGEP_A", "SEX_A", "EDUCP_A", "HISPALLP_A", "WTFA_A", "PSTRAT", "PPSU", "REGION"}},
    "income": {"url": DATA_BASE + "adultinc24csv.zip", "file": "adultinc24.csv", "expected_rows": 326290,
               "columns": {"RECTYPE", "HHX", "IMPNUM_A", "POVRATTC_A", "RATCAT_A"}},
}
DOCUMENTS = {"adult-codebook.pdf": DOC_BASE + "adult-codebook.pdf", "srvydesc-508.pdf": DOC_BASE + "srvydesc-508.pdf",
             "adult-summary.pdf": DOC_BASE + "adult-summary.pdf", "adultinc-summary.pdf": DOC_BASE + "adultinc-summary.pdf",
             "NHIS2024-imputation-techdoc-508.pdf": DOC_BASE + "NHIS2024-imputation-techdoc-508.pdf"}
_CACHE = {}


def _now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_csv(path, kind="adult", min_rows=1000):
    """Integrity checks, not estimates. Missing codes remain untouched in CSV."""
    if kind not in SOURCES:
        raise ValueError("Unknown NHIS data kind")
    ids = set()
    imputation_ids = {}
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []
        if len(names) != len(set(names)):
            raise ValueError("CSV contains duplicate column names")
        missing = SOURCES[kind]["columns"] - set(names)
        if missing:
            raise ValueError("NHIS CSV lacks required columns: " + ", ".join(sorted(missing)))
        for row in reader:
            count += 1
            hhx = (row.get("HHX") or "").strip()
            if not hhx or None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed CSV record or missing HHX")
            if kind == "adult":
                if row["SRVY_YR"].strip() != "2024" or row["RECTYPE"].strip() != "10":
                    raise ValueError("Expected 2024 Sample Adult record type 10")
                if hhx in ids:
                    raise ValueError("Sample Adult CSV contains duplicate HHX")
                ids.add(hhx)
                try:
                    weight = float(row["WTFA_A"])
                except ValueError:
                    raise ValueError("Invalid sample weight")
                if not 0 < weight < float("inf"):
                    raise ValueError("Nonpositive or nonfinite sample weight")
                if not row["PSTRAT"].strip() or not row["PPSU"].strip():
                    raise ValueError("Missing survey design identifier")
            else:
                if row["RECTYPE"].strip() != "30":
                    raise ValueError("Expected Sample Adult Income record type 30")
                try:
                    imp = int(row["IMPNUM_A"])
                except ValueError:
                    raise ValueError("Invalid income imputation number")
                if imp not in range(1, 11):
                    raise ValueError("Expected IMPNUM_A in 1 through 10")
                seen = imputation_ids.setdefault(hhx, set())
                if imp in seen:
                    raise ValueError("Duplicate HHX / IMPNUM_A income record")
                seen.add(imp)
    if count < min_rows:
        raise ValueError("CSV has fewer records than expected for the NHIS public file")
    if kind == "income" and any(len(v) != 10 for v in imputation_ids.values()):
        raise ValueError("Income file does not contain all 10 imputations for every HHX")
    return {"row_count": count, "columns": names, "household_count": len(ids) if kind == "adult" else len(imputation_ids),
            "sha256": sha256_file(path), "bytes": Path(path).stat().st_size,
            "imputations": 10 if kind == "income" else None}


def _download(url, dest, limit=256 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": "NHIS-Health-Agent/1.0 (+public-research)"})
    size = 0
    with urllib.request.urlopen(req, timeout=90) as response, Path(dest).open("wb") as out:
        for chunk in iter(lambda: response.read(128 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ValueError("CDC download exceeded the configured size limit")
            out.write(chunk)
    if size == 0:
        raise ValueError("CDC returned an empty file")


def _extract_csv(archive, dest, expected_name):
    with zipfile.ZipFile(archive) as z:
        matches = [m for m in z.infolist() if not m.is_dir() and Path(m.filename).name.lower() == expected_name]
        if len(matches) != 1:
            raise ValueError("CDC archive must contain exactly one " + expected_name)
        item = matches[0]
        # No archive path is ever used as a filesystem destination.
        if item.file_size > 512 * 1024 * 1024:
            raise ValueError("Uncompressed CSV exceeds the configured limit")
        with z.open(item) as src, Path(dest).open("wb") as out:
            for chunk in iter(lambda: src.read(128 * 1024), b""):
                out.write(chunk)


def _atomic_json(path, payload):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temp), str(path))


def _verified_entry(root, manifest, kind):
    entry = manifest.get("datasets", {}).get(kind, {})
    path = root / "data" / SOURCES[kind]["file"]
    if not entry or not path.is_file():
        return None, "The official data have not been downloaded."
    if entry.get("source") != SOURCES[kind]["url"] or entry.get("year") != 2024 or entry.get("row_count") != SOURCES[kind]["expected_rows"]:
        return None, "The data source metadata do not match the official release."
    stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size, entry.get("sha256"))
    if stamp in _CACHE:
        return _CACHE[stamp], None
    try:
        inspected = inspect_csv(path, kind)
    except (OSError, ValueError, csv.Error) as e:
        return None, "Data integrity check failed: " + str(e)
    if inspected["sha256"] != entry.get("sha256") or inspected["row_count"] != entry.get("row_count"):
        return None, "The data checksum or row count does not match the download record. Download the files again."
    value = dict(entry, file=str(path), ready=True)
    _CACHE[stamp] = value
    return value, None


def status(root):
    root = Path(root)
    manifest_path = root / "data" / "manifest.json"
    base = {"ready": False, "income_ready": False, "file": str(root / "data" / "adult24.csv"),
            "year": 2024, "source": SOURCE_PAGE, "row_count": None, "sha256": None,
            "message": "The official NHIS 2024 data have not been downloaded.", "datasets": {}}
    if not manifest_path.is_file():
        return base
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("Unknown manifest schema")
        adult, adult_error = _verified_entry(root, manifest, "adult")
        income, income_error = _verified_entry(root, manifest, "income")
        if adult:
            base.update(adult)
            base.update(ready=True, message="The official NHIS 2024 Sample Adult data have been downloaded and passed integrity checks.")
        else:
            base["message"] = adult_error
        base["income_ready"] = bool(income)
        base["income_message"] = "All 10 income imputations are complete." if income else income_error
        base["datasets"] = {"adult": adult, "income": income}
        base["documents"] = manifest.get("documents", {})
        base["downloaded_at"] = manifest.get("downloaded_at")
    except (OSError, ValueError, KeyError, TypeError) as e:
        base["message"] = "The data download record could not be verified: " + str(e)
    return base


def download(root):
    """Fetch real CDC files atomically. Failed downloads never become usable data."""
    root = Path(root)
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = data_dir / "manifest.json"
    manifest = {"schema_version": 1, "year": 2024, "source_page": SOURCE_PAGE,
                "downloaded_at": _now(), "datasets": {}, "documents": {}}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("schema_version") == 1:
                manifest = previous
        except (OSError, ValueError):
            pass
    errors = []
    with tempfile.TemporaryDirectory(prefix="nhis-download-", dir=str(data_dir)) as tmp:
        temp_dir = Path(tmp)
        for kind, source in SOURCES.items():
            existing, _ = _verified_entry(root, manifest, kind)
            if existing:
                continue
            try:
                archive = temp_dir / (kind + ".zip")
                dest = temp_dir / source["file"]
                _download(source["url"], archive)
                _extract_csv(archive, dest, source["file"])
                entry = inspect_csv(dest, kind)
                if entry["row_count"] != source["expected_rows"]:
                    raise ValueError("Official 2024 file row count mismatch: expected " + str(source["expected_rows"]))
                if kind == "income" and manifest["datasets"].get("adult") and entry["household_count"] != manifest["datasets"]["adult"]["household_count"]:
                    raise ValueError("Income file household count does not match Sample Adult file")
                entry.update(source=source["url"], source_page=SOURCE_PAGE, year=2024,
                             archive_sha256=sha256_file(archive), downloaded_at=_now())
                os.replace(str(dest), str(data_dir / source["file"]))
                manifest["datasets"][kind] = entry
                _atomic_json(manifest_path, manifest)
            except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as e:
                errors.append({"dataset": kind, "message": str(e)})
        if manifest["datasets"]:
            for name, url in DOCUMENTS.items():
                docs_dir = data_dir / "documentation"
                docs_dir.mkdir(exist_ok=True)
                dest = docs_dir / name
                entry = manifest.get("documents", {}).get(name, {})
                if dest.exists() and entry.get("sha256") == sha256_file(dest):
                    continue
                try:
                    tmp_doc = temp_dir / name
                    _download(url, tmp_doc, limit=40 * 1024 * 1024)
                    with tmp_doc.open("rb") as f:
                        if f.read(5) != b"%PDF-":
                            raise ValueError("CDC documentation response is not PDF")
                    entry = {"source": url, "sha256": sha256_file(tmp_doc), "downloaded_at": _now()}
                    os.replace(str(tmp_doc), str(dest))
                    manifest.setdefault("documents", {})[name] = entry
                except (OSError, ValueError) as e:
                    errors.append({"document": name, "message": str(e)})
            manifest["downloaded_at"] = _now()
            _atomic_json(manifest_path, manifest)
    result = status(root)
    result["errors"] = errors
    result["status"] = "ok" if result["ready"] and result["income_ready"] and not errors else "partial" if result["ready"] else "blocked"
    if not result["ready"]:
        result["message"] = "The official CDC data could not be downloaded. Run scripts/setup.command in a local terminal and try again. No demonstration data were generated."
    return result
