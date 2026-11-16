"""Artificial aggregate fixtures test chart integrity, not survey estimates."""
import copy
import json
import unittest
from unittest.mock import patch

from nhis_agent.charts import chart_request, make_charts


def result(number=1,group='education',sex=None):
    return {'status':'ok','run_id':'20261116T065024Z-{:010x}'.format(number),
            'spec':{'year':2024,'outcome':'fair_poor','age_min':25,'age_max':64,'confidence':.95,
                    'analysis':'proportion','group_by':group,'filters':{'sex':['female']} if sex else {}},
            'survey':{'status':'ok','rows':[
                {'group_id':'less_than_high_school' if group=='education' else 'below_100','status':'ok',
                 'label':'Source label','estimate':.3,'ci_low':.25,'ci_high':.35,'se':.02,'n':500},
                {'group_id':'bachelors_plus' if group=='education' else '400_plus','status':'ok',
                 'estimate':.1,'ci_low':.08,'ci_high':.12,'se':.01,'n':1000}],
                'gradient':None,'regression':None,'imputation':{'required':False,'m':1}},
            'measurement':{'rows':[{'mean':.999991}],'calls':[{'raw_response':'PRIVATE_RAW_RESPONSE'}]}}


class ChartTests(unittest.TestCase):
    def test_exact_plot_requests_include_all_discussed_results(self):
        for question in ['give me a whole plot of all results we have discussed','give me some plot','画图','把所有结果一起画图']:
            with self.subTest(question=question):
                self.assertEqual(chart_request(question),{'scope':'all','kind':'auto','style':'point'})
        self.assertEqual(chart_request('Plot the latest result as a bar chart'),
                         {'scope':'latest','kind':'auto','style':'bar'})
        self.assertEqual(chart_request('Make a forest plot of the regression odds ratios')['kind'],'regression')
        self.assertEqual(chart_request('Show an OR forest plot')['kind'],'regression')
        self.assertEqual(chart_request('Show a bar or point chart of these results')['kind'],'auto')
        self.assertEqual(chart_request('可视化一下'),{'scope':'all','kind':'auto','style':'point'})
        self.assertEqual(chart_request('render this as a figure')['scope'],'latest')
        self.assertEqual(chart_request('show all figures')['scope'],'all')

    def test_general_art_plot_definitions_and_missing_results_are_not_hijacked(self):
        for text in ['Draw me a cat.','Explain the plot of this novel.','What is a forest plot?',
                     'Help me write a story plot.','画一只猫','What are black holes?',
                     'Plot a sine wave.','Graph y = x^2.','Show the normal distribution.',
                     'Figure out a name for my cat.']:
            with self.subTest(text=text):self.assertIsNone(chart_request(text))
        self.assertIsNone(chart_request('give me some plot',has_results=False))

    def test_three_scopes_remain_separate_with_english_labels_and_stable_ids(self):
        values=[result(1),result(2,'income'),result(3,'income',sex='female')]
        before=copy.deepcopy(values)
        charts=make_charts(values)
        self.assertEqual(len(charts),3)
        self.assertEqual([chart['source_run_id'] for chart in charts],[item['run_id'] for item in values])
        self.assertNotIn('Restricted to Female',charts[1]['subtitle'])
        self.assertIn('Restricted to Female',charts[2]['subtitle'])
        self.assertIn('Less than high school',charts[0]['groups'][0]['label'])
        self.assertIn('Below 100%',charts[1]['groups'][0]['label'])
        self.assertEqual(charts,make_charts(values))
        self.assertEqual(values,before)

    def test_proportions_use_only_recorded_points_intervals_and_sample_counts(self):
        chart=make_charts(result())[0]
        self.assertEqual(chart['unit'],'percent')
        self.assertEqual(chart['reference'],0)
        self.assertEqual(chart['groups'][0],{'label':'Less than high school','value':30.,'low':25.,
                                            'high':35.,'n':500,'group_id':'less_than_high_school'})
        self.assertNotIn('PRIVATE_RAW_RESPONSE',json.dumps(chart))
        self.assertNotIn('99.9991',json.dumps(chart))

    def test_suppressed_and_invalid_rows_never_leak_estimates_or_counts(self):
        value=result()
        for updates in [{'status':'suppressed'},{'n':99},{'estimate':float('nan')},{'ci_low':.8},
                        {'ci_high':float('inf')},{'se':-1},{'estimate':True},{'n':10**400}]:
            with self.subTest(updates=str(updates)):
                bad=copy.deepcopy(value['survey']['rows'][0]);bad.update(updates)
                bad['label']='HIDDEN_LABEL';bad['group_id']='hidden';bad['n']=updates.get('n',555555)
                modified=result();modified['survey']['rows']=[bad]
                self.assertEqual(make_charts(modified),[])

    def test_missing_legacy_count_is_omitted_without_inventing_a_denominator(self):
        value=result()
        value['survey']['rows'][0].pop('n')
        group=make_charts(value)[0]['groups'][0]
        self.assertEqual(group['value'],30.)
        self.assertNotIn('n',group)

    def test_blocked_runs_unreleased_surveys_and_bad_run_ids_do_not_chart(self):
        for mutate in [lambda value:value.update(status='blocked'),
                       lambda value:value['survey'].update(status='blocked'),
                       lambda value:value.update(run_id='../private'),
                       lambda value:value['spec'].update(confidence=.9)]:
            value=result();mutate(value)
            self.assertEqual(make_charts(value),[])

    def test_gradient_uses_actual_joint_interval_and_requires_both_released_components(self):
        value=result();value['spec']['analysis']='gradient'
        value['survey']['gradient']={'contrasts':[{'group_id':'less_than_high_school','reference_id':'bachelors_plus',
            'status':'ok','estimate':.2,'ci_low':.14,'ci_high':.26,'se':.03}]}
        chart=make_charts(value)[0]
        self.assertEqual(chart['kind'],'gradient');self.assertEqual(chart['unit'],'percentage_points')
        self.assertEqual(chart['reference'],0)
        self.assertEqual(chart['groups'][0]['value'],20.)
        self.assertAlmostEqual(chart['groups'][0]['low'],14.)
        self.assertEqual(chart['groups'][0]['high'],26.)
        self.assertNotIn('n',chart['groups'][0])
        for status in ('suppressed','missing'):
            modified=copy.deepcopy(value)
            if status=='missing':modified['survey']['rows'].pop()
            else:modified['survey']['rows'][1]['status']='suppressed'
            self.assertEqual(make_charts(modified),[])
        modified=copy.deepcopy(value);modified['survey']['gradient']['contrasts'][0].pop('reference_id')
        self.assertEqual(make_charts(modified),[])

    def test_regression_forest_uses_or_scale_reference_and_same_sample(self):
        value=result();value['spec']['analysis']='regression'
        value['survey']['regression']={'status':'ok','n':800,'unadjusted':{'n':800},
            'references':{'education':'bachelors_plus'},'coefficients':[
                {'term':'(Intercept)','odds_ratio':.2,'ci_low':.1,'ci_high':.3,'se':.1},
                {'term':'educationless_than_high_school','odds_ratio':2.3,'ci_low':1.5,'ci_high':3.4,'se':.2},
                {'term':'suppressed','status':'suppressed','odds_ratio':9999,'ci_low':8888,'ci_high':10000}]}
        chart=make_charts(value,style='bar')[0]
        self.assertEqual(chart['kind'],'regression');self.assertEqual(chart['style'],'forest')
        self.assertEqual(chart['unit'],'odds_ratio');self.assertEqual(chart['reference'],1)
        self.assertEqual(len(chart['groups']),1)
        self.assertEqual(chart['groups'][0]['value'],2.3)
        self.assertEqual(chart['groups'][0]['low'],1.5)
        self.assertEqual(chart['groups'][0]['high'],3.4)
        self.assertIn('vs Bachelor',chart['groups'][0]['label'])
        self.assertEqual(chart['groups'][0]['n'],800)
        value['survey']['regression']['unadjusted']['n']=700
        self.assertEqual(make_charts(value),[])

    def test_income_imputation_counts_are_not_multiplied_and_duplicates_do_not_repeat(self):
        value=result(group='income')
        value['survey']['imputation']={'required':True,'m':10}
        value['survey']['rows'][0]['n_range']={'min':500,'max':550}
        charts=make_charts([value,copy.deepcopy(value)])
        self.assertEqual(len(charts),1)
        self.assertEqual(charts[0]['groups'][0]['n'],500)
        self.assertIn('n is the minimum',charts[0]['subtitle'])

    def test_no_requested_advanced_result_is_invented_from_proportions(self):
        self.assertEqual(make_charts(result(),kind='gradient'),[])
        self.assertEqual(make_charts(result(),kind='regression'),[])
        self.assertEqual(make_charts(None),[])
        with self.assertRaises(ValueError):make_charts(result(),kind='invent')

    def test_reported_conversation_requests_replay_without_recalculation(self):
        # Replays the reported turn sequence using isolated aggregate fixtures;
        # it does not require private conversation or run files in a checkout.
        discussed=[result(1),result(2,'income'),result(3,'income',sex='female')]
        questions=['give me a whole plot of all results we have discussed','give me some plot']
        with patch('subprocess.run',side_effect=AssertionError('No R or shell calls')), \
             patch('nhis_agent.chat_api.request',side_effect=AssertionError('No model calls')):
            for question in questions:
                request=chart_request(question,has_results=True)
                selected=discussed if request['scope']=='all' else discussed[-1:]
                panels=make_charts(selected,kind=request['kind'],style=request['style'])
                self.assertEqual(len(panels),3)
                self.assertEqual([panel['source_run_id'] for panel in panels],[value['run_id'] for value in discussed])
                self.assertNotIn('Female',panels[1]['subtitle'])
                self.assertIn('Female',panels[2]['subtitle'])

    def test_recorded_gradient_interval_is_not_clipped_to_logical_point_bounds(self):
        value=result();value['spec']['analysis']='gradient'
        value['survey']['gradient']={'contrasts':[{'group_id':'less_than_high_school','reference_id':'bachelors_plus',
            'status':'ok','estimate':.2,'ci_low':-1.1,'ci_high':1.2,'se':.5}]}
        chart=make_charts(value)[0]
        self.assertAlmostEqual(chart['groups'][0]['low'],-110.)
        self.assertEqual(chart['groups'][0]['high'],120.)


if __name__=='__main__':unittest.main()
