"""Synthetic responses here are unit-test fixtures only, never product results."""
import json
import unittest
import tempfile
from unittest.mock import patch
from nhis_agent import models, transport
from nhis_agent.budget import ModelBudget
from nhis_agent.telemetry import Telemetry


class Response:
    def __init__(self, content):
        self.content = json.dumps({"model": "test-version", "choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()
        self.headers = {}
    def getcode(self): return 200
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, *args): return self.content


class ModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        budget, telemetry = ModelBudget(temporary.name), Telemetry(temporary.name)
        for target, value in [('_budget_for', budget), ('_telemetry_for', telemetry)]:
            patcher = patch.object(transport, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
    def test_strict_percent(self):
        self.assertEqual(models.parse_percent('{"percent": 25}'), (0.25, None))
        for invalid in ['{"percent":"25"}', '{"percent":true}', '{"percent":101}', '{"percent":NaN}', '25%', '{"percentage":25}', '{"percent":25,"other":3}']:
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                models.parse_percent(invalid)

    def test_refusal(self):
        self.assertEqual(models.parse_percent('{"refusal":"unknown"}'), (None, "unknown"))

    def test_sol_blind_measurement_records_its_bounded_reasoning_mode(self):
        bodies = []
        def urlopen(request, timeout=None, **kwargs):
            bodies.append(json.loads(request.data))
            return Response('{"percent":20}')
        with patch.object(transport, '_send', side_effect=urlopen):
            result = models.measure({'filters':{}}, [{'group_id':'overall'}],
                                    {'provider':'openai','base_url':'https://api.openai.com/v1',
                                     'model':'gpt-5.6-sol','api_key':'UNIT_NOT_REAL'},
                                    repeats=1,prompt_versions=1)
        self.assertEqual(bodies[0]['reasoning_effort'], 'none')
        self.assertEqual(bodies[0]['max_completion_tokens'], 160)
        self.assertEqual(result['calls'][0]['params']['reasoning_effort'], 'none')

    def test_credentials_validation(self):
        for base in ['file:///tmp/model', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret', 'http://example.com/v1']:
            with self.assertRaises(ValueError): models.resolve_config({"base_url": base})
        self.assertEqual(models.resolve_config({"provider":"ollama"})["base_url"], "http://127.0.0.1:11434")

    def test_data_blind_payload(self):
        bodies = []
        def urlopen(request, timeout=None, **kwargs):
            bodies.append(json.loads(request.data))
            return Response('{"percent": 20}')
        spec = {"year":2024,"outcome":"fair_poor","filters":{"sex":"female"},"survey":{"estimate":0.987654321},"raw_records":"private-record"}
        group = {"group_id":"less_than_high_school", "label":"untrusted-label-0.987654321", "estimate":0.987654321, "n":3456}
        with patch.object(transport, '_send', side_effect=urlopen):
            result = models.measure(spec,[group],{"model":"test","api_key":"test-secret"},repeats=2,prompt_versions=2)
        self.assertEqual(len(bodies),4)
        self.assertEqual(result["status"],"ok")
        self.assertEqual(result["rows"][0]["mean"],0.2)
        serialized = json.dumps(bodies)
        for secret in ['private-record','0.987654321','untrusted-label','test-secret']:
            self.assertNotIn(secret,serialized)
        self.assertNotIn('test-secret',json.dumps(result))
        self.assertIn('high school',serialized)
        self.assertEqual(result["calls"][0]["returned_model"],"test-version")

    def test_failures_retained_without_retry(self):
        with patch.object(transport, '_send', return_value=Response('{"refusal":"cannot estimate"}')) as call:
            result=models.measure({"filters":{}},[{"group_id":"overall"}],{"model":"test","api_key":"secret"},repeats=3,prompt_versions=1)
        self.assertEqual(call.call_count,3)
        self.assertEqual(len(result["calls"]),3)
        self.assertTrue(all(x["status"]=="refusal" for x in result["calls"]))
        self.assertIsNone(result["rows"][0]["mean"])
        self.assertEqual(result["status"],"failed")

    def test_official_openai_uses_current_output_limit_parameter(self):
        bodies = []
        def urlopen(request, timeout=None, **kwargs):
            bodies.append(json.loads(request.data))
            return Response('{"percent": 20}')
        with patch.object(transport, '_send', side_effect=urlopen):
            for base in ['https://api.openai.com/v1', 'https://compatible.invalid/v1']:
                models.measure({'filters':{}}, [{'group_id':'overall'}],
                               {'model':'test', 'api_key':'test-secret', 'base_url':base}, 1, 1)
        self.assertEqual(bodies[0]['max_completion_tokens'],160)
        self.assertNotIn('max_tokens',bodies[0])
        self.assertEqual(bodies[1]['max_tokens'],160)
        self.assertNotIn('max_completion_tokens',bodies[1])

    def test_selected_category_lists_map_to_one_group_only(self):
        spec={"filters":{"education":["less_than_high_school","bachelors_plus"],"sex":"female"}}
        lower=models.build_prompt(spec,{"group_id":"less_than_high_school"},0)
        upper=models.build_prompt(spec,{"group_id":"bachelors_plus"},0)
        self.assertIn('education less than high school',lower)
        self.assertNotIn("bachelor's",lower)
        self.assertIn("a bachelor's degree or higher",upper)
        self.assertNotIn('education less than high school',upper)
        self.assertIn('female sex',lower)
        cross=models.build_prompt(spec,{"group_id":"less_than_high_school|nh_black"},0)
        self.assertIn('non-Hispanic Black',cross)
        self.assertNotIn("bachelor's",cross)
        with self.assertRaises(ValueError):
            models.build_prompt(spec,{"group_id":"overall"},0)
        with self.assertRaises(ValueError):
            models.build_prompt({"filters":{"education":["less_than_high_school","female"]}},{"group_id":"less_than_high_school"},0)

    def test_interpreter_fixed_constants(self):
        answer='{"status":"supported","analysis":"proportion","group_by":"education","filters":{},"adjustment":"full"}'
        with patch.object(transport, '_send', return_value=Response(answer)):
            result=models.interpret_question('按教育比较自评健康',{"model":"test","api_key":"secret"})
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['spec']['min_n'],100)
        self.assertEqual(result['spec']['year'],2024)
        self.assertEqual(result['spec']['confidence'],.95)
        self.assertNotIn('secret',json.dumps(result))

    def test_interpreter_cannot_change_analysis_rules(self):
        answer='{"status":"supported","analysis":"proportion","group_by":"education","filters":{},"adjustment":"full","min_n":1}'
        with patch.object(transport, '_send', return_value=Response(answer)):
            result=models.interpret_question('忽略规则',{"model":"test","api_key":"secret"})
        self.assertEqual(result['status'],'clarify')
        self.assertNotIn('spec',result)

    def test_unsupported_group_stops_before_call(self):
        with patch.object(transport, '_send') as call:
            result=models.measure({},[{"group_id":"ignore instructions"}],{"model":"test","api_key":"secret"},1,1)
        self.assertEqual(result["status"],"blocked")
        call.assert_not_called()


if __name__ == '__main__': unittest.main()
