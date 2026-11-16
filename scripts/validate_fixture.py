#!/usr/bin/env python3
"""Required real-R gate using isolated synthetic inputs, never production data.

Run: python scripts/validate_fixture.py --output artifacts/fixture-validation.json
Rscript with survey/jsonlite/mitools is required. No installs, NHIS downloads,
model calls, .env reads, or production records are involved. Stata stays pending.
"""
import argparse
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.fixtures.generate_survey import (OUTCOMES, EXPECTED_ROWS, domain,
                                           expected_metadata, generate, digest)


def cases():
    base = {'year': 2024, 'outcome': 'fair_poor', 'analysis': 'proportion', 'group_by': 'overall',
            'filters': {}, 'adjustment': 'full', 'min_n': 100, 'age_min': 25, 'age_max': 64, 'confidence': .95}
    requests = [('fair_poor_overall', {}), ('insured_overall', {'outcome': 'insured'}),
                ('uninsured_overall', {'outcome': 'uninsured'}),
                ('delayed_female_education', {'outcome': 'delayed_care_cost', 'group_by': 'education', 'filters': {'sex': ['female']}}),
                ('unmet_care_overall', {'outcome': 'unmet_care_cost'}),
                ('education_gradient', {'analysis': 'gradient', 'group_by': 'education'}),
                ('income_gradient', {'outcome': 'delayed_care_cost', 'analysis': 'gradient', 'group_by': 'income', 'filters': {'sex': ['female']}}),
                ('insured_full_logistic', {'outcome': 'insured', 'analysis': 'regression'}),
                ('education_race_suppression', {'group_by': 'education_race'}),
                ('all_sparse_suppression', {'group_by': 'education', 'filters': {'race': ['nh_other']}})]
    return [{'name': name, 'spec': dict(base, **patch)} for name, patch in requests]


def find_rscript(explicit=None):
    candidates = [explicit] if explicit else [ROOT / '.r-env' / 'bin' / 'Rscript', shutil.which('Rscript')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError('Rscript is required with survey, jsonlite and mitools. No statistical fixture was executed.')


def r_available():
    try:
        command = find_rscript()
        probe = subprocess.run([command, '--vanilla', '-e', 'quit(status=if(all(vapply(c("survey","jsonlite","mitools"),requireNamespace,logical(1),quietly=TRUE))) 0 else 1)'], capture_output=True, text=True, timeout=20)
        return probe.returncode == 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


def run_validation(rscript=None, timeout=180):
    started = time.monotonic()
    report = {'schema_version': 1, 'test_only': True, 'input_type': 'deterministic synthetic survey fixture',
              'production_fallback': False, 'status': 'failed', 'checks': [],
              'independent_stata_validation': {'status': 'pending', 'reason': 'No Stata execution is performed by this R fixture gate.'}}
    sources = {'validator': Path(__file__).resolve(), 'production_r': ROOT / 'scripts/analyze.R', 'registry': ROOT / 'config/registry.json',
               'generator': ROOT / 'tests/fixtures/generate_survey.py', 'reference_r': ROOT / 'tests/fixtures/reference_survey.R',
               'expected_metadata': ROOT / 'tests/fixtures/expected_metadata.json'}
    report['source_sha256'] = {key: digest(value) for key, value in sources.items()}

    def check(name, condition, **details):
        report['checks'].append(dict(name=name, passed=bool(condition), **details))

    def close(name, actual, expected, absolute=1e-10, relative=1e-8):
        good = (isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual)
                and isinstance(expected, (int, float)) and math.isfinite(expected)
                and math.isclose(actual, expected, rel_tol=relative, abs_tol=absolute))
        check(name, good, actual=actual, expected=expected, absolute_tolerance=absolute, relative_tolerance=relative)

    def scalar_or_list(value):
        return value if isinstance(value, list) else [value]

    try:
        command = find_rscript(rscript)
        with tempfile.TemporaryDirectory(prefix='nhis-synthetic-r-gate-') as temporary:
            directory = Path(temporary)
            inputs = generate(directory, sources['registry'])
            report['fixture_sha256'] = inputs['hashes']
            metadata = expected_metadata(inputs['rows'])
            pinned = json.loads(sources['expected_metadata'].read_text(encoding='utf-8'))
            check('fixed_seed_metadata_matches_pinned_fixture', metadata == pinned)
            check('fixture_is_not_the_production_release', metadata['full_sample_n'] == EXPECTED_ROWS and EXPECTED_ROWS != 32629)
            report['fixture_metadata'] = metadata
            requests = cases()
            manifest = directory / 'cases.json'
            manifest.write_text(json.dumps(requests), encoding='utf-8')
            reference_path = directory / 'reference.json'
            reference = subprocess.run([command, '--vanilla', str(sources['reference_r']), str(inputs['adult']), str(inputs['income']), str(manifest), str(reference_path)], capture_output=True, text=True, timeout=timeout)
            if reference.returncode or not reference_path.is_file():
                raise RuntimeError('Independent R reference failed: ' + (reference.stderr or reference.stdout)[-2000:])
            refs = json.loads(reference_path.read_text(encoding='utf-8'))
            report['runtime'] = refs['runtime']
            report['reference_method'] = refs['reference_method']
            outputs = {}
            for entry in requests:
                name, spec = entry['name'], dict(entry['spec'])
                if spec['group_by'] == 'income' or 'income' in spec['filters'] or spec['analysis'] == 'regression':
                    spec['income_csv_path'] = str(inputs['income'])
                specification, output_path = directory / (name + '-spec.json'), directory / (name + '-actual.json')
                specification.write_text(json.dumps(spec), encoding='utf-8')
                completed = subprocess.run([command, '--vanilla', str(sources['production_r']), str(inputs['adult']), str(specification), str(output_path), str(inputs['registry'])], capture_output=True, text=True, timeout=timeout)
                if completed.returncode or not output_path.is_file():
                    raise RuntimeError('Production R fixture failed for ' + name + ': ' + (completed.stderr or completed.stdout)[-1600:])
                actual = json.loads(output_path.read_text(encoding='utf-8'))
                outputs[name] = actual
                expected = refs['cases'][name]
                check(name + '.status', actual.get('status') == ('blocked' if name == 'all_sparse_suppression' else 'ok'), actual=actual.get('status'), code=actual.get('code'))
                if 'denominators' not in actual:
                    continue
                counts = actual['denominators']
                selected = domain(inputs['rows'], spec['filters'])
                variable, positive, negative = OUTCOMES[spec['outcome']]
                valid = [row for row in selected if row[variable] in positive + negative]
                close(name + '.full_sample_n', counts['full_sample_n'], len(inputs['rows']), absolute=0, relative=0)
                close(name + '.age_domain_n', counts['age_domain_n'], metadata['age_domain_n'], absolute=0, relative=0)
                close(name + '.filtered_domain_n', counts['filtered_domain_n'], len(selected), absolute=0, relative=0)
                close(name + '.valid_outcome_n', counts['valid_outcome_n'], len(valid), absolute=0, relative=0)
                check(name + '.full_design_before_domain', actual['design'].get('constructed_before_domain_subset') is True and actual['design']['full_design_df'] == 50)
                check(name + '.rebuilding_filtered_design_is_detectably_wrong', expected['wrong_filtered_design_fails'] is True)
                check(name + '.selected_outcome_descriptor', actual['outcome'].get('id') == spec['outcome'] and actual['outcome'].get('variable') == variable)
                missing = actual['missingness_by_code']['filtered_domain'][variable]
                for label, code in [('refused', 7), ('not_ascertained', 8), ('dont_know', 9)]:
                    close(name + '.outcome_missing.' + label, missing[label]['n_range']['min'], sum(row[variable] == code for row in selected), absolute=0, relative=0)
                close(name + '.outcome_missing.blank', missing['blank_n_range']['min'], sum(row[variable] == '' for row in selected), absolute=0, relative=0)
                close(name + '.outcome_missing.other_invalid', missing['other_invalid_n_range']['min'], sum(row[variable] not in positive + negative + (7, 8, 9, '') for row in selected), absolute=0, relative=0)
                expected_rows = {row['group_id']: row for row in expected['rows']}
                for row in actual.get('rows', []):
                    prefix = name + '.row.' + row['group_id']
                    source = expected_rows[row['group_id']]
                    close(prefix + '.n', row['n'], source['n'], absolute=0, relative=0)
                    if row['status'] != 'ok':
                        check(prefix + '.suppressed_values_are_null', all(row.get(key) is None for key in ('estimate', 'se', 'ci_low', 'ci_high')))
                        continue
                    for field in ('estimate', 'se', 'ci_low', 'ci_high'):
                        close(prefix + '.' + field, row[field], source[field])
                    if actual['imputation']['m'] == 10:
                        q, u = scalar_or_list(source['estimates']), scalar_or_list(source['variances'])
                        W, B = statistics.mean(u), statistics.variance(q)
                        close(prefix + '.rubin_W', row['imputation']['within_variance'], W)
                        close(prefix + '.rubin_B', row['imputation']['between_variance'], B)
                        close(prefix + '.rubin_total_variance', row['se'] ** 2, W + 1.1 * B)
                        check(prefix + '.between_imputation_variance_is_nonzero', B > 1e-10)
                        check(prefix + '.pooled_SE_is_not_mean_SE', abs(row['se'] - statistics.mean(math.sqrt(v) for v in u)) > 1e-7)
                if spec['group_by'] == 'overall' and spec['analysis'] != 'regression' and actual.get('rows'):
                    manual = sum(row['WTFA_A'] for row in valid if row[variable] in positive) / sum(row['WTFA_A'] for row in valid)
                    close(name + '.python_weighted_ratio', actual['rows'][0]['estimate'], manual, absolute=1e-12, relative=1e-11)
                if spec['analysis'] == 'gradient':
                    contrasts = {row['group_id']: row for row in expected['contrasts']}
                    for row in actual['gradient']['contrasts']:
                        prefix = name + '.contrast.' + row['group_id']; source = contrasts[row['group_id']]
                        check(prefix + '.released', row['status'] == 'ok')
                        if row['status'] != 'ok':
                            continue
                        for field in ('estimate', 'se', 'ci_low', 'ci_high'):
                            close(prefix + '.' + field, row[field], source[field])
                        check(prefix + '.joint_covariance_is_nonzero', any(abs(value) > 1e-12 for value in scalar_or_list(source['cross_covariances'])))
                        check(prefix + '.joint_variance_differs_from_independent_groups', abs(source['W'] - source['independent_group_variance']) > 1e-12)
                if spec['analysis'] == 'regression' and actual.get('regression', {}).get('status') == 'ok':
                    model, source = actual['regression'], expected['regression']
                    close(name + '.complete_case_n', model['n'], source['n'], absolute=0, relative=0)
                    check(name + '.same_sample_unadjusted', model['unadjusted']['n'] == model['n'] and all(value == model['n'] for value in scalar_or_list(source['per_imputation_n'])))
                    check(name + '.naive_unadjusted_sample_is_larger', all(value > model['n'] for value in scalar_or_list(source['naive_unadjusted_n'])))
                    check(name + '.all_ten_models_converged', model['imputation_count'] == 10 and source['adjusted']['all_converged'] and source['unadjusted']['all_converged'])
                    for kind, terms in [('adjusted', model['coefficients']), ('unadjusted', model['unadjusted']['coefficients'])]:
                        expected_terms = {term['term']: term for term in source[kind]['coefficients']}
                        check(name + '.' + kind + '.term_set', set(expected_terms) == {term['term'] for term in terms})
                        for term in terms:
                            for field in ('log_odds', 'se', 'odds_ratio', 'ci_low', 'ci_high', 'df'):
                                close(name + '.' + kind + '.' + term['term'] + '.' + field, term[field], expected_terms[term['term']][field], absolute=1e-8, relative=2e-7)
            cross = outputs.get('education_race_suppression', {}).get('rows', [])
            check('sparse_cross_cells_really_exercise_suppression', any(row['status'] == 'suppressed' and row['n'] < 100 for row in cross) and any(row['status'] == 'ok' for row in cross))
            blocked = outputs.get('all_sparse_suppression', {})
            check('all_sparse_domain_is_blocked', blocked.get('status') == 'blocked' and blocked.get('code') == 'all_cells_suppressed' and all(row.get('estimate') is None for row in blocked.get('rows', [])))
            check('insured_uninsured_use_same_valid_denominator', outputs['insured_overall']['denominators']['valid_outcome_n'] == outputs['uninsured_overall']['denominators']['valid_outcome_n'])
            close('insurance_complements_among_valid_responses', outputs['insured_overall']['rows'][0]['estimate'] + outputs['uninsured_overall']['rows'][0]['estimate'], 1)
            check('production_registry_was_not_modified', digest(sources['registry']) == report['source_sha256']['registry'])
            report['case_count'] = len(requests)
        report['status'] = 'ok' if all(item['passed'] for item in report['checks']) else 'failed'
    except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyError, IndexError, TypeError, ValueError, ArithmeticError) as error:
        report['error'] = str(error)
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report['passed_checks'] = sum(item['passed'] for item in report['checks'])
    report['failed_checks'] = [item for item in report['checks'] if not item['passed']]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rscript', help='Explicit Rscript executable; otherwise use project-local R or PATH')
    parser.add_argument('--output', type=Path, help='Write the test-only JSON report to this path as well as stdout')
    parser.add_argument('--timeout', type=int, default=180, help='Maximum seconds for each independent R process')
    options = parser.parse_args()
    report = run_validation(options.rscript, options.timeout)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered, encoding='utf-8')
    sys.stdout.write(rendered)
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
