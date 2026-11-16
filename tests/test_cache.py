import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent.cache import SurveyCache
from nhis_agent.engine import Agent

RUNTIME={'ready':True,'r_version':'4.5','survey_version':'4.5','jsonlite_version':'2','mitools_version':'2.4'}
SPEC={'year':2024,'analysis':'proportion','group_by':'education','filters':{},'outcome':'fair_poor'}
DATA={'ready':True,'row_count':32629,'sha256':'a'*64,'datasets':{'income':{'sha256':'b'*64}},'income_ready':True}
SURVEY={'status':'ok','rows':[{'group_id':'bachelors_plus','label':'Bachelor','status':'ok','n':1000,
        'estimate':.2,'se':.01,'ci_low':.18,'ci_high':.22}]}


class CacheTests(unittest.TestCase):
    def test_identity_invalidates_on_data_scope_or_r_version(self):
        with tempfile.TemporaryDirectory() as temporary,patch('nhis_agent.cache.stats.readiness',return_value=RUNTIME):
            cache=SurveyCache(temporary)
            base=cache.identity(SPEC,DATA)
            self.assertNotEqual(base['key'],cache.identity(SPEC,dict(DATA,sha256='c'*64))['key'])
            self.assertNotEqual(base['key'],cache.identity(dict(SPEC,filters={'sex':'female'}),DATA)['key'])
            with patch('nhis_agent.cache.stats.readiness',return_value=dict(RUNTIME,survey_version='4.6')):
                self.assertNotEqual(base['key'],cache.identity(SPEC,DATA)['key'])
            cache.save(base,SURVEY,'original-run')
            self.assertEqual(cache.read(base)['source_run_id'],'original-run')
            path=cache.directory/(base['key']+'.json')
            tampered=json.loads(path.read_text());tampered['survey']['rows'][0]['estimate']=.9
            path.write_text(json.dumps(tampered))
            self.assertIsNone(cache.read(base))
            path.write_text('[]')
            self.assertIsNone(cache.read(base))

    def test_only_validated_survey_cached_and_measurement_always_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent=Agent(Path(temporary))
            measurement={'status':'ok','rows':[{'group_id':'bachelors_plus','mean':.3,'valid_count':1,'total_count':1}],
                         'valid_calls':1}
            with patch('nhis_agent.engine.data.status',return_value=DATA), \
                 patch('nhis_agent.cache.stats.readiness',return_value=RUNTIME), \
                 patch('nhis_agent.engine.stats.analyze',return_value=SURVEY) as calculate, \
                 patch('nhis_agent.engine.models.measure',return_value=measurement) as model:
                first=agent.analyze_spec(SPEC,'按教育比较健康',True,1,1)
                second=agent.analyze_spec(SPEC,'再看看同样教育组',True,1,1)
            self.assertEqual(calculate.call_count,1)
            self.assertEqual(model.call_count,2)
            self.assertFalse(first['cache']['hit'])
            self.assertTrue(second['cache']['hit'])
            self.assertEqual(second['cache']['source_run_id'],first['run_id'])
            self.assertTrue(any(e['facts'].get('cache_hit') for e in second['process']))
            self.assertEqual(second['survey']['rows'][0]['estimate'],.2)


if __name__ == '__main__':unittest.main()
