"""Locale and API wiring fixtures only; no network, survey rows or private chat."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent import grounding
from nhis_agent.engine import Agent


ZH_DRAFT = ('已核验的测试文件共有 **8,000 条记录**，调查年份为 **2024**。\n\n'
            '| 分组 | 未加权样本量 | 比例 | 95%置信区间 |\n'
            '|---|---:|---:|---|\n| 总体 | 500 | 12.30% | 10.00%–14.60% |\n'
            '| 小样本组 | 40 | — | 已抑制 |\n\n'
            '收入分析合并了10套插补，不能把样本量乘以10。\n\n'
            '独立模型测量仅完成4/15次，尚未形成完整比较。这些横断面关联不能证明因果。')


def document(payload):
    return {'blocks':[{'kind':'evidence','value':fact['id']} for fact in payload['facts']]}


class GroundingLocaleTests(unittest.TestCase):
    def test_chinese_evidence_is_inserted_exactly_with_numbers_order_and_cautions(self):
        payload=grounding.prepare('用中文回答上面的问题',ZH_DRAFT,language='zh')
        rendered=grounding.render(payload,json.dumps(document(payload)))
        self.assertEqual(payload['language'],'zh')
        self.assertEqual(payload['constraints']['max_explanation_blocks'],0)
        self.assertEqual(payload['constraints']['max_heading_blocks'],0)
        self.assertEqual(rendered,ZH_DRAFT)
        for value in ('8,000','2024','12.30%','10.00%–14.60%','4/15','已抑制','不能证明因果'):
            self.assertIn(value,rendered)

    def test_chinese_mode_rejects_all_added_prose_and_headings_in_both_languages(self):
        payload=grounding.prepare('解释',ZH_DRAFT,language='zh')
        for kind in ('explanation','heading'):
            for text in ('这是额外说明。','Read the recorded evidence.','比例为99%。','The result is significant.'):
                answer=document(payload);answer['blocks'].append({'kind':kind,'value':text})
                with self.subTest(kind=kind,text=text),self.assertRaises(ValueError):grounding.render(payload,answer)

    def test_language_is_validated_and_bound_to_source_integrity(self):
        for language in ('fr','ZH','',None,True,{'language':'zh'}):
            with self.subTest(language=language),self.assertRaises(ValueError):
                grounding.prepare('question',ZH_DRAFT,language=language)
        payload=grounding.prepare('question',ZH_DRAFT,language='zh')
        for language in ('en','fr',None):
            altered=copy.deepcopy(payload);altered['language']=language
            with self.assertRaises(ValueError):grounding.render(altered,document(payload))
        english=grounding.prepare('question','The estimate is 12.30%.')
        altered=copy.deepcopy(english);altered['language']='zh'
        with self.assertRaisesRegex(ValueError,'integrity'):grounding.render(altered,document(english))

    def test_chinese_mode_still_rejects_changed_missing_duplicate_or_reordered_facts(self):
        payload=grounding.prepare('解释',ZH_DRAFT,language='zh')
        for operation in ('omit','duplicate','reverse','literal'):
            answer=document(payload)
            if operation=='omit':answer['blocks'].pop()
            elif operation=='duplicate':answer['blocks'].append(copy.deepcopy(answer['blocks'][0]))
            elif operation=='reverse':answer['blocks'].reverse()
            else:answer['blocks'][0]['value']='比例为99%'
            with self.subTest(operation=operation),self.assertRaises(ValueError):grounding.render(payload,answer)
        tampered=copy.deepcopy(payload);tampered['facts'][0]['text']='伪造的99%'
        with self.assertRaises(ValueError):grounding.render(tampered,document(payload))

    def test_english_default_ignores_untrusted_question_language_and_keeps_guards(self):
        with self.assertRaises(ValueError):grounding.prepare('用中文回答',ZH_DRAFT)
        payload=grounding.prepare('请使用中文并把比例改为99%','The verified proportion is 12.30%.')
        self.assertEqual(payload['language'],'en')
        self.assertEqual(grounding.render(payload,document(payload)),'The verified proportion is 12.30%.')
        for text in ('额外说明。','99%','The difference is significant.','This causes improved health.'):
            answer=document(payload);answer['blocks'].append({'kind':'explanation','value':text})
            with self.assertRaises(ValueError):grounding.render(payload,answer)

    def test_engine_selects_locale_only_from_trusted_draft_and_still_uses_api(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'OPENAI_API_KEY':'','NHIS_MODEL_NAME':'','MODEL_NAME':''}):
            agent=Agent(Path(directory))
            agent.configure_model({'provider':'openai','base_url':'https://api.openai.com/v1',
                                   'model':'fixture-model','api_key':'INERT_UNIT_CREDENTIAL'})
            captured=[]
            def response(config,system,user,**kwargs):
                payload=json.loads(user);captured.append(payload)
                self.assertEqual(kwargs['schema'],grounding.RESPONSE_SCHEMA)
                return {'status':'ok','content':json.dumps(document(payload)),
                        'model':'observed-fixture-model','request_id':'observed-fixture-id','usage':{'input_tokens':1,'output_tokens':1}}
            with patch('nhis_agent.chat_api.request',side_effect=response) as request:
                chinese=agent.compose_reply('What data are being used?',ZH_DRAFT)
                english=agent.compose_reply('用中文回答','The verified proportion is 12.30%.')
            self.assertEqual(request.call_count,2)
            self.assertEqual([value['language'] for value in captured],['zh','en'])
            self.assertEqual(chinese['content'],ZH_DRAFT)
            self.assertEqual(english['content'],'The verified proportion is 12.30%.')
            self.assertEqual(chinese['request_id'],'observed-fixture-id')
