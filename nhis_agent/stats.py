"""Auditable bridge to R's survey package; never substitutes synthetic estimates."""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "registry.json"
REQUIRED = ("SRVY_YR", "RECTYPE", "HHX", "WTFA_A", "PSTRAT", "PPSU", "AGEP_A", "PHSTAT_A", "EDUCP_A", "HISPALLP_A", "SEX_A")
GROUPS = {
    "education": ("less_than_high_school", "high_school", "some_college", "bachelors_plus"),
    "race": ("hispanic", "nh_white", "nh_black", "nh_asian", "nh_other"),
    "sex": ("male", "female"),
    "age": ("25_34", "35_44", "45_54", "55_64"),
    "income": ("below_100", "100_199", "200_399", "400_plus"),
}


def _blocked(code, reason, **extra):
    result = {"status": "blocked", "code": code, "reason": reason, "rows": [], "checks": [],
              "created_at": datetime.now(timezone.utc).isoformat()}
    result.update(extra)
    return result


def readiness():
    """Inspect project-local R first, then PATH, without changing the computer."""
    candidates = []
    project_r = ROOT / ".r-env" / "bin" / "Rscript"
    if project_r.is_file() and os.access(str(project_r), os.X_OK):
        candidates.append(str(project_r))
    path_r = shutil.which("Rscript")
    if path_r and path_r not in candidates:
        candidates.append(path_r)
    if not candidates:
        return {"ready": False, "status": "blocked", "rscript": None,
                "reason": "Rscript is unavailable. Run scripts/setup.command to install the isolated R environment, or install R and survey, jsonlite, and mitools.",
                "install_url": "https://cran.r-project.org/", "packages": ["survey", "jsonlite", "mitools"]}
    expression = ('p <- c("survey","jsonlite","mitools"); ok <- vapply(p,requireNamespace,logical(1),quietly=TRUE); '
                  'if(!all(ok)){cat(paste(p[!ok],collapse=","));quit(status=2)}; '
                  'cat(as.character(getRversion()),as.character(packageVersion("survey")),'
                  'as.character(packageVersion("jsonlite")),as.character(packageVersion("mitools")),sep=intToUtf8(10))')
    failures = []
    for executable in candidates:
        try:
            process = subprocess.run([executable, "--vanilla", "-e", expression], capture_output=True,
                                     text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(executable + ": " + str(exc))
            continue
        if process.returncode:
            failures.append(executable + ": " + (process.stdout.strip() or process.stderr.strip() or "dependency check failed"))
            continue
        versions = process.stdout.strip().splitlines()
        if len(versions) != 4:
            failures.append(executable + ": could not parse the four required runtime versions")
            continue
        return {"ready": True, "status": "ok", "rscript": executable,
                "r_version": versions[0], "survey_version": versions[1],
                "jsonlite_version": versions[2], "mitools_version": versions[3]}
    return {"ready": False, "status": "blocked", "rscript": candidates[0],
            "reason": "R runtime or dependencies are unavailable: " + "; ".join(failures),
            "packages": ["survey", "jsonlite", "mitools"],
            "install_command": "Run scripts/setup.command, or install R packages survey, jsonlite, and mitools."}


def _normalise_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("Analysis specification must be an object.")
    year = spec.get("year", 2024)
    if isinstance(year, bool) or str(year) != "2024":
        raise ValueError("Only the verified 2024 NHIS Sample Adult release is supported.")
    if spec.get("outcome", "fair_poor") != "fair_poor":
        raise ValueError("Only fair/poor self-rated health (PHSTAT_A = 4 or 5) is implemented.")
    if spec.get("age_min", 25) != 25 or spec.get("age_max", 64) != 64:
        raise ValueError("The registered age domain is fixed at 25–64 years.")
    if spec.get("confidence", 0.95) != 0.95:
        raise ValueError("The registered confidence level is fixed at 95%.")
    analysis = spec.get("analysis", "proportion")
    if analysis not in ("proportion", "gradient", "regression"):
        raise ValueError("Unsupported analysis; choose proportion, gradient, or regression.")
    group_by = spec.get("group_by", "overall")
    if group_by not in ("overall", "education", "race", "sex", "age", "education_race", "income"):
        raise ValueError("Unsupported grouping variable.")
    filters = spec.get("filters", {})
    if not isinstance(filters, dict):
        raise ValueError("Filters must be an object.")
    clean_filters = {}
    for variable, values in filters.items():
        if variable not in GROUPS:
            raise ValueError("Unsupported filter: " + str(variable))
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or any(v not in GROUPS[variable] for v in values):
            raise ValueError("Invalid category for " + variable)
        clean_filters[variable] = list(dict.fromkeys(values))
    adjustment = spec.get("adjustment", "full")
    if adjustment not in ("full", "no_income"):
        raise ValueError("Unsupported adjustment; choose full or explicitly requested no_income.")
    if any(k in spec for k in ("adjusters", "covariates", "model", "formula")):
        raise ValueError("Custom model terms are not supported; use the registered full or explicit no_income model.")
    if analysis == "gradient" and group_by == "overall":
        group_by = "education"
    if analysis == "gradient" and group_by not in ("education", "education_race", "income"):
        raise ValueError("Gradients require education, education_race, or income grouping.")
    if analysis == "gradient":
        variable = "income" if group_by == "income" else "education"
        reference = "400_plus" if variable == "income" else "bachelors_plus"
        if variable in clean_filters and (len(clean_filters[variable]) < 2 or reference not in clean_filters[variable]):
            raise ValueError("A gradient must retain the registered reference category and at least one comparison category.")
    minimum = spec.get("min_n", 100)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 100 <= minimum <= 100000:
        raise ValueError("min_n must be an integer from 100 to 100000; the safety floor is 100.")
    answer = {"year": 2024, "outcome": "fair_poor", "analysis": analysis, "group_by": group_by,
              "filters": clean_filters, "min_n": minimum, "adjustment": adjustment}
    if "income_csv_path" in spec:
        if not isinstance(spec["income_csv_path"], str):
            raise ValueError("income_csv_path must be a local path string.")
        answer["income_csv_path"] = str(Path(spec["income_csv_path"]).expanduser().resolve())
    return answer


def _preflight(csv_path):
    """Check the full official release, not a user-filtered extract, before R sees it."""
    source = Path(csv_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("The official adult CSV has not been downloaded: " + str(source))
    with REGISTRY_PATH.open(encoding="utf-8") as stream:
        registry = json.load(stream)
    expected_n = registry["datasets"]["adult_2024"]["expected_rows"]
    count, strata, households = 0, {}, set()
    numeric_fields = [field for field in REQUIRED if field != "HHX"]
    valid_codes = {"PHSTAT_A": set(range(1, 6)) | {7, 8, 9},
                   "EDUCP_A": set(range(0, 11)) | {97, 98, 99},
                   "HISPALLP_A": set(range(1, 8)) | {97, 98, 99},
                   "SEX_A": {1, 2, 7, 8, 9},
                   "AGEP_A": set(range(18, 86)) | {97, 98, 99}}
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []
        if len(names) != len(set(names)):
            raise ValueError("The CSV contains duplicate column names.")
        missing = sorted(set(REQUIRED) - set(names))
        if missing:
            raise ValueError("Missing official variables: " + ", ".join(missing))
        for row in reader:
            count += 1
            try:
                vals = {name: float(row[name]) for name in numeric_fields}
            except (ValueError, TypeError):
                raise ValueError("Missing or nonnumeric required variable at data row " + str(count))
            if not all(math.isfinite(v) for v in vals.values()):
                raise ValueError("Nonfinite required variable at data row " + str(count))
            if vals["SRVY_YR"] != 2024 or vals["RECTYPE"] != 10:
                raise ValueError("CSV must contain only 2024 Sample Adult records (RECTYPE=10).")
            if vals["WTFA_A"] <= 0 or vals["PSTRAT"] <= 0 or vals["PPSU"] <= 0:
                raise ValueError("Nonpositive final weight, stratum, or PSU at data row " + str(count))
            if any(vals[k] != int(vals[k]) for k in ("PSTRAT", "PPSU")):
                raise ValueError("Stratum and PSU codes must be integers.")
            for name, codes in valid_codes.items():
                if vals[name] not in codes:
                    raise ValueError("Unrecognized " + name + " code at data row " + str(count))
            key = row["HHX"]
            if not key or key in households:
                raise ValueError("Missing or duplicate Sample Adult household identifier.")
            households.add(key)
            strata.setdefault(int(vals["PSTRAT"]), set()).add(int(vals["PPSU"]))
    if count != expected_n:
        raise ValueError("Expected the full official adult release ({} records), received {}. "
                         "Do not filter the CSV before constructing the survey design.".format(expected_n, count))
    if not strata or any(len(psus) < 2 for psus in strata.values()):
        raise ValueError("At least one full-sample stratum has fewer than two PSUs; variance estimation is blocked.")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(source), "sha256": digest.hexdigest(), "records": count,
            "strata": len(strata), "psus": sum(len(v) for v in strata.values()),
            "design_df": sum(len(v) - 1 for v in strata.values()),
            "source_url": registry["datasets"]["adult_2024"]["url"],
            "documentation_url": registry["documentation_url"],
            "source_authentication": "Local SHA-256 and official release structure checks; not a CDC-published checksum."}


def _income_preflight(income_path, adult_path):
    source = Path(income_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("All 10 official income imputations are required, but adultinc24.csv is missing. "
                         "Download the official income file; single-imputation adult fields are never substituted.")
    with Path(adult_path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.reader(stream)
        column = next(reader).index("HHX")
        adult_keys = {row[column] for row in reader}
    keys = {m: set() for m in range(1, 11)}
    count = 0
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"RECTYPE", "HHX", "IMPNUM_A", "RATCAT_A"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing required official income variables.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("Duplicate income CSV column names.")
        for row in reader:
            count += 1
            try:
                record_type = float(row["RECTYPE"])
                imputation = float(row["IMPNUM_A"])
                ratcat = float(row["RATCAT_A"])
            except (ValueError, TypeError):
                raise ValueError("Missing or nonnumeric required income code at row " + str(count))
            if record_type != 30 or imputation not in keys or ratcat not in range(1, 15):
                raise ValueError("Invalid record type, imputation, or poverty category in the income file.")
            m = int(imputation)
            key = row["HHX"]
            if key not in adult_keys or key in keys[m]:
                raise ValueError("Income household keys are unmatched or duplicated within an imputation.")
            keys[m].add(key)
    if count != 10 * len(adult_keys) or any(v != adult_keys for v in keys.values()):
        raise ValueError("Income file must contain exactly one record per adult household in each of all 10 imputations.")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(source), "sha256": digest.hexdigest(), "records": count,
            "imputations": 10, "records_per_imputation": len(adult_keys),
            "merge_key": ["HHX", "IMPNUM_A"], "all_households_match": True,
            "source_url": "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Datasets/NHIS/2024/adultinc24csv.zip"}


def analyze(csv_path: str, spec: dict, run_dir: str) -> dict:
    """Execute a prespecified real-data survey analysis and retain its audit artifacts."""
    destination = Path(run_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        clean_spec = _normalise_spec(spec)
    except (ValueError, TypeError) as exc:
        result = _blocked("invalid_spec", str(exc))
    else:
        requires_mi = (clean_spec["group_by"] == "income" or "income" in clean_spec["filters"] or
                       (clean_spec["analysis"] == "regression" and clean_spec["adjustment"] == "full"))
        if requires_mi and "income_csv_path" not in clean_spec:
            clean_spec["income_csv_path"] = str(Path(csv_path).expanduser().resolve().parent / "adultinc24.csv")
        request_path = destination / "analysis_spec.json"
        request_path.write_text(json.dumps(clean_spec, indent=2) + "\n", encoding="utf-8")
        try:
            provenance = _preflight(csv_path)
            provenance["analysis_sources_sha256"] = {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("config/registry.json", "scripts/analyze.R", "nhis_agent/stats.py")}
            if requires_mi:
                provenance["income"] = _income_preflight(clean_spec["income_csv_path"], provenance["path"])
        except (ValueError, OSError, csv.Error, KeyError, json.JSONDecodeError) as exc:
            result = _blocked("data_preflight_failed", str(exc), spec=clean_spec)
        else:
            runtime = readiness()
            if not runtime["ready"]:
                result = _blocked("r_runtime_unavailable", runtime["reason"], runtime=runtime,
                                  provenance=provenance, spec=clean_spec)
            else:
                output_path = destination / "r_result.json"
                # A previous output may never stand in for a failed current run.
                if output_path.exists():
                    output_path.unlink()
                command = [runtime["rscript"], "--vanilla", str(ROOT / "scripts" / "analyze.R"),
                           str(Path(csv_path).expanduser().resolve()), str(request_path), str(output_path),
                           str(REGISTRY_PATH)]
                (destination / "command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
                try:
                    process = subprocess.run(command, capture_output=True, text=True, timeout=240,
                                             check=False, cwd=str(ROOT))
                    (destination / "R.stdout.log").write_text(process.stdout, encoding="utf-8")
                    (destination / "R.stderr.log").write_text(process.stderr, encoding="utf-8")
                    if process.returncode != 0 or not output_path.is_file():
                        result = _blocked("r_analysis_failed", "The survey engine failed; inspect R.stderr.log.")
                    else:
                        result = json.loads(output_path.read_text(encoding="utf-8"))
                        if not isinstance(result, dict) or result.get("status") not in ("ok", "blocked"):
                            raise ValueError("Unexpected R result format")
                        if not isinstance(result.get("rows"), list):
                            raise ValueError("R result rows must be an array")
                        # Strict serialization rejects NaN rather than letting a UI show a result as valid.
                        json.dumps(result, allow_nan=False)
                except subprocess.TimeoutExpired:
                    result = _blocked("r_timeout", "Survey analysis exceeded the 240-second runtime limit.")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    result = _blocked("r_result_invalid", str(exc))
                result.update({"provenance": provenance, "runtime": runtime, "spec": clean_spec})
                result["checks"] = [{"name": "full_official_release_structure", "status": "passed"},
                                    {"name": "positive_complete_survey_design", "status": "passed"}] + result.get("checks", [])
    result["artifacts"] = {"spec": "analysis_spec.json", "result": "analysis_result.json",
                           "r_script": "scripts/analyze.R", "stata_reference": "scripts/reference.do"}
    (destination / "analysis_result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
