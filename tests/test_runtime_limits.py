"""Resource boundary tests: fake HTTP sockets and real isolated local children.

No HTTP listener, model request, production data or credential file is used.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from nhis_agent import r_worker,stats
from nhis_agent.runtime import BoundedHTTPServer
from nhis_agent.execution_context import ExecutionCancelled,ExecutionContext
from nhis_agent.job_store import JobStore,fingerprint


class Connection:
    def __init__(self):
        self.closed=threading.Event()
        self.output=bytearray()
        self.timeouts=[]
    def settimeout(self,value):self.timeouts.append(value)
    def sendall(self,value):self.output.extend(value)
    def shutdown(self,*args):self.closed.set()
    def close(self):self.closed.set()


class HttpBoundaryTests(unittest.TestCase):
    def server(self,handler,**kwargs):
        # TCPServer creates a socket even when bind_and_activate=False. Replace
        # it as well so these checks never open a network listener/socket.
        with patch('socket.socket',return_value=Connection()):
            server=BoundedHTTPServer(('127.0.0.1',0),handler,bind_and_activate=False,**kwargs)
        self.addCleanup(server.server_close)
        return server

    def test_defaults_bound_threads_backlog_reads_and_absolute_lifetime(self):
        with patch.dict(os.environ,{},clear=True):server=self.server(lambda *args:None)
        self.assertEqual(server.max_requests,32)
        self.assertEqual(server.request_queue_size,64)
        self.assertEqual(server.read_timeout,10)
        self.assertEqual(server.connection_deadline,30)

    def test_overload_is_rejected_before_spawning_another_request_thread(self):
        started=threading.Event();calls=[]
        def handler(request,*args):
            calls.append(request);started.set();request.closed.wait(2)
        server=self.server(handler,max_requests=1)
        first,second=Connection(),Connection()
        server.process_request(first,('fixture',1))
        self.assertTrue(started.wait(1))
        server.process_request(second,('fixture',2))
        self.assertEqual(len(calls),1)
        self.assertIn(b'503 Service Unavailable',second.output)
        self.assertIn(b'Retry-After: 1',second.output)
        self.assertTrue(second.closed.is_set())
        self.assertEqual(first.timeouts,[10])
        self.assertEqual(server.active_connections,1)

    def test_dribble_activity_cannot_extend_the_original_connection_deadline(self):
        now=[100.0];started=threading.Event()
        def handler(request,*args):started.set();request.closed.wait(2)
        server=self.server(handler,clock=lambda:now[0],connection_deadline=30)
        request=Connection();server.process_request(request,('fixture',1))
        self.assertTrue(started.wait(1))
        now[0]=120;request.sendall(b'activity')
        self.assertEqual(server.expire_connections(),0)
        self.assertFalse(request.closed.is_set())
        now[0]=131
        self.assertEqual(server.expire_connections(),1)
        self.assertTrue(request.closed.wait(1))

    def test_close_is_bounded_and_closes_active_connections(self):
        started=threading.Event()
        def handler(request,*args):started.set();request.closed.wait(2)
        server=self.server(handler)
        request=Connection();server.process_request(request,('fixture',1))
        self.assertTrue(started.wait(1))
        before=time.monotonic();server.server_close()
        self.assertLess(time.monotonic()-before,1)
        self.assertTrue(request.closed.is_set())
        self.assertFalse(server._watchdog.is_alive())

    def test_thread_start_failure_releases_capacity_and_closes_socket(self):
        started=threading.Event()
        server=self.server(lambda *args:started.set(),max_requests=1)
        first=Connection()
        with patch('threading.Thread.start',side_effect=RuntimeError('Injected start failure')):
            with self.assertRaises(RuntimeError):server.process_request(first,('fixture',1))
        self.assertTrue(first.closed.is_set())
        server.process_request(Connection(),('fixture',2))
        self.assertTrue(started.wait(1))

    def test_shutdown_before_serve_does_not_wait_for_an_unstarted_loop(self):
        server=self.server(lambda *args:None)
        thread=threading.Thread(target=server.shutdown,daemon=True)
        thread.start();thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server._closing.is_set())

    def test_invalid_configuration_fails_before_creating_a_socket(self):
        for name,value in [('NHIS_HTTP_MAX_THREADS','0'),('NHIS_HTTP_MAX_THREADS','100000'),
                           ('NHIS_HTTP_READ_TIMEOUT','nan'),('NHIS_HTTP_CONNECTION_DEADLINE','0')]:
            with self.subTest(name=name,value=value),patch.dict(os.environ,{name:value},clear=True), \
                 patch('socket.socket') as socket_factory,self.assertRaises(ValueError):
                BoundedHTTPServer(('fixture',0),lambda *args:None,bind_and_activate=False)
            socket_factory.assert_not_called()


class CancelContext:
    def __init__(self):self.cancelled=threading.Event();self.started=threading.Event();self.checks=0
    def check_cancelled(self):
        self.checks+=1
        if self.checks>=2:self.started.set()
        if self.cancelled.is_set():raise ExecutionCancelled('Cancelled fixture')


class RWorkerBoundaryTests(unittest.TestCase):
    def command(self,code):return [sys.executable,'-c',code]

    def test_real_child_receives_limits_and_no_server_credentials(self):
        code=('import json,os,resource;print(json.dumps({"env":dict(os.environ),'
              '"cpu":resource.getrlimit(resource.RLIMIT_CPU),'
              '"file":resource.getrlimit(resource.RLIMIT_FSIZE),'
              '"as":resource.getrlimit(resource.RLIMIT_AS)}))')
        secrets={'OPENAI_API_KEY':'UNIT_NOT_REAL_PRIVATE_KEY','WEB_AUTH_SECRET':'UNIT_NOT_REAL_SIGNING_SECRET',
                 'NHIS_MODEL_BASE_URL':'https://private.invalid','NHIS_MODEL_NAME':'PRIVATE_MODEL'}
        with patch.dict(os.environ,secrets):
            result=r_worker.run_limited(self.command(code),timeout=3,cpu_seconds=2,memory_mb=64,output_bytes=8192)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.assertEqual(observed['cpu'],[2,3])
        self.assertEqual(observed['file'],[8192,8192])
        self.assertEqual(observed['env']['R_MAX_VSIZE'],'64M')
        self.assertEqual(observed['env']['OMP_NUM_THREADS'],'1')
        for key in secrets:self.assertNotIn(key,observed['env'])
        self.assertNotIn('UNIT_NOT_REAL',result.stdout)
        if sys.platform.startswith('linux'):self.assertEqual(observed['as'],[64*1024*1024]*2)

    def test_real_wall_timeout_kills_and_reaps_the_child(self):
        with TemporaryDirectory() as folder:
            marker=Path(folder)/'pid'
            code='import os,time;open('+repr(str(marker))+',"w").write(str(os.getpid()));time.sleep(10)'
            start=time.monotonic()
            with self.assertRaises(r_worker.RWorkerTimeout):
                r_worker.run_limited(self.command(code),timeout=.25)
            self.assertLess(time.monotonic()-start,2)
            if marker.exists():
                with self.assertRaises(ProcessLookupError):os.kill(int(marker.read_text()),0)

    def test_real_child_output_is_bounded_without_unbounded_pipes(self):
        code='import os;data=b"x"*4096\nwhile True: os.write(1,data)'
        with self.assertRaises(r_worker.RWorkerOutputLimit):
            r_worker.run_limited(self.command(code),timeout=3,output_bytes=2048)

    @unittest.skipUnless(sys.platform.startswith('linux'),'Linux address-space limit; macOS uses R vector-heap limits.')
    def test_linux_child_cannot_allocate_past_its_address_space_limit(self):
        result=r_worker.run_limited(self.command('x=bytearray(256*1024*1024)'),timeout=3,memory_mb=64)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('MemoryError',result.stderr)

    @unittest.skipUnless(sys.platform=='darwin','macOS-specific R vector-heap policy.')
    def test_macos_real_r_vector_heap_is_capped_without_address_space_limits(self):
        project=Path(__file__).resolve().parents[1]/'.r-env'/'bin'/'Rscript'
        executable=str(project) if project.is_file() else shutil.which('Rscript')
        if not executable:self.skipTest('Rscript is unavailable for the optional vector-heap check.')
        result=r_worker.run_limited([executable,'--vanilla','-e','x <- numeric(25e6); cat(length(x))'],
                                    timeout=5,cpu_seconds=3,memory_mb=64,output_bytes=8192)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('memory',result.stderr.lower())

    def test_cancellation_kills_the_child_and_frees_worker_capacity(self):
        context=CancelContext();outcome=[]
        def run():
            try:r_worker.run_limited(self.command('import time;time.sleep(10)'),timeout=5,context=context)
            except BaseException as error:outcome.append(type(error))
        thread=threading.Thread(target=run);thread.start()
        self.assertTrue(context.started.wait(1));context.cancelled.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(outcome,[ExecutionCancelled])
        result=r_worker.run_limited(self.command('print("slot released")'),timeout=3)
        self.assertEqual(result.stdout.strip(),'slot released')

    def test_parallel_call_is_refused_before_spawning_an_extra_child(self):
        context=CancelContext()
        with ThreadPoolExecutor(max_workers=1) as executor:
            first=executor.submit(r_worker.run_limited,self.command('import time;time.sleep(.2)'),timeout=3,context=context)
            self.assertTrue(context.started.wait(1))
            with self.assertRaises(r_worker.RWorkerBusy):
                r_worker.run_limited(self.command('print("not executed")'),timeout=3)
            first.result(timeout=3)

    def test_statistical_step_budget_is_persisted_per_turn(self):
        with TemporaryDirectory() as folder:
            store=JobStore(folder)
            row,_=store.create('workspace','a'*32,'fixture-key',fingerprint({'message':'fixture'}),{'message':'fixture'})
            self.assertTrue(store.claim('workspace',row['job_id'],'owner'))
            context=ExecutionContext(store=store,workspace_id='workspace',turn_id=row['turn_id'],
                job_id=row['job_id'],trace_id=row['trace_id'],owner='owner',state_root=folder)
            for _ in range(2):
                result=r_worker.run_limited(self.command('print("fixture")'),timeout=3,context=context,step_limit=2)
                self.assertEqual(result.returncode,0)
            with patch.object(r_worker.subprocess,'Popen') as spawn,self.assertRaises(r_worker.RStepBudgetExceeded):
                r_worker.run_limited(self.command('print("not executed")'),context=context,step_limit=2)
            spawn.assert_not_called()
            with store.transaction() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM statistical_attempts').fetchone()[0],2)

    def test_production_entrypoint_refuses_arbitrary_r_code_and_foreign_scripts(self):
        for command in (['Rscript','-e','system("arbitrary")'],
                        ['Rscript','--vanilla','/tmp/foreign.R','a','b','c','d']):
            with patch.object(r_worker.subprocess,'Popen') as spawn,self.assertRaises(ValueError):
                r_worker.run_r(command)
            spawn.assert_not_called()

    def test_stats_rejects_oversized_result_before_reading_json(self):
        with TemporaryDirectory() as folder:
            def fake(command,**kwargs):
                Path(command[5]).write_text(json.dumps({'status':'ok','rows':[],'padding':'x'*2048}))
                return subprocess.CompletedProcess(command,0,'','')
            with patch.object(stats,'_preflight',return_value={'path':'fixture.csv'}), \
                 patch.object(stats,'readiness',return_value={'ready':True,'rscript':'Rscript'}), \
                 patch.object(stats.r_worker,'run_r',side_effect=fake), \
                 patch.object(stats.r_worker,'limits',return_value={'output_bytes':1024}):
                result=stats.analyze('fixture.csv',{},folder)
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(result['code'],'r_output_limit')
            self.assertEqual(result['rows'],[])


if __name__=='__main__':unittest.main()
