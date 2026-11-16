"""Checks of documented coding and metadata; no survey estimation or API calls."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nhis_agent import outcomes
from nhis_agent.outcome_intent import outcome_mentions, selected_outcomes


class CanonicalOutcomeIntentTests(unittest.TestCase):
    def test_shared_medical_object_names_both_events_with_original_source_spans(self):
        questions=(
            '哪些年龄组更常因费用而延迟或未获得医疗服务？',
            '按年龄组比较因费用而延迟或未获得医疗服务的比例',
            '哪个年龄组因为费用而推迟或未能就医？',
            'Compare cost-related delayed and unmet medical care by age group',
            'Which age groups have more cost-related delayed or unmet care?',
            'Which age groups more often delay or fail to obtain medical care because of cost?',
        )
        for question in questions:
            with self.subTest(question=question):
                mentions=outcome_mentions(question)
                self.assertEqual(selected_outcomes(question),('delayed_care_cost','unmet_care_cost'))
                self.assertEqual({code for item in mentions for code in item.outcomes},
                                 {'delayed_care_cost','unmet_care_cost'})
                for item in mentions:
                    self.assertEqual(question[item.start:item.end],item.text)
                    self.assertLess(item.start,item.end)

    def test_single_events_and_insurance_polarity_are_never_implicitly_swapped(self):
        for question,expected in (
            ('Compare delayed medical care by age group',('delayed_care_cost',)),
            ('Compare unmet medical care by age group',('unmet_care_cost',)),
            ('按年龄组比较费用导致的未就医',('unmet_care_cost',)),
            ('Compare insured proportions by education',('insured',)),
            ('Compare uninsured proportions by education',('uninsured',)),
            ('Compare people without health insurance by age group',('uninsured',)),
            ('Compare people not insured by age group',('uninsured',)),
            ('按年龄组比较有医保比例',('insured',)),
            ('按年龄组比较无医保比例',('uninsured',)),
            ('Show health insurance coverage distribution',('insured','uninsured')),
            ('Show whether adults are insured or uninsured',('insured','uninsured')),
            ('按年龄组比较有无医保',('insured','uninsured')),
        ):
            with self.subTest(question=question):
                self.assertEqual(selected_outcomes(question),expected)

    def test_unknown_outcomes_and_nonmedical_insurance_do_not_become_health_events(self):
        for question in ('Compare blood glucose by education',
                         'Compare car insurance coverage by age group',
                         'Compare religious affiliation by education'):
            with self.subTest(question=question):
                self.assertEqual(selected_outcomes(question),())


class OutcomeMetadataTests(unittest.TestCase):
    def tearDown(self):
        outcomes._registered.cache_clear()

    def test_registered_five_outcomes_keep_legacy_health_coding(self):
        self.assertEqual(outcomes.allowed_outcomes(),
            ('fair_poor','insured','uninsured','delayed_care_cost','unmet_care_cost'))
        value=outcomes.get_outcome()
        self.assertEqual(value['variable'],'PHSTAT_A')
        self.assertEqual(value['one'],[4,5])
        self.assertEqual(value['zero'],[1,2,3])
        self.assertEqual(value['missing'],[7,8,9])
        self.assertEqual(value['codebook_page'],32)
        self.assertEqual(outcomes.outcome_label(),'Fair or poor self-rated health')

    def test_coverage_polarity_is_edited_notcov_and_never_the_initial_screen(self):
        covered=outcomes.get_outcome('insured');uncovered=outcomes.get_outcome('uninsured')
        self.assertEqual(covered['variable'],'NOTCOV_A')
        self.assertEqual(uncovered['variable'],'NOTCOV_A')
        self.assertEqual(covered['one'],[2]);self.assertEqual(covered['zero'],[1])
        self.assertEqual(uncovered['one'],[1]);self.assertEqual(uncovered['zero'],[2])
        self.assertEqual(covered['one'],uncovered['zero'])
        self.assertEqual(covered['missing'],uncovered['missing'])
        self.assertIn('interview',covered['time_window'])
        self.assertEqual(covered['codebook_page'],187)
        self.assertIn('edited NOTCOV_A',covered['definition'])
        screen=covered['related_items']['HICOV_A']
        self.assertEqual(screen['yes'],[1]);self.assertEqual(screen['no'],[2])
        self.assertEqual(screen['missing'],[7,8,9])
        self.assertEqual(screen['codebook_page'],297)
        self.assertIn('editing',screen['limitation'])
        self.assertNotIn('HICOV_A',outcomes.allowed_outcomes())

    def test_cost_items_have_verified_yes_no_codes_windows_and_official_pages(self):
        for name,variable,page,question in [
            ('delayed_care_cost','MEDDL12M_A',332,'UTZ.0100.00.1'),
            ('unmet_care_cost','MEDNG12M_A',333,'UTZ.0110.00.1')]:
            with self.subTest(name=name):
                value=outcomes.get_outcome(name)
                self.assertEqual(value['variable'],variable)
                self.assertEqual(value['one'],[1]);self.assertEqual(value['zero'],[2])
                self.assertEqual(value['missing'],[7,8,9])
                self.assertEqual(value['time_window'],'Past 12 months')
                self.assertIn('cost',value['definition'])
                self.assertIn('past 12 months',value['definition'])
                self.assertEqual(value['codebook_page'],page)
                self.assertEqual(value['question_id'],question)
                self.assertTrue(value['codebook_url'].startswith('https://ftp.cdc.gov/'))
                self.assertTrue(value['codebook_url'].endswith('/2024/adult-codebook.pdf'))

    def test_cost_barrier_complements_are_not_registered_as_general_timely_care(self):
        self.assertEqual(outcomes.ACCESS_OUTCOMES,
                         ('insured','uninsured','delayed_care_cost','unmet_care_cost'))
        for name in ('delayed_care_cost','unmet_care_cost'):
            value=outcomes.get_outcome(name)
            self.assertIn('only',value['limitation'])
            self.assertIn('no response does not establish',value['limitation'].lower())
        self.assertIn('not only people reporting a need',outcomes.get_outcome('unmet_care_cost')['limitation'])
        with self.assertRaises(ValueError):outcomes.get_outcome('timely_care')

    def test_item_universe_is_documented_without_broadening_registered_analysis_age(self):
        registry=json.loads(outcomes.REGISTRY_PATH.read_text())
        domain=registry['design']['domain']
        self.assertEqual((domain['minimum'],domain['maximum']),(25,64))
        for name in outcomes.allowed_outcomes():
            value=outcomes.get_outcome(name)
            self.assertIn('18',value['universe'])
            self.assertEqual(value['universe_expression'],'HHSTAT_A=1')
            self.assertIn('18 years or older',value['definition'])

    def test_missing_codes_and_system_missing_are_not_negative_events(self):
        for name in outcomes.allowed_outcomes():
            with self.subTest(name=name):
                value=outcomes.get_outcome(name)
                one,zero,missing=map(set,(value['one'],value['zero'],value['missing']))
                self.assertFalse(one & zero);self.assertFalse(one & missing);self.assertFalse(zero & missing)
                self.assertEqual(value['missing_reasons'],{'7':'Refused','8':'Not ascertained','9':"Don't know"})
                self.assertIn('never recode them to zero',value['system_missing_policy'])
                self.assertIn('excluded',value['denominator_note'])

    def test_metadata_access_returns_deep_copies_and_no_estimates_or_raw_records(self):
        original=outcomes.get_outcome('insured')
        modified=outcomes.get_outcome('insured')
        modified['one'].append(999)
        modified['related_items']['HICOV_A']['missing'].append(888)
        modified['missing_reasons']['9']='MODIFIED'
        self.assertEqual(outcomes.get_outcome('insured'),original)
        for key in ('estimate','rows','n','raw_response','calls','api_key'):
            self.assertNotIn(key,original)

    def test_unknown_or_invalid_identifiers_fail_without_echoing_input(self):
        for value in ('unknown','NOTCOV_A','../path',None,[],{'outcome':'insured'}):
            with self.subTest(kind=type(value).__name__),self.assertRaises(ValueError) as caught:
                outcomes.get_outcome(value)
            self.assertEqual(str(caught.exception),'Unknown registered outcome.')

    def test_invalid_or_overlapping_registry_codes_are_rejected(self):
        source=json.loads(outcomes.REGISTRY_PATH.read_text())
        for mutation in [lambda value:value.update(one=[1],zero=[1]),
                         lambda value:value.update(one=[True]),
                         lambda value:value.update(missing=[]),
                         lambda value:value.update(codebook_page=0),
                         lambda value:value.update(definition='')]:
            invalid=copy.deepcopy(source);mutation(invalid['outcomes']['insured'])
            with TemporaryDirectory() as folder:
                path=Path(folder)/'registry.json';path.write_text(json.dumps(invalid))
                with patch.object(outcomes,'REGISTRY_PATH',path):
                    outcomes._registered.cache_clear()
                    with self.assertRaises(ValueError):outcomes.allowed_outcomes()
                outcomes._registered.cache_clear()


if __name__=='__main__':unittest.main()
