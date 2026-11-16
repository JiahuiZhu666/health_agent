"""Fixed statistical pipeline; language never controls executable statistical code."""
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from . import data, models, stats
from .spec import route, fingerprint, GROUPS
from .process import ProcessTrace, describe_spec
from .cache import SurveyCache
from . import chat_api, grounding, dialogue

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def validate_rows(rows):
    """Failed gates prevent a result from entering the answer or exported report."""
    errors = []
    seen = set()
    for row in rows:
        gid = row.get('group_id')
        if not gid or gid in seen or row.get('status') not in ('ok', 'suppressed'):
            errors.append('A group identifier or release status failed validation.')
        seen.add(gid)
        if row.get('status') == 'suppressed':
            # Defense in depth: a small cell never exposes an estimate.
            for field in ['estimate', 'se', 'ci_low', 'ci_high']:
                row[field] = None
            continue
        fields = [row.get(k) for k in ['estimate', 'se', 'ci_low', 'ci_high']]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in fields):
            errors.append('A nonfinite estimate or missing variance was found: ' + str(row.get('label')))
            continue
        p, se, lo, hi = fields
        if not (0 <= lo <= p <= hi <= 1 and se >= 0):
            errors.append('A proportion or confidence interval failed validation: ' + str(row.get('label')))
        if not isinstance(row.get('n'), (int, float)) or row['n'] < 100:
            errors.append('The unweighted sample size is below 100: ' + str(row.get('label')))
    return errors


def validate_advanced(survey):
    errors = []
    for contrast in survey.get('gradient', {}).get('contrasts', []):
        if contrast.get('status') != 'ok':
            for key in ['estimate', 'se', 'ci_low', 'ci_high']:
                contrast[key] = None
            continue
        values = [contrast.get(k) for k in ['estimate', 'se', 'ci_low', 'ci_high']]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            errors.append('A gradient contains an invalid estimate or variance.')
        elif not (-1 <= values[0] <= 1 and values[1] > 0 and values[2] <= values[0] <= values[3]):
            errors.append('A gradient or its confidence interval failed validation.')
    regression = survey.get('regression', {})
    if regression.get('status') == 'ok':
        if regression.get('n', 0) < 100 or regression.get('n') != regression.get('unadjusted', {}).get('n'):
            errors.append('The regression sample size or same-sample check failed.')
        for coefficients in [regression.get('coefficients', []), regression.get('unadjusted', {}).get('coefficients', [])]:
            if not coefficients:
                errors.append('Regression coefficients are missing.')
            for term in coefficients:
                values = [term.get(k) for k in ['log_odds', 'se', 'odds_ratio', 'ci_low', 'ci_high']]
                if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
                    errors.append('The regression contains an invalid coefficient or interval.')
                elif not (values[1] > 0 and 0 < values[3] <= values[2] <= values[4]):
                    errors.append('A regression standard error or odds-ratio interval failed validation.')
    return errors


def compare(survey_rows, model_rows):
    measured = {r['group_id']: r for r in model_rows}
    result = []
    for row in survey_rows:
        model = measured.get(row['group_id'])
        if row.get('status') != 'ok' or not model or model.get('mean') is None:
            continue
        mean = model['mean']
        if not isinstance(mean, (int, float)) or not math.isfinite(mean) or not 0 <= mean <= 1:
            continue
        result.append({'group_id': row['group_id'], 'label': row['label'], 'survey': row['estimate'],
                       'model_mean': mean, 'model_sd': model.get('sd'), 'gap_pp': 100 * (mean - row['estimate']),
                       'abs_gap_pp': 100 * abs(mean - row['estimate']),
                       'inside_ci': row['ci_low'] <= mean <= row['ci_high'],
                       'valid_count': model.get('valid_count'), 'total_count': model.get('total_count')})
    return sorted(result, key=lambda r: r['abs_gap_pp'], reverse=True)


def csv_report(result):
    output = io.StringIO()
    regression = result.get('survey', {}).get('regression', {})
    if regression.get('status') == 'ok':
        fields = ['model', 'term', 'log_odds', 'se', 'odds_ratio', 'ci_low', 'ci_high', 'df', 'missing_information']
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for kind, coefficients in [('adjusted', regression.get('coefficients', [])),
                                   ('unadjusted_same_sample', regression.get('unadjusted', {}).get('coefficients', []))]:
            for coefficient in coefficients:
                writer.writerow(dict(coefficient, model=kind))
        return '\ufeff' + output.getvalue()
    fields = ['group_id', 'label', 'n', 'estimate', 'se', 'ci_low', 'ci_high', 'status', 'suppression_reason',
              'model_mean', 'gap_pp', 'inside_ci', 'valid_count', 'total_count']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    comparison = {r['group_id']: r for r in result.get('comparison', [])}
    for row in result.get('survey', {}).get('rows', []):
        writer.writerow(dict(row, **{k: v for k, v in comparison.get(row['group_id'], {}).items() if k not in row}))
    return '\ufeff' + output.getvalue()


def markdown_report(result):
    lines = ['# NHIS 2024 health stratification analysis record', '', 'Run: ' + result['run_id'], '', result['answer'], '',
             '## Registered specification', '', '```json', json.dumps(result['spec'], ensure_ascii=False, indent=2), '```', '',
             '## Survey estimates', '', '| Group | Unweighted n | Fair or poor (%) | 95% CI (%) | Status |', '|---|---:|---:|---|---|']
    for row in result['survey'].get('rows', []):
        if row.get('status') == 'ok':
            lines.append('| {} | {} | {:.2f} | {:.2f}–{:.2f} | Passed |'.format(row['label'], row['n'], 100*row['estimate'], 100*row['ci_low'], 100*row['ci_high']))
        else:
            lines.append('| {} | {} | — | — | {} |'.format(row['label'], row['n'], row.get('suppression_reason', 'Suppressed')))
    regression = result['survey'].get('regression', {})
    if regression.get('status') == 'ok':
        lines += ['', '## Logistic regression', '', 'Model: `' + regression['formula'] + '`',
                  '', 'Complete-case sample n=' + str(regression['n']) + '; the unadjusted and adjusted models use the same analysis sample.',
                  '', '| Term | Adjusted OR | 95% CI |', '|---|---:|---|']
        for term in regression.get('coefficients', []):
            lines.append('| {} | {:.3f} | {:.3f}–{:.3f} |'.format(term['term'], term['odds_ratio'], term['ci_low'], term['ci_high']))
        lines += ['', 'See the JSON and CSV records for the unadjusted same-sample coefficients and full covariance provenance. Odds ratios are not risk ratios.']
    contrasts = result['survey'].get('gradient', {}).get('contrasts', [])
    if contrasts:
        lines += ['', '## Gradients (percentage points)', '', '| Contrast | Survey difference | 95% CI |', '|---|---:|---|']
        for contrast in contrasts:
            if contrast.get('status') == 'ok':
                lines.append('| {} | {:+.2f} | {:+.2f}–{:+.2f} |'.format(contrast['label'], contrast['estimate'] * 100, contrast['ci_low'] * 100, contrast['ci_high'] * 100))
            else:
                lines.append('| {} | — | Suppressed |'.format(contrast['label']))
    if result.get('gradient_comparison'):
        lines += ['', '## Model–survey gradient gaps', '', '| Contrast | Survey gradient (pp) | Model gradient (pp) | Gradient gap (pp) |', '|---|---:|---:|---:|']
        for contrast in result['gradient_comparison']:
            lines.append('| {} | {:+.2f} | {:+.2f} | {:+.2f} |'.format(contrast['label'], contrast['survey_gradient_pp'], contrast['model_gradient_pp'], contrast['gradient_gap_pp']))
    lines += ['', '## Methods and verification status', '', 'Construct the complex survey design on the full adult sample before restricting to the population aged 25–64, using WTFA_A weights, PSTRAT strata, and PPSU sampling units.',
              'The outcome is PHSTAT_A=4 or 5. Invalid responses, refusals, and other missing responses are not classified as healthy. See result.json for denominators and missing-data handling.',
              'Independent R/Stata agreement checks and external NCHS comparisons remain pending. These research results await independent verification.',
              'These cross-sectional associations do not establish causation. Fair or poor self-rated health is not a clinical diagnosis, and agreement with survey estimates does not make a model a substitute for the survey.']
    if result.get('comparison'):
        lines += ['', '## Model–survey gaps', '', '| Group | Model mean (%) | Model − survey (percentage points) | Within survey CI | Valid/planned calls |', '|---|---:|---:|---|---|']
        for row in result['comparison']:
            lines.append('| {} | {:.2f} | {:+.2f} | {} | {}/{} |'.format(row['label'],100*row['model_mean'],row['gap_pp'],'Yes' if row['inside_ci'] else 'No',row['valid_count'],row['total_count']))
    lines += ['', 'Model measurement status: ' + str(result.get('measurement', {}).get('status')), '',
              'The JSON record includes the data SHA256, specification hash, prompt versions, raw model responses, and failures. Model measurement sends only registered group definitions.']
    return '\n'.join(lines) + '\n'


class Agent:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        (self.root / 'runs').mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._download_state = {'status': 'idle', 'message': ''}
        self._model = {
            'provider': os.environ.get('NHIS_MODEL_PROVIDER', os.environ.get('MODEL_PROVIDER', 'openai')),
            'base_url': os.environ.get('NHIS_MODEL_BASE_URL', os.environ.get('MODEL_BASE_URL', 'http://127.0.0.1:11434' if os.environ.get('NHIS_MODEL_PROVIDER', os.environ.get('MODEL_PROVIDER')) == 'ollama' else 'https://api.openai.com/v1')),
            'model': os.environ.get('NHIS_MODEL_NAME', os.environ.get('MODEL_NAME', '')),
            'api_key': os.environ.get('OPENAI_API_KEY', ''),
        }
        self._model_config_lock = threading.RLock()
        self._model_revision = 0
        self._dialogue_connection = {'status': 'unverified', 'message': 'No real API request has been performed yet.'}

    def status(self):
        registry_path = self.root / 'config' / 'registry.json'
        if not registry_path.is_file():
            registry_path = ROOT / 'config' / 'registry.json'
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
        config = self._model.copy()
        return {'data': dict(data.status(self.root), download=self._download_state.copy()),
                'statistics': stats.readiness(),
                'models': models.public_status(config),
                'dialogue': self.dialogue_status(),
                'capabilities': [{'id': key, 'label': value} for key, value in GROUPS.items()],
                'registry': registry,
                'validation': {'r_stata': 'pending', 'nchs_external': 'pending', 'real_data_only': True},
                'project': str(self.root)}

    def history(self, limit=30):
        """List only completed local run metadata, never model responses or records."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('The history limit must be an integer from 1 to 100.')
        runs_root = self.root / 'runs'
        try:
            directories = sorted(runs_root.iterdir(), key=lambda path: path.name, reverse=True)
        except OSError:
            return []
        items = []
        for directory in directories:
            if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}', directory.name):
                continue
            registration_path = directory / 'registration.json'
            result_path = directory / 'result.json'
            try:
                if registration_path.is_symlink() or result_path.is_symlink():
                    continue
                # Partial writes and unexpectedly large files do not block the list.
                if registration_path.stat().st_size > 64 * 1024 or result_path.stat().st_size > 16 * 1024 * 1024:
                    continue
                registration = json.loads(registration_path.read_text(encoding='utf-8'))
                result = json.loads(result_path.read_text(encoding='utf-8'))
                if not isinstance(registration, dict) or not isinstance(result, dict):
                    continue
                spec = registration.get('spec')
                question = registration.get('question')
                created_at = registration.get('registered_at')
                if (result.get('run_id') != directory.name or result.get('status') not in ('ok', 'blocked', 'clarify')
                        or not isinstance(spec, dict) or not isinstance(question, str) or not 1 <= len(question) <= 2000
                        or not isinstance(created_at, str)):
                    continue
                dt.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                measurement = result.get('measurement')
                model = measurement.get('model') if isinstance(measurement, dict) else None
                items.append({'run_id': directory.name, 'question': question,
                              'status': result['status'], 'group_by': spec.get('group_by'),
                              'analysis': spec.get('analysis'), 'model': model if isinstance(model, str) else None,
                              'created_at': created_at})
            except (OSError, ValueError, TypeError, UnicodeError):
                continue
            if len(items) >= limit:
                break
        return items

    def configure_model(self, config):
        provider = config.get('provider', 'openai')
        if provider not in ('openai', 'ollama'):
            raise ValueError('Only OpenAI-compatible endpoints or Ollama are supported.')
        from urllib.parse import urlsplit
        default = 'http://127.0.0.1:11434' if provider == 'ollama' else 'https://api.openai.com/v1'
        base = str(config.get('base_url') or default).strip().rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('The model endpoint must be a valid HTTP(S) base URL without credentials or query parameters.')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote model endpoints must use HTTPS. HTTP is allowed only for local services such as Ollama.')
        model = str(config.get('model', '')).strip()
        if not model or len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ValueError('Enter a model name.')
        with self._model_config_lock:
            same_endpoint = self._model.get('provider') == provider and self._model.get('base_url') == base
            key = str(config.get('api_key') or (self._model.get('api_key') if same_endpoint else '') or '').strip()
            self._model = {'provider': provider, 'base_url': base, 'model': model,
                           'api_key': key if provider == 'openai' else ''}
            self._model_revision += 1
            self._dialogue_connection = {'status':'unverified', 'message':'Connection settings are saved but have not been verified by a real request.'}
        return {'status': 'ok', 'message': 'Settings are saved in server memory. The connection remains unverified until a real API request is performed.'}

    def dialogue_status(self):
        """Public connection evidence, distinct from merely having credentials."""
        with self._model_config_lock:
            state = models.public_status(self._model.copy())
            connection = dict(self._dialogue_connection)
        if not state.get('configured'):
            connection = {'status':'not_configured', 'message':'The server has not configured a model connection.'}
        state['message'] = connection.get('message') or ('The most recent real API request succeeded.' if connection.get('status')=='ok' else state.get('message'))
        return dict(state, connection=connection)

    def _record_dialogue_call(self, result, revision):
        fields = ('status','provider','model','response_id','request_id','usage','elapsed_ms',
                  'http_status','error_code','message','api_status')
        public = {key:result[key] for key in fields if key in result}
        public['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._model_config_lock:
            if revision == self._model_revision:
                self._dialogue_connection = public
        return public

    def test_connection(self):
        """One real, bounded generation request; no research experiment is run."""
        with self._model_config_lock:
            config, revision = self._model.copy(), self._model_revision
        result = chat_api.test_connection(config)
        self._record_dialogue_call(result, revision)
        return {'status':result['status'], 'message':result.get('message') or
                ('The real API request succeeded.' if result['status']=='ok' else 'The real API request did not succeed.'),
                'dialogue':self.dialogue_status()}

    def compose_reply(self, question, draft):
        """Generate wording through the API while keeping statistical evidence fixed."""
        with self._model_config_lock:
            config, revision = self._model.copy(), self._model_revision
        if not models.public_status(config).get('configured'):
            return {'status':'not_configured', 'content':None, 'error_code':'missing_configuration',
                    'message':'The server has not configured a model connection.'}
        try:
            payload = grounding.prepare(question, draft)
        except (ValueError, TypeError):
            return {'status':'failed','content':None,'error_code':'evidence_preparation_failed',
                    'message':'Reply evidence could not be prepared. No API request was sent.'}
        result = chat_api.request(config, grounding.SYSTEM_PROMPT,
                                  json.dumps(payload, ensure_ascii=False),
                                  schema=grounding.RESPONSE_SCHEMA, max_output_tokens=3200)
        if result.get('status') == 'ok':
            try:
                result['content'] = grounding.render(payload, result['content'])
            except (ValueError, TypeError, KeyError):
                result.update(status='failed', api_status='ok', content=None,
                              error_code='answer_validation_failed',
                              message='The API responded, but the answer failed statistical evidence validation and was not published as a model-generated reply.')
        self._record_dialogue_call(result, revision)
        return result

    def decide_reply(self, question, history, research_context=None):
        """Use conversation history for free chat without executing survey work."""
        with self._model_config_lock:
            config, revision = self._model.copy(), self._model_revision
        if not models.public_status(config).get('configured'):
            return {'status':'not_configured','mode':None,'reply':'',
                    'error_code':'missing_configuration','message':'The server connection is not configured.'}
        result = dialogue.decide(config, question, history, research_context)
        self._record_dialogue_call(result, revision)
        return result

    def setup_data(self):
        with self._lock:
            if self._download_state['status'] == 'running':
                return self._download_state.copy()
            self._download_state = {'status': 'running', 'message': 'Downloading the official NHIS 2024 public-use files from CDC.'}
        def worker():
            try:
                result = data.download(self.root)
                self._download_state = {'status': 'complete' if result.get('ready') or result.get('status') == 'ok' else 'failed',
                                        'message': result.get('message', 'Download finished. Check the real-data status.')}
            except Exception:
                self._download_state = {'status': 'failed', 'message': 'The CDC data download failed. Run scripts/setup.command in a local terminal and check the network connection.'}
        threading.Thread(target=worker, daemon=True).start()
        return self._download_state.copy()

    def ask(self, question, measure_model=False, repeats=5, prompt_versions=3, on_progress=None):
        trace = ProcessTrace(on_progress)
        trace.emit('understand', 'running', 'Map the question to the registered population, outcome, and grouping.')
        routed = route(question)
        if routed['status'] != 'ready':
            trace.emit('understand', 'blocked', routed['answer'])
            return dict(routed, messages=[], process=trace.events)
        trace.emit('understand', 'complete', 'The question has been mapped to a structured analysis specification.')
        return self._execute_spec(routed['spec'], question, measure_model, repeats, prompt_versions, trace)

    def analyze_spec(self, spec, question, measure_model=False, repeats=5, prompt_versions=3, on_progress=None):
        """Execute a validated context-resolved specification, without reparsing a follow-up."""
        trace = ProcessTrace(on_progress)
        trace.emit('understand', 'running', 'Combine this request with the research scope of the current conversation.')
        try:
            clean = stats._normalise_spec(spec)
        except (ValueError, TypeError) as error:
            trace.emit('understand', 'blocked', str(error))
            return {'status':'blocked', 'answer':str(error), 'messages':[], 'process':trace.events}
        constants = {key: spec[key] for key in ('version', 'age_min', 'age_max', 'confidence') if key in spec}
        # Keep caller-friendly scalar/list filters; R receives its own normalized version.
        validated = dict(clean, **constants)
        trace.emit('understand', 'complete', 'Conversation context is resolved; the year, outcome, and survey rules retain their registered values.')
        return self._execute_spec(validated, question, measure_model, repeats, prompt_versions, trace)

    def _execute_spec(self, spec, question, measure_model, repeats, prompt_versions, trace):
        if not isinstance(measure_model, bool):
            raise ValueError('measure_model must be Boolean.')
        if type(repeats) is not int or not 1 <= repeats <= 5 or type(prompt_versions) is not int or not 1 <= prompt_versions <= 3:
            raise ValueError('Use 1–5 repetitions and 1–3 prompt versions.')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError('Enter a question of 1–2,000 characters.')
        model_config = self._model.copy()
        trace.emit('scope', 'running', 'Check the registered population, outcome, filters, and statistical method.')
        plan = describe_spec(spec)
        trace.emit('scope', 'complete', plan['population'] + '; grouped by ' + plan['group_by'] + '; ' + ('; '.join(plan['filters']) or 'no additional category filters'), plan)
        run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:10]
        directory = self.root / 'runs' / run_id
        directory.mkdir()
        trace.attach(directory)
        registration = {'spec': spec, 'spec_hash': fingerprint(spec), 'question': question,
                        'measure_model': measure_model, 'repeats': repeats, 'prompt_versions': prompt_versions,
                        'registered_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        write_json(directory / 'registration.json', registration)
        result = {'run_id': run_id, 'status': 'blocked', 'spec': spec, 'spec_hash': fingerprint(spec),
                  'survey': {'rows': []}, 'measurement': {'status': 'not_requested', 'rows': []},
                  'comparison': [], 'messages': [], 'analysis_plan':plan,
                  'validation': {'r_stata': 'pending', 'nchs_external': 'pending'}}
        trace.emit('data', 'running', 'Verify the CDC download record, CSV integrity, and survey design fields without transmitting individual records.')
        dataset = data.status(self.root)
        if not dataset.get('ready'):
            result['answer'] = 'The official NHIS 2024 Sample Adult data have not been obtained and verified, so no statistics were generated. Select Download official data or run scripts/setup.command in a terminal.'
            result['messages'].append(dataset.get('message', 'The official data are not ready.'))
            trace.emit('data', 'blocked', result['answer'])
            return self._finish(directory, result, trace)
        result['data'] = {k: dataset.get(k) for k in ('sha256', 'source', 'row_count', 'downloaded_at', 'income_ready')}
        trace.emit('data', 'complete', 'The real Sample Adult file passed integrity checks.',
                   {'full_sample_n':dataset.get('row_count'), 'sha256':dataset.get('sha256'), 'income_ready':dataset.get('income_ready')})
        cache = SurveyCache(self.root)
        try:
            cache_identity = cache.identity(spec, dataset)
            cached = cache.read(cache_identity)
        except (ValueError, OSError):
            cache_identity, cached = None, None
        trace.emit('statistics', 'running', ('Read validated statistics matching the same data, specification, program, and R versions.' if cached else plan['method'] + '; construct the full design before restricting to the population aged 25–64.'),
                   {'design_variables':['WTFA_A','PSTRAT','PPSU'], 'missing_policy':'Use only valid denominators for the relevant outcome and grouping; missing responses are not classified as healthy.',
                    'estimation':'Registered complex-survey logistic models; adjusted and unadjusted models use the same complete cases.' if spec.get('analysis') == 'regression' else 'Group proportion = Σ(survey weight × fair/poor indicator) / Σsurvey weight; standard errors use Taylor linearization with strata and PSUs.',
                    'income_imputations':10 if spec.get('group_by') == 'income' or 'income' in spec.get('filters', {}) or spec.get('analysis') == 'regression' and spec.get('adjustment') == 'full' else None})
        if cached:
            survey = cached['survey']
            survey.setdefault('provenance', {})['cache_reuse'] = {'source_run_id':cached['source_run_id'], 'key':cached['key']}
            write_json(directory/'analysis_spec.json', stats._normalise_spec(spec))
            write_json(directory/'analysis_result.json', survey)
            write_json(directory/'cache.json', {'key':cached['key'], 'source_run_id':cached['source_run_id'],
                       'note':'R was not rerun. Aggregate estimates were reused only after matching the data, analysis specification, statistical source code, and R package versions.'})
        else:
            survey = stats.analyze(str(self.root / 'data' / 'adult24.csv'), spec, str(directory))
        result['cache'] = {'hit':bool(cached), 'source_run_id':cached['source_run_id'] if cached else None}
        result['survey'] = survey
        if survey.get('status') != 'ok':
            result['answer'] = survey.get('message') or survey.get('reason') or 'The statistical calculation failed validation and will not enter the report.'
            trace.emit('statistics', 'blocked', result['answer'])
            return self._finish(directory, result, trace)
        denominators = survey.get('denominators', {})
        trace.emit('statistics', 'complete', 'Matching validated historical statistics were loaded; R was not rerun.' if cached else 'The R survey program returned estimates, standard errors, and confidence intervals. Each group denominator includes only valid outcome and grouping responses.',
                   {'denominators':survey.get('denominators', {}), 'design':survey.get('design', {}),
                    'imputation':survey.get('imputation', {}), 'cache_hit':bool(cached),
                    'full_sample_n':denominators.get('full_sample_n'), 'age_domain_n':denominators.get('age_domain_n'),
                    'filtered_domain_n':denominators.get('filtered_domain_n'), 'valid_outcome_n':denominators.get('valid_outcome_n'),
                    'runtime':survey.get('runtime', {}), 'methods':survey.get('methods', {}),
                    'source_run_id':cached['source_run_id'] if cached else None})
        trace.emit('validation', 'running', 'Check denominators, intervals, sparse groups, and regression convergence. Values that fail checks are not released.')
        if spec['analysis'] == 'regression' and survey.get('regression', {}).get('status') != 'ok':
            reason = survey.get('regression', {}).get('reason', 'The regression calculation failed validation.')
            result['survey'] = {'status': 'blocked', 'rows': [], 'regression': survey.get('regression', {})}
            result['answer'] = 'The requested regression failed validation. Unadjusted proportions were not substituted for regression results: ' + reason
            trace.emit('validation', 'blocked', result['answer'])
            return self._finish(directory, result, trace)
        errors = validate_rows(survey.get('rows', [])) + validate_advanced(survey)
        if errors:
            result['messages'] = errors
            result['survey'] = {'status': 'blocked', 'rows': [], 'message': 'Result validation failed; numerical estimates were removed from the report.'}
            result['answer'] = 'Result validation failed; no statistics were released: ' + '; '.join(errors)
            trace.emit('validation', 'blocked', result['answer'])
            return self._finish(directory, result, trace)
        rows = [r for r in survey.get('rows', []) if r.get('status') == 'ok']
        if not rows and spec['analysis'] != 'regression':
            result['answer'] = 'All groups were suppressed because sample-size or statistical checks failed. No estimates are available for comparison.'
            trace.emit('validation', 'blocked', result['answer'])
            return self._finish(directory, result, trace)
        trace.emit('validation', 'complete', 'Application release checks are complete. Independent R/Stata agreement and external comparisons remain pending.',
                   {'released_groups':len(rows), 'suppressed_groups':sum(r.get('status') == 'suppressed' for r in survey.get('rows', [])),
                    'regression_converged':True if survey.get('regression', {}).get('status') == 'ok' else None,
                    'independent_validation':'pending'})
        if not cached:
            try:
                cache.save(cache_identity, survey, run_id)
            except (OSError, ValueError):
                pass
        if measure_model and spec['analysis'] == 'regression':
            result['measurement'] = {'status': 'not_applicable', 'rows': [],
                'message': 'Phase-one aggregate prompts measure group proportions and do not generate model regression coefficients. Coefficient comparisons require phase-two profile simulation, which is not available in this release.'}
            trace.emit('model', 'skipped', result['measurement']['message'])
        elif not measure_model:
            trace.emit('model', 'skipped', 'This step computed only the survey benchmark; no independent model measurement was requested.')
        if measure_model and rows:
            # Freeze the plan before calling any remote service. Never pass survey rows to models.
            groups = [{'group_id': r['group_id'], 'label': r['label']} for r in rows]
            write_json(directory / 'measurement_plan.json', {'spec': spec, 'groups': groups, 'repeats': repeats,
                       'prompt_versions': prompt_versions, 'registered_before_calls': True,
                       'provider': model_config.get('provider'), 'model': model_config.get('model'),
                       'prompt_registry': {'version': models.PROMPT_VERSION, 'sha256': models.TEMPLATE_HASH,
                                           'templates': models.TEMPLATES, 'system': models.SYSTEM},
                       'planned_prompts': [{'group_id': g['group_id'], 'version': v + 1,
                                            'text': models.build_prompt(spec, g, v)}
                                           for g in groups for v in range(prompt_versions)]})
            planned_calls = len(groups) * repeats * prompt_versions
            trace.emit('model', 'running', 'Measure the model independently with registered English prompts that contain no survey estimates or individual records.',
                       {'groups':len(groups), 'repeats':repeats, 'prompt_versions':prompt_versions,
                        'planned_calls':planned_calls, 'model':model_config.get('model')})
            result['measurement'] = models.measure(spec, groups, model_config, repeats, prompt_versions)
            write_json(directory / 'model_calls.json', result['measurement'])
            result['comparison'] = compare(rows, result['measurement'].get('rows', []))
            measured_status = result['measurement'].get('status')
            trace.emit('model', 'complete' if measured_status in ('ok', 'partial') else 'blocked',
                       result['measurement'].get('message', 'Model calls finished. Raw responses and failures were retained.'),
                       {'valid_calls':result['measurement'].get('valid_calls', 0), 'planned_calls':planned_calls, 'status':measured_status})
        measured = {r['group_id']: r for r in result['measurement'].get('rows', [])}
        result['gradient_comparison'] = []
        for contrast in survey.get('gradient', {}).get('contrasts', []):
            target = measured.get(contrast['group_id'], {})
            reference = measured.get(contrast['reference_id'], {})
            if contrast.get('status') == 'ok' and target.get('mean') is not None and reference.get('mean') is not None:
                model_gap = target['mean'] - reference['mean']
                result['gradient_comparison'].append({
                    'group_id': contrast['group_id'], 'reference_id': contrast['reference_id'],
                    'label': contrast['label'], 'survey_gradient_pp': contrast['estimate'] * 100,
                    'model_gradient_pp': model_gap * 100,
                    'gradient_gap_pp': (model_gap - contrast['estimate']) * 100,
                    'note': 'Difference between the two group model means. See the measurement record for valid call counts and variation across prompt versions.'})
        result['status'] = 'ok'
        result['answer'] = self._explain(result)
        return self._finish(directory, result, trace)

    def _explain(self, result):
        rows = [r for r in result['survey'].get('rows', []) if r.get('status') == 'ok']
        lines = ['The registered NHIS 2024 analysis is complete. Denominators include adults aged 25–64 in the requested domain with valid outcome and relevant grouping variables.']
        for row in rows[:6]:
            lines.append('{}: {:.1f}% (95% CI {:.1f}–{:.1f}%), unweighted n={}.'.format(row['label'],row['estimate']*100,row['ci_low']*100,row['ci_high']*100,row['n']))
        regression = result['survey'].get('regression', {})
        if regression.get('status') == 'ok':
            lines.append('A complex-survey logistic regression was fitted: ' + regression.get('formula', '') + '. See the regression table for adjusted odds ratios and intervals; they are not risk ratios or causal effects.')
        for contrast in result['survey'].get('gradient', {}).get('contrasts', [])[:3]:
            if contrast.get('status') == 'ok':
                lines.append('{}: {:+.1f} percentage points (95% CI {:+.1f}–{:+.1f}).'.format(contrast['label'], 100 * contrast['estimate'], 100 * contrast['ci_low'], 100 * contrast['ci_high']))
        if result['comparison']:
            worst = result['comparison'][0]
            lines.append('The largest model–survey mean gap was for {}: {:+.1f} percentage points.'.format(worst['label'],worst['gap_pp']))
        elif result['measurement'].get('status') != 'not_requested':
            lines.append('Model measurement has not produced valid comparison values. Check the call records and server configuration.')
        else:
            lines.append('No independent model measurement was requested for this run. The survey benchmark is shown; enable independent benchmarking to measure model–survey gaps.')
        suppressed = sum(r.get('status') == 'suppressed' for r in result['survey'].get('rows', []))
        if suppressed:
            lines.append('{} groups failed release checks and their numerical estimates were suppressed.'.format(suppressed))
        lines.append('R/Stata agreement and external NCHS comparisons remain pending. These results describe cross-sectional associations; fair or poor self-rated health is not a clinical diagnosis.')
        return '\n'.join(lines)

    def _finish(self, directory, result, trace=None):
        if trace:
            trace.emit('report', 'running', 'Save the analysis specification, observed calculation steps, results, and reproducibility records.')
        run_id = result['run_id']
        result['artifacts'] = {'json': '/api/runs/' + run_id}
        if result['status'] == 'ok':
            result['artifacts'].update({'report': '/api/runs/' + run_id + '/report', 'csv': '/api/runs/' + run_id + '/csv'})
            (directory / 'report.md').write_text(markdown_report(result), encoding='utf-8')
            (directory / 'estimates.csv').write_text(csv_report(result), encoding='utf-8')
        if trace:
            trace.emit('report', 'complete', 'The run record was saved.' if result['status'] != 'ok' else 'The aggregate statistical report and data table were saved and are available to inspect or export.',
                       {'run_id':run_id, 'report_released':result['status'] == 'ok'})
            result['process'] = list(trace.events)
        write_json(directory / 'result.json', result)
        return result
