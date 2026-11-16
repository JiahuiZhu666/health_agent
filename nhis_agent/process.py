"""Observable execution events, not private model reasoning or invented progress."""
import datetime as dt
import json
import time
from pathlib import Path

LABELS = {
    'understand': 'Understand the question', 'scope': 'Confirm the research scope', 'data': 'Verify real data',
    'statistics': 'Run survey statistics', 'validation': 'Check result reliability',
    'model': 'Measure the model independently', 'report': 'Save evidence and report',
}
GROUP_LABELS = {'overall':'All adults','education':'Education','income':'Income group','race':'Race and ethnicity',
                'sex':'Sex','age':'Age group','education_race':'Education × race and ethnicity'}
CATEGORY_LABELS = {
    'less_than_high_school':'Less than high school','high_school':'High school / GED','some_college':'Some college / associate degree',
    'bachelors_plus':"Bachelor's degree or higher",'male':'Male','female':'Female','hispanic':'Hispanic or Latino',
    'nh_white':'Non-Hispanic White','nh_black':'Non-Hispanic Black','nh_asian':'Non-Hispanic Asian','nh_other':'Non-Hispanic other / multiple races',
    '25_34':'Ages 25–34','35_44':'Ages 35–44','45_54':'Ages 45–54','55_64':'Ages 55–64',
    'below_100':'Below 100% of the poverty threshold','100_199':'100% to <200% of the poverty threshold',
    '200_399':'200% to <400% of the poverty threshold','400_plus':'At least 400% of the poverty threshold',
}


def describe_spec(spec):
    filters = []
    for dimension, values in spec.get('filters', {}).items():
        if isinstance(values, str):
            values = [values]
        filters.append(GROUP_LABELS.get(dimension, dimension) + ': ' + ', '.join(CATEGORY_LABELS.get(value, value) for value in values))
    mode = {'proportion':'Survey-weighted proportions and 95% confidence intervals',
            'gradient':'Group prevalence differences and 95% confidence intervals',
            'regression':'Complex-survey logistic regression'}[spec.get('analysis', 'proportion')]
    return {'population':'U.S. civilian noninstitutionalized adults aged 25–64', 'year':2024,
            'outcome':'Fair or poor self-rated health (PHSTAT_A=4 or 5)',
            'group_by':GROUP_LABELS.get(spec.get('group_by', 'overall'), 'All adults'),
            'filters':filters, 'method':mode, 'confidence':'95%', 'minimum_n':100,
            'design':'Build the full adult survey design before subsetting domains; WTFA_A / PSTRAT / PPSU',
            'income':'Use and pool all 10 official income imputations when required; do not multiply sample counts by 10'}


class ProcessTrace:
    def __init__(self, callback=None):
        self.callback = callback
        self.events = []
        self._starts = {}
        self._path = None

    def attach(self, directory):
        self._path = Path(directory) / 'process.jsonl'
        with self._path.open('w', encoding='utf-8') as stream:
            for event in self.events:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')

    def emit(self, stage, status, detail='', facts=None):
        if status not in ('running', 'complete', 'blocked', 'failed', 'skipped'):
            raise ValueError('Unknown execution event state')
        now = time.monotonic()
        if status == 'running' and stage not in self._starts:
            self._starts[stage] = now
        event = {'stage':stage, 'label':LABELS.get(stage, stage), 'status':status,
                 'detail':detail, 'timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),
                 'facts':dict(facts or {})}
        if stage in self._starts:
            event['elapsed_ms'] = round((now - self._starts[stage]) * 1000)
        self.events.append(event)
        if self._path:
            with self._path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
        if self.callback:
            try:
                self.callback(dict(event))
            except Exception:
                # A disconnected presentation layer does not change a scientific result.
                pass
        return event
