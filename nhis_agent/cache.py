"""Cache only validated survey outputs under exact data, code, spec and R versions.

Model measurements are never cached: every registered repetition remains a new call.
"""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

from . import stats

ROOT = Path(__file__).resolve().parents[1]


class SurveyCache:
    def __init__(self, root):
        self.directory = Path(root) / '.cache' / 'survey'

    def identity(self, spec, dataset):
        adult = dataset.get('sha256')
        if not isinstance(adult, str) or not re.fullmatch(r'[a-f0-9]{64}', adult):
            return None
        clean = stats._normalise_spec(spec)
        for key, values in clean.get('filters', {}).items():
            clean['filters'][key] = sorted(values)
        uses_income = clean['group_by'] == 'income' or 'income' in clean['filters'] or clean['analysis'] == 'regression' and clean['adjustment'] == 'full'
        income = (dataset.get('datasets', {}).get('income') or {}).get('sha256') if uses_income else None
        if uses_income and (not isinstance(income, str) or not re.fullmatch(r'[a-f0-9]{64}', income)):
            return None
        runtime = stats.readiness()
        if not runtime.get('ready'):
            return None
        version_keys = ('r_version','survey_version','jsonlite_version','mitools_version')
        if any(not runtime.get(key) or runtime[key] == 'unknown' for key in version_keys):
            return None
        identity = {'spec':clean, 'adult_sha256':adult, 'income_sha256':income,
                    'versions':{k:runtime.get(k) for k in version_keys},
                    'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                               for name in ('config/registry.json','scripts/analyze.R','nhis_agent/stats.py')}}
        return {'key':hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest(), 'identity':identity}

    def read(self, identity):
        if not identity:
            return None
        path = self.directory / (identity['key'] + '.json')
        try:
            if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or not isinstance(value.get('survey'), dict):
                return None
            if value.get('key') != identity['key'] or value.get('identity') != identity['identity'] or value['survey'].get('status') != 'ok':
                return None
            checksum = hashlib.sha256(json.dumps(value['survey'],sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            if value.get('survey_sha256') != checksum:
                return None
            json.dumps(value,allow_nan=False)
            return value
        except (OSError, ValueError, TypeError):
            return None

    def save(self, identity, survey, run_id):
        if not identity or survey.get('status') != 'ok':
            return
        self.directory.mkdir(parents=True,exist_ok=True)
        value = dict(identity,survey=copy.deepcopy(survey),source_run_id=run_id,
                     cached_at=dt.datetime.now(dt.timezone.utc).isoformat())
        value['survey_sha256'] = hashlib.sha256(json.dumps(survey,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        path = self.directory/(identity['key']+'.json')
        # Unique temporary names permit separate conversations to write the same valid key.
        import uuid
        temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf-8')
        temporary.replace(path)
