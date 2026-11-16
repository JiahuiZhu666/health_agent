"""Synthetic dependency responses only; no R process, survey data or API call."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nhis_agent import r_worker, stats
from nhis_agent.execution_context import ExecutionContext, ExecutionDeadline, bind


class ReadinessResourceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.executable=self.root/'.r-env'/'bin'/'Rscript'
        self.executable.parent.mkdir(parents=True);self.executable.write_text('INERT_TEST_FILE');self.executable.chmod(0o755)
        for target,value in [('ROOT',self.root)]:
            patcher=patch.object(stats,target,value);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(stats.shutil,'which',return_value=None);patcher.start();self.addCleanup(patcher.stop)
        stats.clear_readiness_cache();self.addCleanup(stats.clear_readiness_cache)
        self.completed=subprocess.CompletedProcess([],0,'4.2.2\n4.1.1\n1.8.4\n2.4\n','')

    def test_all_callers_share_one_observation_and_can_force_explicit_refresh(self):
        entered=threading.Event();release=threading.Event()
        def probe(*args,**kwargs):
            entered.set();release.wait(1);return self.completed
        with patch.object(r_worker,'run_probe',side_effect=probe) as call,ThreadPoolExecutor(max_workers=8) as pool:
            futures=[pool.submit(stats.readiness) for _ in range(8)]
            self.assertTrue(entered.wait(1));release.set()
            results=[item.result() for item in futures]
            self.assertTrue(all(item['ready'] for item in results));self.assertEqual(call.call_count,1)
            results[0]['r_version']='MUTATED'
            self.assertEqual(stats.readiness()['r_version'],'4.2.2')
            stats.readiness(force=True);self.assertEqual(call.call_count,2)

    def test_probe_command_is_fixed_bounded_and_does_not_charge_a_research_step(self):
        context=ExecutionContext(deadline=time.monotonic()+.5)
        with bind(context),patch.object(r_worker,'run_limited',return_value=self.completed) as call:
            result=r_worker.run_probe(self.executable)
        self.assertIs(result,self.completed)
        command=call.call_args.args[0];limits=call.call_args.kwargs
        self.assertEqual(command,[str(self.executable.resolve()),'--vanilla','-e',r_worker.PROBE_EXPRESSION])
        self.assertIs(limits['context'],context);self.assertIsNone(limits['step_limit'])
        self.assertGreater(limits['timeout'],0);self.assertLessEqual(limits['timeout'],.5)
        self.assertLessEqual(limits['cpu_seconds'],15);self.assertLessEqual(limits['output_bytes'],65536)

    def test_probe_uses_the_same_r_capacity_gate_and_never_spawns_when_busy(self):
        with patch.dict('os.environ',{'NHIS_R_MAX_WORKERS':'1'}),patch.object(subprocess,'Popen') as spawn:
            with r_worker._slot(1):
                with self.assertRaises(r_worker.RWorkerBusy):r_worker.run_probe(self.executable)
        spawn.assert_not_called()

    def test_busy_probe_does_not_poison_the_cached_capability(self):
        with patch.object(r_worker,'run_probe',side_effect=r_worker.RWorkerBusy()):
            value=stats.readiness()
        self.assertEqual(value['status'],'busy')
        with patch.object(r_worker,'run_probe',return_value=self.completed) as call:
            self.assertTrue(stats.readiness()['ready'])
        call.assert_called_once()

    def test_deadline_cancels_a_waiting_single_flight_without_another_probe(self):
        entered=threading.Event();release=threading.Event()
        def probe(*args,**kwargs):entered.set();release.wait(1);return self.completed
        with patch.object(r_worker,'run_probe',side_effect=probe) as call,ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(stats.readiness)
            self.assertTrue(entered.wait(1))
            try:
                with bind(ExecutionContext(deadline=time.monotonic()+.02)):
                    with self.assertRaises(ExecutionDeadline):stats.readiness()
            finally:release.set()
            self.assertTrue(running.result()['ready'])
        self.assertEqual(call.call_count,1)

    def test_cancellation_deadline_prevents_any_probe_and_failed_output_is_not_exposed(self):
        with bind(ExecutionContext(deadline=time.monotonic()-1)),patch.object(r_worker,'run_limited') as call:
            with self.assertRaises(ExecutionDeadline):r_worker.run_probe(self.executable)
        call.assert_not_called()
        failed=subprocess.CompletedProcess([],2,'PRIVATE_CHILD_OUTPUT','PRIVATE_CHILD_ERROR')
        with patch.object(r_worker,'run_probe',return_value=failed):value=stats.readiness()
        self.assertFalse(value['ready']);self.assertNotIn('PRIVATE_',str(value))
