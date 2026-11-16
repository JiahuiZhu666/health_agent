import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nhis_agent.budget import ModelBudget, BudgetExceeded
from nhis_agent.metrics import snapshot
from nhis_agent.telemetry import Telemetry
from scripts.model_admin import main


class OperatorToolsTests(unittest.TestCase):
    def invoke(self,root,*arguments):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['--root',str(root),*arguments])
        self.assertEqual(code,0)
        return json.loads(output.getvalue())

    def test_disable_survives_new_budget_instance_and_reads_remain_available(self):
        with TemporaryDirectory() as folder:
            self.assertFalse(self.invoke(folder,'disable')['enabled'])
            with self.assertRaises(BudgetExceeded):
                ModelBudget(folder).reserve(attempt_id='attempt',turn_id='turn',workspace_id='workspace',input_tokens=10,output_tokens=10)
            self.assertFalse(self.invoke(folder,'status')['enabled'])
            self.assertTrue(self.invoke(folder,'enable')['enabled'])

    def test_unknown_usage_is_held_until_complete_observed_operator_reconciliation(self):
        with TemporaryDirectory() as folder:
            budget=ModelBudget(folder)
            reservation=budget.reserve(attempt_id='attempt',turn_id='turn',workspace_id='workspace',input_tokens=100,output_tokens=20)
            budget.reconcile(reservation['id'],unknown=True)
            self.assertEqual(self.invoke(folder,'reservations')[0]['state'],'held')
            result=self.invoke(folder,'reconcile','--reservation-id',reservation['id'],
                '--input-tokens','80','--output-tokens','15','--evidence-reference','provider_audit_123')
            self.assertEqual(result['state'],'reconciled')
            self.assertEqual(result['input_tokens'],80)
            self.assertEqual(budget.status()['held_reservations'],0)

    def test_missing_usage_and_ambiguous_refund_are_rejected_before_state_change(self):
        with TemporaryDirectory() as folder:
            budget=ModelBudget(folder);item=budget.reserve(attempt_id='attempt',turn_id='turn',workspace_id='workspace',input_tokens=10,output_tokens=10)
            budget.reconcile(item['id'],unknown=True)
            for options in ([],['--input-tokens','1'],['--confirmed-not-sent','--input-tokens','0']):
                with self.subTest(options=options),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    main(['--root',folder,'reconcile','--reservation-id',item['id'],'--evidence-reference','independent_evidence',*options])
            self.assertEqual(budget.status()['held_reservations'],1)

    def test_proven_not_sent_release_is_explicit_and_idempotent(self):
        with TemporaryDirectory() as folder:
            budget=ModelBudget(folder);item=budget.reserve(attempt_id='attempt',turn_id='turn',workspace_id='workspace',input_tokens=10,output_tokens=10)
            for _ in range(2):
                result=self.invoke(folder,'reconcile','--reservation-id',item['id'],
                    '--confirmed-not-sent','--evidence-reference','local_unsent_audit')
                self.assertEqual(result['state'],'released')
            self.assertEqual(budget.status()['day']['calls'],0)

    def test_metrics_are_metadata_only_readonly_and_alert_from_retained_observations(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);telemetry=Telemetry(root)
            for _ in range(10):telemetry.emit('model.finished',{'error_code':'budget_exceeded','elapsed_ms':4,
                'input_tokens':3,'output_tokens':1,'prompt':'PRIVATE_TEXT','api_key':'PRIVATE_KEY'})
            before={p.name:p.read_bytes() for p in (root/'.cache').glob('*')}
            result=snapshot(root)
            self.assertEqual(result['models']['observed_tokens'],{'input_tokens':30,'output_tokens':10})
            self.assertIn('model_budget_exceeded',result['alerts'])
            self.assertIn('model_error_ratio_above_20_percent',result['alerts'])
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertEqual(before,{p.name:p.read_bytes() for p in (root/'.cache').glob('*')})


if __name__=='__main__':unittest.main()
