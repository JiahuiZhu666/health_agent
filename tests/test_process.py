import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent.engine import Agent
from nhis_agent.process import ProcessTrace, describe_spec

SPEC = {'year':2024,'outcome':'fair_poor','analysis':'proportion','group_by':'education',
        'filters':{'sex':'female'},'min_n':100,'age_min':25,'age_max':64,'confidence':.95,'adjustment':'full'}
SURVEY = {'status':'ok','rows':[{'group_id':'bachelors_plus','label':'Bachelor or higher', 'n':1000,
           'estimate':.1,'se':.01,'ci_low':.08,'ci_high':.12,'status':'ok'}],
          'denominators':{'full_sample_n':32629,'filtered_domain_n':1000}}


class ProcessTests(unittest.TestCase):
    def test_registered_spec_has_human_readable_scope(self):
        plan = describe_spec(SPEC)
        self.assertEqual(plan['group_by'],'Education')
        self.assertEqual(plan['filters'],['Sex: Female'])
        self.assertEqual(plan['minimum_n'],100)

    def test_callback_journal_and_result_match_real_stage_changes(self):
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch('nhis_agent.engine.data.status',return_value={'ready':True,'row_count':32629}), \
                 patch('nhis_agent.engine.stats.analyze',return_value=SURVEY), \
                 patch('nhis_agent.engine.models.measure') as measurement:
                result = agent.analyze_spec(SPEC,'那女性呢',on_progress=events.append)
            self.assertEqual(result['status'],'ok')
            measurement.assert_not_called()
            self.assertEqual(events,result['process'])
            states = {(e['stage'],e['status']) for e in events}
            for phase in ['understand','scope','data','statistics','validation','report']:
                self.assertIn((phase,'complete'),states)
            self.assertIn(('model','skipped'),states)
            self.assertEqual(next(e for e in events if e['stage']=='data' and e['status']=='complete')['facts']['full_sample_n'],32629)
            directory = Path(temporary)/'runs'/result['run_id']
            journal = [json.loads(line) for line in (directory/'process.jsonl').read_text().splitlines()]
            self.assertEqual(journal,events)
            self.assertEqual(json.loads((directory/'result.json').read_text())['process'],events)
            self.assertNotIn('api_key',json.dumps(events))

    def test_missing_data_never_claims_statistics_or_validation_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch('nhis_agent.engine.data.status',return_value={'ready':False}), \
                 patch('nhis_agent.engine.stats.analyze') as calculate:
                result = agent.analyze_spec(SPEC,'按教育比较女性健康')
            self.assertEqual(result['status'],'blocked')
            calculate.assert_not_called()
            self.assertIn(('data','blocked'),{(e['stage'],e['status']) for e in result['process']})
            self.assertFalse(any(e['stage'] in ('statistics','validation') for e in result['process']))

    def test_statistical_failure_not_reported_as_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch('nhis_agent.engine.data.status',return_value={'ready':True}), \
                 patch('nhis_agent.engine.stats.analyze',return_value={'status':'blocked','reason':'Insufficient sample','rows':[]}):
                result = agent.analyze_spec(SPEC,'按教育比较女性健康')
            self.assertEqual(result['status'],'blocked')
            self.assertFalse(any(e['stage']=='validation' and e['status']=='complete' for e in result['process']))
            self.assertNotIn('report',result['artifacts'])

    def test_fixed_rules_validated_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch('nhis_agent.engine.stats.analyze') as calculate:
                result = agent.analyze_spec(dict(SPEC,year=2023),'上一年呢')
            self.assertEqual(result['status'],'blocked')
            calculate.assert_not_called()

    def test_disconnected_progress_observer_does_not_change_result(self):
        def broken(event):
            raise ConnectionError('browser disconnected')
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(Path(temporary))
            with patch('nhis_agent.engine.data.status',return_value={'ready':True}), \
                 patch('nhis_agent.engine.stats.analyze',return_value=SURVEY):
                result = agent.analyze_spec(SPEC,'按教育比较女性健康',on_progress=broken)
            self.assertEqual(result['status'],'ok')


if __name__ == '__main__':
    unittest.main()
