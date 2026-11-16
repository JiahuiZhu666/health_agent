"""Deterministic synthetic survey inputs exclusively for integration tests.

These records are never application fallback data or estimates of NHIS results.
The caller supplies an isolated output directory; no production data is read.
"""
import csv
import hashlib
import json
import math
from pathlib import Path
import random

SEED = 20261116
STRATA = 50
PSUS = 2
PER_PSU = 80
EXPECTED_ROWS = STRATA * PSUS * PER_PSU
OUTCOMES = {
    'fair_poor': ('PHSTAT_A', (4, 5), (1, 2, 3)),
    'insured': ('NOTCOV_A', (2,), (1,)),
    'uninsured': ('NOTCOV_A', (1,), (2,)),
    'delayed_care_cost': ('MEDDL12M_A', (1,), (2,)),
    'unmet_care_cost': ('MEDNG12M_A', (1,), (2,)),
}
FIELDS = ['SRVY_YR', 'RECTYPE', 'HHX', 'WTFA_A', 'PSTRAT', 'PPSU', 'AGEP_A',
          'PHSTAT_A', 'NOTCOV_A', 'HICOV_A', 'MEDDL12M_A', 'MEDNG12M_A', 'EDUCP_A', 'HISPALLP_A', 'SEX_A']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _missing(index, value, schedule):
    for divisor, code in schedule:
        if index % divisor == 0:
            return code
    return value


def records():
    rng = random.Random(SEED)
    rows, latent = [], []
    for stratum in range(STRATA):
        cluster_shift = rng.uniform(-.3, .3)
        for psu in range(1, PSUS + 1):
            for position in range(PER_PSU):
                index = len(rows) + 1
                age = rng.randint(18, 80)
                # A selected domain loses a PSU, while the full design has two.
                if stratum == 0:
                    age = rng.randint(65, 80) if psu == 1 else rng.randint(25, 64)
                education = rng.randrange(4)
                race = rng.choices([1, 2, 3, 4, 6], weights=[.21, .34, .21, .20, .04])[0]
                sex = 1 if stratum == 1 and psu == 1 else rng.choice([1, 2])
                income = rng.randrange(4)
                age_group = min(3, max(0, (age - 25) // 10))
                predictor = -.95 + .22 * (3 - education) + .12 * age_group + .13 * (sex == 1) + .10 * (race == 3) + cluster_shift + .12 * (psu == 1)
                bernoulli = lambda value: rng.random() < 1 / (1 + math.exp(-value))
                health = rng.choice([4, 5]) if bernoulli(predictor) else rng.choice([1, 2, 3])
                uninsured = bernoulli(-1.35 + .20 * (3 - education) + .30 * (3 - income) + .12 * (race == 3) + cluster_shift)
                delayed = bernoulli(-1.6 + .17 * (3 - education) + .33 * (3 - income) + .10 * (sex == 2) + cluster_shift)
                unmet = bernoulli(-1.8 + .16 * (3 - education) + .29 * (3 - income) + cluster_shift)
                row = dict(zip(FIELDS, [2024, 10, 'SYNTHETIC{:07d}'.format(index), round(1 + 4 * rng.random() + .6 * (stratum % 5) + .3 * psu, 6),
                    101 + stratum, psu, age, health, 1 if uninsured else 2,
                    1 if index % 11 == 0 else 2, 1 if delayed else 2, 1 if unmet else 2,
                    [2, 4, 6, 9][education], race, sex]))
                row['AGEP_A'] = _missing(index, age, [(191, 99)])
                row['EDUCP_A'] = _missing(index, row['EDUCP_A'], [(97, 98), (211, 97), (307, 99)])
                row['HISPALLP_A'] = _missing(index, race, [(173, 98), (337, 99)])
                row['SEX_A'] = _missing(index, sex, [(223, 7), (379, 9)])
                for variable, schedule in {
                    'PHSTAT_A': [(137, 7), (149, 8), (157, 9), (401, ''), (419, 0)],
                    'NOTCOV_A': [(127, 7), (163, 8), (179, 9), (409, ''), (443, 3)],
                    'MEDDL12M_A': [(131, 7), (167, 8), (181, 9), (397, ''), (431, 0)],
                    'MEDNG12M_A': [(139, 7), (169, 8), (193, 9), (389, ''), (433, 0)],
                }.items():
                    row[variable] = _missing(index, row[variable], schedule)
                rows.append(row)
                latent.append((income, rng.random() < .45))
    return rows, latent


def generate(directory, registry_path):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows, latent = records()
    adult = directory / 'synthetic-adult.csv'
    with adult.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
    income = directory / 'synthetic-income.csv'
    with income.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['RECTYPE', 'HHX', 'IMPNUM_A', 'RATCAT_A'], lineterminator='\n')
        writer.writeheader()
        for m in range(1, 11):
            rng = random.Random(SEED + 1000 * m)
            for row, (base, imputed) in zip(rows, latent):
                category = rng.randrange(4) if imputed and rng.random() < .5 else base
                writer.writerow({'RECTYPE': 30, 'HHX': row['HHX'], 'IMPNUM_A': m, 'RATCAT_A': [2, 5, 9, 13][category]})
    registry = json.loads(Path(registry_path).read_text(encoding='utf-8'))
    registry['datasets']['adult_2024']['expected_rows'] = len(rows)
    registry['datasets']['income_2024']['expected_rows'] = len(rows) * 10
    registry['_test_fixture_only'] = {'synthetic': True, 'seed': SEED, 'production_fallback': False}
    copied_registry = directory / 'fixture-registry.json'
    copied_registry.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return {'adult': adult, 'income': income, 'registry': copied_registry, 'rows': rows,
            'hashes': {'adult': digest(adult), 'income': digest(income), 'registry': digest(copied_registry)}}


def category(row, field):
    value = row[field]
    if field == 'EDUCP_A':
        return 'less_than_high_school' if value in (0, 1, 2) else 'high_school' if value in (3, 4) else 'some_college' if value in (5, 6, 7) else 'bachelors_plus' if value in (8, 9, 10) else None
    if field == 'HISPALLP_A':
        return {1: 'hispanic', 2: 'nh_white', 3: 'nh_black', 4: 'nh_asian', 5: 'nh_other', 6: 'nh_other', 7: 'nh_other'}.get(value)
    if field == 'SEX_A':
        return {1: 'male', 2: 'female'}.get(value)
    return None


def domain(rows, filters=None):
    fields = {'education': 'EDUCP_A', 'race': 'HISPALLP_A', 'sex': 'SEX_A'}
    return [row for row in rows if isinstance(row['AGEP_A'], int) and 25 <= row['AGEP_A'] <= 64
            and all(category(row, fields[key]) in (values if isinstance(values, list) else [values]) for key, values in (filters or {}).items())]


def expected_metadata(rows):
    selected = domain(rows)
    outcomes = {}
    for name, (variable, positive, negative) in OUTCOMES.items():
        valid = [row for row in selected if row[variable] in positive + negative]
        outcomes[name] = {'variable': variable, 'positive_codes': list(positive), 'negative_codes': list(negative),
                          'valid_outcome_n': len(valid), 'missing_outcome_n': len(selected) - len(valid),
                          'weighted_proportion': sum(row['WTFA_A'] for row in valid if row[variable] in positive) / sum(row['WTFA_A'] for row in valid)}
    return {'fixture_only': True, 'seed': SEED, 'full_sample_n': len(rows), 'age_domain_n': len(selected),
            'strata': STRATA, 'full_design_df': STRATA, 'income_imputations': 10, 'outcomes': outcomes}
