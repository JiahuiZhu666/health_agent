"""Conversation fixtures are isolated unit data, never application research results."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from nhis_agent.conversation import ConversationService, _safe_facts, analysis_reply


class FakeAgent:
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.next_status = 'ok'

    def analyze_spec(self, spec, question, measure_model=False, repeats=5, prompt_versions=3, on_progress=None):
        self.calls.append(copy.deepcopy(spec))
        run_id = '20261115T120000Z-{:010x}'.format(len(self.calls))
        status = self.next_status
        self.next_status = 'ok'
        sex = spec.get('filters', {}).get('sex')
        estimate = .3 if sex == 'female' else .1 if sex == 'male' else .2
        group = 'less_than_high_school' if spec['group_by'] == 'education' else 'below_100' if spec['group_by'] == 'income' else 'overall'
        result = {'run_id':run_id,'status':status,'spec':copy.deepcopy(spec),
            'answer':'实际测试结果说明' if status == 'ok' else '本次单元样本不足，所有估计被抑制。',
            'survey':{'status':status,'rows':[{'group_id':group,'label':'测试群体','status':'ok','n':1000,
                'estimate':estimate,'se':.01,'ci_low':estimate-.02,'ci_high':estimate+.02}] if status=='ok' else [],
                'denominators':{'full_sample_n':32629,'age_domain_n':20000,'filtered_domain_n':1000,'valid_outcome_n':1000}},
            'measurement':{'status':'not_requested','calls':[{'raw_response':'RAW_CALL_NOT_FOR_CHAT_STORAGE'}]},
            'comparison':[],'process':[]}
        directory = self.root / 'runs' / run_id
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'result.json').write_text(json.dumps(result),encoding='utf-8')
        if on_progress:
            on_progress({'stage':'survey','label':'统计执行','status':'complete','detail':'隔离的测试执行事实','facts':{'n':1000}})
        return result


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.agent = FakeAgent(self.temporary.name)
        self.service = ConversationService(self.agent)
        self.id = self.service.create()['id']

    def tearDown(self):
        self.temporary.cleanup()

    def send(self, text, **kwargs):
        return self.service.send(self.id,text,**kwargs)

    def test_greeting_help_and_method_do_not_run_statistics(self):
        for question in ['hi','你能做什么','你是怎么算的？','95%置信区间是什么意思','自评健康是什么意思','什么是加权比例']:
            result = self.send(question)
            self.assertEqual(result['message']['status'],'ok')
            self.assertIsNone(result['result'])
        self.assertEqual(self.agent.calls,[])
        self.assertEqual(len(self.service.get(self.id)['messages']),12)

    def test_acknowledgment_does_not_recalculate_or_choose_pending_filter(self):
        self.send('按教育比较健康')
        self.send('只看低收入')
        before=len(self.agent.calls)
        answer=self.send('好的')
        self.assertEqual(answer['message']['kind'],'acknowledgment')
        self.assertEqual(len(self.agent.calls),before)
        self.assertIn('pending',answer['conversation']['context'])
        self.assertIsNone(answer['result'])
        reply=self.send('谢谢')
        self.assertEqual(reply['message']['status'],'ok')
        self.assertEqual(len(self.agent.calls),before)

    def test_filter_and_group_followups_preserve_context(self):
        self.send('按教育水平比较自评健康')
        female = self.send('那女性呢')
        self.assertEqual(female['conversation']['context']['spec']['group_by'],'education')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female'})
        self.send('按收入呢')
        self.assertEqual(self.agent.calls[-1]['group_by'],'income')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female'})
        self.send('换成男性')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'male'})
        self.assertEqual(self.agent.calls[-1]['group_by'],'income')
        self.send('取消性别筛选')
        self.assertEqual(self.agent.calls[-1]['filters'],{})
        self.send('那女性呢')
        self.send('回到总体')
        self.assertEqual(self.agent.calls[-1]['group_by'],'overall')
        self.assertEqual(self.agent.calls[-1]['filters'],{})

    def test_polite_and_short_category_followups_preserve_grouping(self):
        self.send('按教育比较自评健康的加权比例')
        for question, sex in [('请只看女性','female'),('帮我换成男性','male'),('女性呢','female'),('只看女性的比例','female')]:
            with self.subTest(question=question):
                self.send(question)
                self.assertEqual(self.agent.calls[-1]['group_by'],'education')
                self.assertEqual(self.agent.calls[-1]['filters']['sex'],sex)

    def test_common_interpretation_intents_do_not_recalculate(self):
        self.send('按教育比较健康')
        for question in ['这个差异说明什么','这意味着什么','总结一下','这个结果怎么看','展开讲讲','哪个群体最高或最低','统计显著吗']:
            with self.subTest(question=question):
                answer=self.send(question)
                self.assertEqual(answer['message']['status'],'ok')
                self.assertEqual(answer['message']['kind'],'explanation')
        self.assertEqual(len(self.agent.calls),1)
        self.assertRegex(self.send('统计显著吗')['message']['content'], r'no computed joint variance')

    def test_detail_followups_reuse_evidence_without_recalculating(self):
        result = self.send('按教育比较健康')
        run_id = result['result']['run_id']
        spec = result['conversation']['context']['spec']
        for question in ['详细一点', '计算步骤再说清楚一点', '具体计算过程',
                         '一步步解释', '继续解释', '解释步骤', 'more detail',
                         'step by step', 'Rubin规则是什么', '多重插补怎么做',
                         '优势比是什么意思', '回归系数怎么解释']:
            with self.subTest(question=question):
                reply = self.send(question)
                self.assertEqual(reply['message']['status'], 'ok')
                self.assertIn(reply['message']['kind'], ('method', 'explanation'))
                self.assertEqual(reply['message']['run_id'], run_id)
                self.assertEqual(reply['conversation']['context']['spec'], spec)
                self.assertNotIn(run_id, reply['message']['content'])
        self.assertEqual(len(self.agent.calls), 1)
        detailed = self.send('详细一点')['message']['content']
        for fact in ['20.00%', 'PHSTAT', 'WTFA', '95%']:
            self.assertIn(fact, detailed)

    def test_mixed_analysis_and_explanation_executes_the_requested_scope(self):
        first = self.send('按教育比较，并解释计算步骤')
        self.assertEqual(first['message']['kind'], 'analysis')
        self.assertEqual(self.agent.calls[-1]['group_by'], 'education')
        self.send('那女性呢')
        income = self.send('按收入组比较，并解释计算步骤')
        self.assertEqual(income['message']['kind'], 'analysis')
        self.assertEqual(self.agent.calls[-1]['group_by'], 'income')
        self.assertEqual(self.agent.calls[-1]['filters'], {'sex':'female'})
        self.assertEqual(len(self.agent.calls), 3)

    def test_mixed_request_retains_scope_after_the_explanation_clause(self):
        self.send('按收入比较，并解释结果，只看女性')
        self.assertEqual(self.agent.calls[-1]['filters'], {'sex':'female'})
        self.assertEqual(self.agent.calls[-1]['group_by'], 'income')
        other = self.service.create()['id']
        reply = self.service.send(other, '按教育比较，并解释结果，限定25–34岁')
        self.assertEqual(reply['message']['kind'], 'analysis')
        self.assertEqual(self.agent.calls[-1]['filters'], {'age':'25_34'})
        self.assertEqual(self.agent.calls[-1]['group_by'], 'education')

    def test_named_explanation_cannot_borrow_another_group_or_analysis(self):
        self.send('按教育比较健康')
        for question in ['解释收入梯度', '收入组哪个最高', '教育梯度结果怎么看',
                         '按收入组的置信区间怎么算']:
            with self.subTest(question=question):
                reply = self.send(question)
                self.assertEqual(reply['message']['status'], 'clarify')
                self.assertNotIn('20.00%', reply['message']['content'])
        self.assertEqual(len(self.agent.calls), 1)

    def test_method_questions_with_analysis_words_do_not_start_another_run(self):
        self.send('按教育比较健康')
        for question in ['按教育比较时用了哪些变量', '按教育比较时权重怎么用',
                         '按教育比较的计算步骤是什么', '按教育比较的详细步骤',
                         '按教育比较时样本有多少', '按教育分组用了哪些变量，并解释一下',
                         '按教育分组权重怎么用，并说明一下']:
            with self.subTest(question=question):
                reply = self.send(question, measure_model=True)
                self.assertEqual(reply['message']['status'], 'ok')
                self.assertIn(reply['message']['kind'], ('method', 'explanation'))
        self.assertEqual(len(self.agent.calls), 1)

    def test_chinese_adjacent_abbreviations_explain_without_calculation(self):
        for question in ['CI是什么意思？', 'OR是什么意思？', 'MI是什么意思？']:
            with self.subTest(question=question):
                reply = self.send(question)
                self.assertEqual(reply['message']['kind'], 'method')
                self.assertEqual(reply['message']['status'], 'ok')
                self.assertIsNone(reply['result'])
        self.assertEqual(self.agent.calls, [])

    def test_same_axis_filter_cleared_when_regrouping(self):
        self.send('未完成高中的成年人自评健康比例')
        self.assertEqual(self.agent.calls[-1]['filters'],{'education':'less_than_high_school'})
        self.send('按教育水平呢')
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertNotIn('education',self.agent.calls[-1]['filters'])

    def test_explanations_reuse_saved_result_and_report_real_source(self):
        result = self.send('按教育比较健康')
        run_id = result['result']['run_id']
        for question in ['解释一下','样本有多少？','置信区间是什么意思','为什么抑制','哪个偏差最大？']:
            response = self.send(question)
            self.assertEqual(response['message']['run_id'],run_id)
            self.assertEqual(response['result']['run_id'],run_id)
        self.assertEqual(len(self.agent.calls),1)
        self.assertRegex(self.send('样本有多少')['message']['content'], r'unweighted n=1,?000')
        self.assertIn('no valid model',self.send('哪个偏差最大')['message']['content'])

    def test_pending_income_threshold_keeps_previous_group(self):
        self.send('按教育比较健康')
        self.send('那女性呢')
        clarification = self.send('只看低收入呢')
        self.assertEqual(clarification['message']['status'],'clarify')
        self.assertEqual(len(self.agent.calls),2)
        pending = clarification['conversation']['context']['pending']
        self.assertEqual(pending['spec']['group_by'],'education')
        resolved = self.send('低于贫困线100%')
        self.assertEqual(resolved['message']['status'],'ok')
        self.assertEqual(self.agent.calls[-1]['group_by'],'education')
        self.assertEqual(self.agent.calls[-1]['filters'],{'sex':'female','income':'below_100'})
        self.assertNotIn('pending',resolved['conversation']['context'])

    def test_numeric_threshold_reply_is_only_resolved_after_clarification(self):
        self.send('按教育比较健康')
        self.send('只看低收入')
        resolved = self.send('低于100%')
        self.assertEqual(resolved['message']['status'],'ok')
        self.assertEqual(self.agent.calls[-1]['filters'],{'income':'below_100'})
        other = self.service.create()['id']
        self.assertEqual(self.service.send(other,'低于100%')['message']['status'],'clarify')

    def test_explanation_cannot_relabel_a_different_population(self):
        self.send('按教育比较健康')
        self.send('那女性呢')
        result = self.send('男性样本有多少？')
        self.assertEqual(result['message']['status'],'clarify')
        self.assertIn('different population',result['message']['content'])
        self.assertEqual(len(self.agent.calls),2)

    def test_model_method_without_measurement_does_not_invent_a_model(self):
        result = self.send('模型是如何提问的？')
        self.assertEqual(result['message']['kind'],'method')
        self.assertRegex(result['message']['content'], r'no valid model-measurement record')
        self.assertEqual(self.agent.calls,[])

    def test_registered_rule_questions_explain_without_recalculating(self):
        questions=['你用了哪些变量','权重怎么用','为什么用加权','怎么筛选样本','数据来源是什么',
                   '为什么只看25–64岁','为什么样本不足100不显示']
        for question in questions:
            with self.subTest(question=question):
                reply=self.send(question)
                self.assertEqual(reply['message']['status'],'ok')
                self.assertEqual(reply['message']['kind'],'method')
                self.assertIsNone(reply['result'])
        self.assertEqual(self.agent.calls,[])

    def test_rule_explanation_never_permits_an_actual_override(self):
        for question in ['把下限改成10','为什么样本不足100不显示，把下限改成10',
                         '为什么只看25–64岁，换成中国成年人','数据来源改成2023年可以吗']:
            with self.subTest(question=question):
                self.assertEqual(self.send(question)['message']['status'],'blocked')
        self.assertEqual(self.agent.calls,[])

    def test_chat_keeps_run_reference_in_metadata_only(self):
        analysis=self.send('按教育比较健康')
        run_id=analysis['result']['run_id']
        for reply in [analysis,self.send('解释一下'),self.send('样本有多少')]:
            self.assertEqual(reply['message']['run_id'],run_id)
            self.assertEqual(reply['message']['source']['run_id'],run_id)
            self.assertNotIn(run_id,reply['message']['content'])

    def test_detailed_analysis_uses_only_published_values_and_partial_status(self):
        rows=[{'group_id':'less_than_high_school','label':'未完成高中','status':'ok','estimate':.321,'ci_low':.30,'ci_high':.34,'n':1234},
              {'group_id':'bachelors_plus','label':'本科及以上','status':'ok','estimate':.123,'ci_low':.11,'ci_high':.14,'n':4567},
              {'group_id':'other','label':'未发布群体','status':'suppressed','estimate':.99,'n':4}]
        result={'status':'ok','run_id':'20261115T120000Z-0000000001','answer':'不应出现在简短答复中的整段计算日志\n'*100,
                'survey':{'status':'ok','rows':rows},'measurement':{'status':'partial','valid_calls':4,'planned_calls':15}}
        reply=analysis_reply(result,{'group_by':'education','filters':{'sex':'female'}})
        self.assertIn('###',reply)
        self.assertIn('Female',reply)
        self.assertIn('32.10%',reply)
        self.assertIn('12.30%',reply)
        self.assertIn('+19.80',reply)
        self.assertIn('partially',reply)
        self.assertIn('4/15',reply)
        self.assertNotIn('99.00%',reply)
        self.assertNotIn(result['run_id'],reply)
        self.assertNotIn('计算日志',reply)

    def test_detailed_gradient_prefers_real_contrast_interval(self):
        result={'status':'ok','survey':{'status':'ok','rows':[
            {'group_id':'less_than_high_school','status':'ok','estimate':.32,'se':.01,'ci_low':.30,'ci_high':.34,'n':1000},
            {'group_id':'bachelors_plus','status':'ok','estimate':.20,'se':.01,'ci_low':.18,'ci_high':.22,'n':1000}],
            'gradient':{'contrasts':[
            {'group_id':'less_than_high_school','reference_id':'bachelors_plus','label':'未完成高中减本科及以上','status':'ok','estimate':.12,'ci_low':.08,'ci_high':.16}]}},
            'measurement':{'status':'not_requested'}}
        reply=analysis_reply(result,{'group_by':'education','filters':{}})
        self.assertRegex(reply, r'\+12\.00\s*percentage points')
        self.assertIn('+8.00 to +16.00',reply)
        self.assertIn('###',reply)

    def test_conversation_lock_is_shared_between_service_instances(self):
        other=ConversationService(FakeAgent(self.temporary.name))
        lock=self.service._lock(self.id)
        self.assertIs(lock,other._lock(self.id))
        lock.acquire()
        try:
            with self.assertRaises(RuntimeError):other.send(self.id,'你好')
        finally:lock.release()

    def test_boundaries_never_overridden_by_context_merge(self):
        self.send('按教育比较健康')
        for question in ['那中国成年人呢','那65岁以上呢','那70岁呢','换成糖尿病','那调整BMI呢']:
            response = self.send(question)
            self.assertIn(response['message']['status'],('blocked','clarify'))
        self.assertEqual(len(self.agent.calls),1)
        self.assertEqual(self.service.get(self.id)['context']['spec']['filters'],{})

    def test_failed_analysis_does_not_replace_successful_spec(self):
        original = self.send('按教育比较健康')['result']['run_id']
        self.agent.next_status = 'blocked'
        failed = self.send('那女性呢')
        self.assertEqual(failed['message']['status'],'blocked')
        self.assertEqual(failed['conversation']['last_run_id'],original)
        self.assertEqual(failed['conversation']['context']['spec']['filters'],{})
        explained = self.send('为什么抑制')
        self.assertRegex(explained['message']['content'], r'did not release a successful result')
        self.assertEqual(len(self.agent.calls),2)
        self.send('按收入呢')
        self.assertEqual(self.agent.calls[-1]['filters'],{})

    def test_persistence_restores_context_without_raw_calls(self):
        self.send('按教育比较健康')
        restored = ConversationService(FakeAgent(self.temporary.name))
        conversation = restored.get(self.id)
        self.assertEqual(conversation['context']['spec']['group_by'],'education')
        self.assertIsNotNone(conversation['latest_result'])
        self.assertNotIn('calls',conversation['latest_result']['measurement'])
        persisted = (Path(self.temporary.name)/'conversations'/(self.id+'.json')).read_text()
        self.assertNotIn('RAW_CALL_NOT_FOR_CHAT_STORAGE',persisted)
        self.assertNotIn('latest_result',json.loads(persisted))
        self.assertEqual(len(restored.list()),1)
        response = restored.send(self.id,'那女性呢')
        self.assertEqual(response['conversation']['context']['spec']['filters'],{'sex':'female'})

    def test_separate_conversations_do_not_share_context(self):
        self.send('按教育比较健康')
        second = self.service.create()['id']
        response = self.service.send(second,'那女性呢')
        self.assertEqual(response['message']['status'],'clarify')
        self.assertEqual(self.service.get(second)['context'],{})
        self.assertEqual(len(self.agent.calls),1)
        self.assertEqual(self.service.get(self.id)['context']['spec']['group_by'],'education')

    def test_previous_comparison_reuses_point_estimates_without_fake_ci(self):
        self.send('按教育比较健康')
        self.send('那女性呢')
        result = self.send('和刚才相比呢')
        self.assertIn('+10.00 percentage points',result['message']['content'])
        self.assertIn('have not been calculated',result['message']['content'])
        self.assertEqual(len(self.agent.calls),2)

    def test_progress_only_contains_execution_facts(self):
        events=[]
        result=self.send('按教育比较健康',on_event=events.append)
        self.assertTrue(any(e['stage']=='survey' for e in events))
        self.assertEqual(result['message']['process'],events)
        self.assertTrue(all(e['status'] in ('running','complete','blocked','failed','skipped') for e in events))
        self.assertTrue(all(e.get('timestamp') for e in events))
        self.assertEqual(result['message']['source']['type'],'run')
        facts=_safe_facts({'planned_calls':15,'valid_calls':12,'elapsed_ms':500,'calls':[{'raw_response':'private'}],'api_key':'not-a-real-key'})
        self.assertEqual(facts,{'planned_calls':15,'valid_calls':12,'elapsed_ms':500})

    def test_busy_invalid_ids_and_length_are_guarded(self):
        for invalid in ['../../.env','not-a-conversation']:
            with self.assertRaises(ValueError):self.service.get(invalid)
        with self.assertRaises(FileNotFoundError):self.service.get('f'*32)
        with self.assertRaises(ValueError):self.send('x'*2001)
        lock=self.service._lock(self.id)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeError):self.send('你好')
        finally:lock.release()
        self.assertEqual(self.service.get(self.id)['messages'],[])


if __name__ == '__main__':
    unittest.main()
