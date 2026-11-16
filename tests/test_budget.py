"""SQLite accounting tests use temporary state and no provider calls."""
import concurrent.futures
import os
import tempfile
import unittest
from unittest.mock import patch

from nhis_agent.budget import ModelBudget, BudgetExceeded


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100_000.0
        self.budget = ModelBudget(self.temp.name, clock=lambda: self.now)

    def reserve(self, **values):
        return self.budget.reserve(turn_id='turn', workspace_id='workspace',
                                   input_tokens=100, output_tokens=200, **values)

    def test_cost_is_derived_from_conservative_token_bounds(self):
        reserved = self.reserve(estimated_cost_micros=1)
        self.assertEqual(reserved['cost_micros'], 100*4+200*20)
        self.assertEqual(self.budget.status('turn')['turn']['calls'], 1)

    def test_complete_usage_reconciles_and_is_idempotent(self):
        reservation = self.reserve()
        actual = self.budget.reconcile(reservation['id'], {'input_tokens': 10, 'output_tokens': 20})
        self.assertEqual(actual['state'], 'reconciled')
        self.assertEqual(actual['cost_micros'], 440)
        again = self.budget.reconcile(reservation['id'], {'input_tokens': 999, 'output_tokens': 999})
        self.assertEqual(again, actual)
        restored = ModelBudget(self.temp.name, clock=lambda: self.now)
        self.assertEqual(restored.status('turn')['turn']['input_tokens'], 10)

    def test_missing_usage_and_unknown_delivery_hold_beyond_day_rollover(self):
        for usage, unknown in [(None, False), ({'input_tokens': 3}, False),
                               ({'input_tokens': 2, 'output_tokens': 4}, True)]:
            reservation = self.reserve()
            result = self.budget.reconcile(reservation['id'], usage, unknown=unknown)
            self.assertEqual(result['state'], 'held')
        self.now += 172800
        status = self.budget.status()
        self.assertEqual(status['day']['calls'], 3)
        self.assertEqual(status['held_reservations'], 3)
        self.assertEqual(status['day']['input_tokens'], 300)

    def test_known_usage_ages_out_of_day_but_not_turn_budget(self):
        reservation = self.reserve()
        self.budget.reconcile(reservation['id'], {'prompt_tokens': 5, 'completion_tokens': 7})
        self.now += 86401
        self.assertEqual(self.budget.status('turn')['day']['calls'], 0)
        self.assertEqual(self.budget.status('turn')['turn']['calls'], 1)

    def test_known_not_sent_releases_all_reservation(self):
        reservation = self.reserve()
        self.budget.reconcile(reservation['id'], not_sent=True)
        self.assertEqual(self.budget.status('turn')['turn']['calls'], 0)
        self.assertEqual(self.budget.status()['day']['cost_micros'], 0)

    def test_atomic_admission_shared_by_multiple_budget_instances(self):
        instances = [ModelBudget(self.temp.name, limits={'day_calls': 7}, clock=lambda: self.now) for _ in range(4)]
        def attempt(index):
            try:
                instances[index % 4].reserve(turn_id='turn-'+str(index), input_tokens=1, output_tokens=1)
                return True
            except BudgetExceeded:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            accepted = list(executor.map(attempt, range(30)))
        self.assertEqual(sum(accepted), 7)
        self.assertEqual(self.budget.status()['day']['calls'], 7)

    def test_per_turn_call_token_and_cost_boundaries(self):
        for limit, value in [('turn_calls', 1), ('turn_input_tokens', 100),
                             ('turn_output_tokens', 200), ('turn_cost_micros', 4400)]:
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                budget = ModelBudget(directory, limits={limit: value})
                budget.reserve(turn_id='turn', input_tokens=100, output_tokens=200)
                with self.assertRaises(BudgetExceeded):
                    budget.reserve(turn_id='turn', input_tokens=100, output_tokens=200)
                self.assertEqual(budget.status('turn')['turn']['calls'], 1)

    def test_observed_overrun_is_recorded_and_blocks_later_calls(self):
        budget = ModelBudget(self.temp.name, limits={'turn_input_tokens': 200})
        reservation = budget.reserve(turn_id='turn', input_tokens=100, output_tokens=10)
        budget.reconcile(reservation['id'], {'input_tokens': 250, 'output_tokens': 10})
        self.assertEqual(budget.status('turn')['turn']['input_tokens'], 250)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(turn_id='turn', input_tokens=1, output_tokens=1)

    def test_kill_switch_persists_and_local_models_remain_usable(self):
        reservation = self.reserve()
        self.budget.set_enabled(False)
        restored = ModelBudget(self.temp.name)
        with self.assertRaises(BudgetExceeded) as raised:
            restored.reserve(turn_id='new', input_tokens=1, output_tokens=1)
        self.assertEqual(raised.exception.code, 'kill_switch')
        self.budget.reconcile(reservation['id'], {'input_tokens': 1, 'output_tokens': 1})
        local = restored.reserve(turn_id='local', input_tokens=10, output_tokens=10, paid=False)
        local = restored.reconcile(local['id'], {'prompt_eval_count': 3, 'eval_count': 2})
        self.assertEqual(local['cost_micros'], 0)
        self.assertFalse(restored.status()['enabled'])
        self.budget.set_enabled(True)
        with patch.dict(os.environ, {'NHIS_MODEL_KILL_SWITCH': 'true'}):
            with self.assertRaises(BudgetExceeded): self.reserve()

    def test_duplicate_attempt_is_not_charged_twice(self):
        self.reserve(attempt_id='same-attempt')
        with self.assertRaises(BudgetExceeded): self.reserve(attempt_id='same-attempt')
        self.assertEqual(self.budget.status()['day']['calls'], 1)

    def test_operator_list_is_bounded_metadata_only_and_supports_manual_resolution(self):
        first, second = self.reserve(), self.reserve()
        self.budget.reconcile(first['id'], unknown=True)
        held = self.budget.reservations()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]['id'], first['id'])
        self.assertEqual(held[0]['cost_basis'], 'reserved_bound')
        self.assertNotIn('workspace_id', held[0])
        self.assertEqual(len(self.budget.reservations('all', limit=1)), 1)
        self.budget.reconcile(first['id'], {'input_tokens': 10, 'output_tokens': 2})
        self.assertEqual(self.budget.reservations(), [])
        self.assertEqual(self.budget.reservations('reconciled')[0]['cost_basis'], 'observed_usage_at_planning_rates')
        self.assertEqual(self.budget.reservations('reserved')[0]['id'], second['id'])
        for state, limit in [('invalid', 1), ('all', 0), ('all', True), ('all', 1001)]:
            with self.assertRaises(ValueError): self.budget.reservations(state, limit)

    def test_invalid_limits_rates_and_usage_are_not_silently_free(self):
        for limit in (0, -1, True, float('inf'), 'nan'):
            with self.assertRaises(ValueError): ModelBudget(self.temp.name, limits={'turn_calls': limit})
        for rate in ('NaN', '-1', 'Infinity'):
            with self.assertRaises(ValueError): ModelBudget(self.temp.name, input_usd_per_million=rate)
        reservation = self.reserve()
        result = self.budget.reconcile(reservation['id'], {'input_tokens': True, 'output_tokens': 20})
        self.assertEqual(result['state'], 'held')
