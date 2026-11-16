"""Fault-injected durable execution tests using temporary state and no API/R."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from nhis_agent.execution_context import (ExecutionContext, ExecutionCancelled,
    UnknownExternalAttempt, ReplayAvailable, bind, current)
from nhis_agent.job_store import JobStore, SubmissionConflict, fingerprint
from nhis_agent.jobs import ChatJobs, ConversationBusy
from nhis_agent.access import PublicAccess

CID = 'a'*32
KEY = 'test-message-0001'
REQUEST = {'message':'Hello','measure_model':False,'repeats':5,'prompt_versions':3}


class Service:
    def __init__(self,root):
        self.root=Path(root)
        self.calls=0
        self.started=threading.Event()
        self.release=threading.Event()
        self.handler=None
        self.deleted=False

    def get(self,cid):
        if self.deleted or cid != CID:
            raise FileNotFoundError('This conversation does not exist.')
        return {'id':cid,'messages':[],'snapshot':'CURRENT_ONLY_NOT_STORED'}

    def send(self,cid,text,on_event=None,**options):
        self.calls+=1
        self.started.set()
        if on_event:on_event({'stage':'test','status':'running','facts':{'actual':1}})
        if self.handler:
            return self.handler(current())
        self.release.wait(2)
        return {'conversation_id':cid,'conversation':self.get(cid),
                'message':{'id':'b'*32,'content':'Complete','status':'ok'},'result':None}

    def delete(self,cid):
        self.get(cid)
        self.deleted=True
        return {'deleted':[cid],'deleted_count':1}

    def clear(self):
        return self.delete(CID)


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.service=Service(Path(self.folder.name)/'workspace')
        self.store=JobStore(Path(self.folder.name)/'state')
        self.coordinators=[]
        self.addCleanup(self.stop)
        self.jobs=self.coordinator(self.service,store=self.store)

    def coordinator(self,service=None,**options):
        jobs=ChatJobs(self.service if service is None else service,**options)
        self.coordinators.append(jobs)
        return jobs

    def stop(self):
        # Any coordinator can win a concurrent admission. Release every service
        # first, then join every owner before TemporaryDirectory removes SQLite.
        for jobs in self.coordinators:
            jobs.service.release.set()
        reports=[jobs.shutdown(2) for jobs in reversed(self.coordinators)]
        for report in reports:
            self.assertTrue(report['drained'], 'Fixture workers are still writing: '+str(report['pending_jobs']))

    def finish(self,jobs,jid):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            value=jobs.get(jid)
            if value['execution_state'] not in ('queued','running'):
                return value
            if value.get('recovery_required'):
                return value
            threading.Event().wait(.005)
        self.fail('The isolated test job did not reach a durable outcome.')

    def seed(self,*,external=False,complete=False,checkpoint=False):
        row,_=self.store.create(self.jobs.workspace_id,CID,KEY,fingerprint(REQUEST),REQUEST)
        self.assertTrue(self.store.claim(self.jobs.workspace_id,row['job_id'],'old-worker'))
        ctx=ExecutionContext(store=self.store,workspace_id=self.jobs.workspace_id,job_id=row['job_id'],
            turn_id=row['turn_id'],trace_id=row['trace_id'],owner='old-worker',state_root=self.store.state_root)
        if checkpoint:ctx.save_step('statistics',{'run_id':'SAVED_TEST_RESULT'},'registered-inputs')
        attempt=None
        if external:
            attempt=ctx.external_start('model','planned-call',{'request_hash':'fixed','reservation_id':'original-reservation'})
            if complete:ctx.external_finish(attempt,{'status':'ok','content':'SAVED_PROVIDER_RESULT'})
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=0,lease_expires_at=0 WHERE job_id=?',(row['job_id'],))
        return row,ctx,attempt

    def test_completed_job_survives_a_coordinator_restart_without_execution(self):
        self.service.release.set()
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        complete=self.finish(self.jobs,submitted['id'])
        restored=self.coordinator(self.service,store=JobStore(self.store.state_root))
        repeated=restored.submit(CID,'Hello',client_message_id=KEY)
        self.assertEqual(repeated['id'],submitted['id'])
        self.assertTrue(repeated['idempotent'])
        self.assertEqual(restored.get(repeated['id'])['response']['message'],complete['response']['message'])
        self.assertEqual(self.service.calls,1)
        with self.store.transaction() as connection:
            text=connection.execute('SELECT response_json FROM jobs').fetchone()[0]
        self.assertNotIn('CURRENT_ONLY_NOT_STORED',text)
        self.assertNotIn('conversation"',text)

    def test_concurrent_duplicate_submissions_across_coordinators_execute_once(self):
        other=self.coordinator(self.service,store=JobStore(self.store.state_root))
        gate=threading.Barrier(8)
        def submit(number):
            gate.wait(2)
            return (self.jobs if number%2 else other).submit(CID,'Hello',client_message_id=KEY)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results=list(executor.map(submit,range(8)))
        self.assertEqual(len({item['id'] for item in results}),1)
        self.assertTrue(self.service.started.wait(1))
        self.assertEqual(self.service.calls,1)
        self.service.release.set()
        self.finish(self.jobs,results[0]['id'])

    def test_reusing_a_key_with_different_text_or_options_is_a_conflict(self):
        self.jobs.submit(CID,'Hello',client_message_id=KEY)
        self.assertTrue(self.service.started.wait(1))
        for text,options in [('Different',{}),('Hello',{'repeats':4}),('Hello',{'measure_model':True})]:
            with self.subTest(text=text,options=options),self.assertRaises(SubmissionConflict):
                self.jobs.submit(CID,text,client_message_id=KEY,**options)
        self.assertEqual(self.service.calls,1)

    def test_deliberate_same_text_with_a_new_key_is_a_new_turn(self):
        self.service.release.set()
        first=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        self.finish(self.jobs,first['id'])
        second=self.jobs.submit(CID,'Hello',client_message_id='test-message-0002')
        self.finish(self.jobs,second['id'])
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(self.service.calls,2)

    def test_quota_and_job_rollback_together_and_no_worker_starts(self):
        with self.store.transaction() as connection:
            connection.execute('CREATE TABLE quota_fixture (event TEXT)')
        admission=Future()
        try:
            with self.store.transaction() as connection:
                connection.execute("INSERT INTO quota_fixture VALUES ('accepted')")
                self.jobs.submit(CID,'Hello',client_message_id=KEY,connection=connection,admission=admission)
                raise sqlite3.OperationalError('Injected commit failure before transaction completion')
        except sqlite3.OperationalError:
            admission.set_result(False)
        self.jobs.shutdown(2)
        self.assertEqual(self.service.calls,0)
        with self.store.transaction() as connection:
            for table in ('quota_fixture','jobs','turns'):
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_worker_waits_for_committed_quota_and_job_records(self):
        with self.store.transaction() as connection:
            connection.execute('CREATE TABLE quota_fixture (event TEXT)')
        admission=Future()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO quota_fixture VALUES ('accepted')")
            submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY,connection=connection,admission=admission)
            self.assertFalse(self.service.started.wait(.03))
        self.assertEqual(self.service.calls,0)
        admission.set_result(True)
        self.assertTrue(self.service.started.wait(1))
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM quota_fixture').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)
        self.service.release.set();self.finish(self.jobs,submitted['id'])

    def test_concurrent_public_duplicates_share_one_quota_event_and_one_job(self):
        access=PublicAccess('UNIT_SIGNING_MATERIAL_NOT_REAL_12345678901234567890',
                            state_dir=self.store.state_root,messages_per_day=1)
        other=self.coordinator(self.service,store=self.store)
        gate=threading.Barrier(6)
        def submit(number):
            jobs=self.jobs if number%2 else other
            admission=Future()
            gate.wait(2)
            try:
                reserved=access.reserve('f'*32,
                    lookup=lambda connection:jobs.lookup_submission(CID,'Hello',client_message_id=KEY,connection=connection),
                    accept_transaction=lambda connection:jobs.submit(CID,'Hello',client_message_id=KEY,
                        connection=connection,admission=admission))
                admission.set_result(reserved['allowed'])
                return reserved
            except BaseException:
                if not admission.done():admission.set_result(False)
                raise
        with ThreadPoolExecutor(max_workers=6) as executor:
            decisions=list(executor.map(submit,range(6)))
        self.assertTrue(all(item['allowed'] for item in decisions))
        self.assertEqual(len({item['value']['id'] for item in decisions}),1)
        self.assertTrue(self.service.started.wait(1))
        self.assertEqual(self.service.calls,1)
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accepted_events WHERE kind='message'").fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_cleanup_drains_a_secondary_owner_before_removing_its_state(self):
        other=self.coordinator(self.service,store=self.store)
        finish_entered=threading.Event()
        allow_finish=threading.Event()
        shutdown_entered=threading.Event()
        cleanup_done=threading.Event()
        errors=[]
        original_finish=self.store.finish
        original_shutdown=other.shutdown

        def gated_finish(*args,**kwargs):
            finish_entered.set()
            if not allow_finish.wait(3):
                raise AssertionError('The cleanup test did not release its final writer.')
            return original_finish(*args,**kwargs)

        def observed_shutdown(*args,**kwargs):
            shutdown_entered.set()
            return original_shutdown(*args,**kwargs)

        def cleanup():
            try:self.stop()
            except BaseException as error:errors.append(error)
            finally:cleanup_done.set()

        with patch.object(self.store,'finish',side_effect=gated_finish), \
             patch.object(other,'shutdown',side_effect=observed_shutdown):
            other.submit(CID,'Hello',client_message_id=KEY)
            self.assertTrue(self.service.started.wait(1))
            thread=threading.Thread(target=cleanup)
            thread.start()
            try:
                self.assertTrue(shutdown_entered.wait(1),'Cleanup did not join the secondary owner.')
                self.assertTrue(finish_entered.wait(1))
                self.assertFalse(cleanup_done.is_set(),'State cleanup passed an unfinished SQLite writer.')
                self.assertTrue(Path(self.folder.name).is_dir())
            finally:
                allow_finish.set()
                thread.join(3)
                # Also drain on assertion failure, so the red regression itself
                # cannot leave a writer racing TemporaryDirectory.cleanup.
                original_shutdown(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertTrue(cleanup_done.is_set())
        self.folder.cleanup()
        self.assertFalse(Path(self.folder.name).exists())

    def test_saved_local_step_is_reused_only_after_explicit_recovery(self):
        row,_,_=self.seed(checkpoint=True)
        restored=self.coordinator(self.service,store=self.store)
        waiting=restored.get(row['job_id'])
        self.assertTrue(waiting['recovery_required'])
        self.assertTrue(waiting['can_resume'])
        self.assertEqual(self.service.calls,0)
        def use_saved(ctx):
            self.assertEqual(ctx.load_step('statistics','registered-inputs'),{'run_id':'SAVED_TEST_RESULT'})
            return {'conversation_id':CID,'message':{'content':'Used the saved local step','status':'ok'}}
        self.service.handler=use_saved
        resumed=restored.resume(row['job_id'])
        self.assertEqual(resumed['id'],row['job_id'])
        self.assertEqual(self.finish(restored,row['job_id'])['execution_state'],'succeeded')

    def test_unfinished_external_attempt_is_unknown_and_cannot_be_resent(self):
        row,_,_=self.seed(external=True)
        restored=self.coordinator(self.service,store=self.store)
        unknown=restored.get(row['job_id'])
        self.assertEqual(unknown['execution_state'],'unknown')
        self.assertTrue(unknown['recovery_required']);self.assertFalse(unknown['can_resume'])
        self.assertEqual(unknown['external_attempts'][0]['state'],'unknown')
        repeated=restored.submit(CID,'Hello',client_message_id=KEY)
        self.assertEqual(repeated['id'],row['job_id'])
        self.assertFalse(repeated['can_resume'])
        with self.assertRaises(ConversationBusy):restored.resume(row['job_id'])
        self.assertEqual(self.service.calls,0)

    def test_saved_provider_result_replays_without_a_new_attempt_or_reservation(self):
        row,_,_=self.seed(external=True,complete=True)
        restored=self.coordinator(self.service,store=self.store)
        def use_saved(ctx):
            result=ctx.external_cached('planned-call',{'request_hash':'fixed','reservation_id':'different-new-reservation'})
            self.assertEqual(result,{'status':'ok','content':'SAVED_PROVIDER_RESULT'})
            with self.assertRaises(ReplayAvailable):
                ctx.external_start('model','planned-call',{'request_hash':'fixed','reservation_id':'another'})
            return {'conversation_id':CID,'message':{'content':result['content'],'status':'ok'}}
        self.service.handler=use_saved
        restored.resume(row['job_id'])
        self.assertEqual(self.finish(restored,row['job_id'])['execution_state'],'succeeded')
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM external_attempts').fetchone()[0],1)

    def test_other_live_coordinator_does_not_reconcile_an_active_lease(self):
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        self.assertTrue(self.service.started.wait(1))
        other=self.coordinator(self.service,store=self.store)
        self.assertEqual(other.get(submitted['id'])['execution_state'],'running')
        self.assertFalse(other.get(submitted['id'])['recovery_required'])
        self.assertEqual(self.service.calls,1)

    def test_queued_cancel_and_running_cancel_distinguish_request_from_effect(self):
        admission=Future()
        queued=self.jobs.submit(CID,'Hello',client_message_id=KEY,admission=admission)
        cancelled=self.jobs.request_cancel(queued['id'])
        self.assertEqual(cancelled['execution_state'],'cancelled')
        admission.set_result(True)
        self.assertEqual(self.service.calls,0)
        self.jobs.shutdown(2)

    def test_running_cooperative_cancellation_preserves_the_request_flag(self):
        def cooperative(ctx):
            self.service.release.wait(2)
            ctx.check_cancelled()
            raise AssertionError('Cancelled work must not continue')
        self.service.handler=cooperative
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        self.assertTrue(self.service.started.wait(1))
        requested=self.jobs.request_cancel(submitted['id'])
        self.assertTrue(requested['cancel_requested'])
        self.assertEqual(requested['execution_state'],'running')
        self.service.release.set()
        final=self.finish(self.jobs,submitted['id'])
        self.assertEqual(final['execution_state'],'cancelled')
        self.assertTrue(final['cancel_requested'])

    def test_late_cancellation_does_not_erase_a_committed_success(self):
        self.service.release.set()
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        self.finish(self.jobs,submitted['id'])
        result=self.jobs.request_cancel(submitted['id'])
        self.assertEqual(result['execution_state'],'succeeded')
        self.assertEqual(result['cancellation_effect'],'too_late')
        self.assertIn('response',result)

    def test_unknown_attempt_cannot_be_swallowed_into_a_successful_fallback(self):
        def swallowed(ctx):
            attempt=ctx.external_start('model','planned-call',{'request_hash':'uncertain'})
            ctx.external_unknown(attempt)
            return {'conversation_id':CID,'message':{'content':'A local notice','status':'ok'}}
        self.service.handler=swallowed
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        result=self.finish(self.jobs,submitted['id'])
        self.assertEqual(result['execution_state'],'unknown')
        self.assertFalse(result['can_resume'])

    def test_workspace_ownership_applies_to_get_cancel_and_recovery(self):
        submitted=self.jobs.submit(CID,'Hello',client_message_id=KEY)
        other=self.coordinator(Service(Path(self.folder.name)/'other'),store=self.store)
        for action in (other.get,other.request_cancel,other.resume):
            with self.assertRaises(FileNotFoundError):action(submitted['id'])
        self.assertEqual(self.jobs.latest_job(CID)['client_message_id'],KEY)

    def test_checkpoints_are_immutable_and_linked_turn_messages_do_not_change(self):
        row,ctx,_=self.seed(checkpoint=True)
        # Restore its lease just for pure storage integrity checks.
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=?,lease_expires_at=? WHERE job_id=?',(os.getpid(),time.time()+30,row['job_id']))
        with self.assertRaises(SubmissionConflict):ctx.save_step('statistics',{'different':1},'registered-inputs')
        with self.assertRaises(SubmissionConflict):ctx.load_step('statistics','changed-inputs')
        ctx.link_messages(user_message_id='c'*32)
        ctx.link_messages(user_message_id='c'*32)
        with self.assertRaises(SubmissionConflict):ctx.link_messages(user_message_id='d'*32)
        self.assertEqual(self.store.get(self.jobs.workspace_id,row['job_id'])['user_message_id'],'c'*32)

    def test_context_is_local_and_attempt_storage_excludes_credentials_and_prompts(self):
        row,ctx,_=self.seed()
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=?,lease_expires_at=? WHERE job_id=?',(os.getpid(),time.time()+30,row['job_id']))
        self.assertIsNone(current())
        with bind(ctx):
            self.assertIs(current(),ctx)
            identifier=ctx.external_start('model','private-metadata',{'request_hash':'safe',
                'api_key':'UNIT_PRIVATE_CREDENTIAL','prompt':'PRIVATE_RAW_PROMPT'})
            ctx.external_finish(identifier,{'status':'failed','message':'Bearer UNIT_PRIVATE_BEARER_VALUE'})
        self.assertIsNone(current())
        with self.store.transaction() as connection:
            saved=' '.join(str(item[0]) for item in connection.execute('SELECT request_meta_json FROM external_attempts'))
            saved+=' '+str(connection.execute('SELECT result_json FROM external_attempts').fetchone()[0])
        for secret in ('UNIT_PRIVATE_CREDENTIAL','PRIVATE_RAW_PROMPT','UNIT_PRIVATE_BEARER_VALUE'):
            self.assertNotIn(secret,saved)

    def test_existing_quota_rows_and_legacy_conversation_files_are_not_migrated_or_rewritten(self):
        root=Path(self.folder.name)/'legacy'
        directory=root/'.cache';directory.mkdir(parents=True)
        path=directory/'public-access.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE accepted_events (id INTEGER PRIMARY KEY, visitor TEXT, kind TEXT, accepted_at REAL)')
            connection.execute("INSERT INTO accepted_events VALUES (1,'fixture','message',1000)")
        legacy=root/'conversation-sentinel.json';legacy.write_text('{"preserved":"fixture"}')
        JobStore(root);JobStore(root)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute('SELECT visitor,kind,accepted_at FROM accepted_events').fetchall(),[('fixture','message',1000.0)])
        self.assertEqual(legacy.read_text(),'{"preserved":"fixture"}')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(directory.stat().st_mode & 0o777,0o700)

    def test_deletion_purges_message_copies_but_keeps_nonresurrectable_audit_tombstones(self):
        def reply(ctx):
            ctx.save_step('saved-dialogue',{'content':'ASSISTANT_PRIVATE_COPY'},'fixture')
            attempt=ctx.external_start('model','fixture-call',{'request_hash':'fixed','purpose':'chat'})
            ctx.external_finish(attempt,{'status':'ok','content':'ASSISTANT_PRIVATE_COPY',
                                         'usage':{'input_tokens':3,'output_tokens':2}})
            return {'conversation_id':CID,'message':{'content':'ASSISTANT_PRIVATE_COPY','status':'ok'}}
        self.service.handler=reply
        submitted=self.jobs.submit(CID,'USER_PRIVATE_PROMPT',client_message_id=KEY)
        self.finish(self.jobs,submitted['id'])
        self.jobs.delete_conversation(CID)
        for action in (self.jobs.get,self.jobs.resume,self.jobs.request_cancel):
            with self.assertRaises(FileNotFoundError):action(submitted['id'])
        with self.assertRaises(FileNotFoundError):self.jobs.submit(CID,'USER_PRIVATE_PROMPT',client_message_id=KEY)
        with self.store.transaction() as connection:
            row=connection.execute('SELECT request_json,response_json,deleted FROM turns JOIN jobs USING(turn_id)').fetchone()
            self.assertEqual(tuple(row),('{}',None,1))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM step_results').fetchone()[0],0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM job_events').fetchone()[0],0)
            audit=json.loads(connection.execute('SELECT result_json FROM external_attempts').fetchone()[0])
            self.assertEqual(audit['usage'],{'input_tokens':3,'output_tokens':2})
            text=json.dumps(audit)
            self.assertNotIn('ASSISTANT_PRIVATE_COPY',text)
            self.assertNotIn('USER_PRIVATE_PROMPT',text)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_pre_dispatch_crash_is_recoverable_but_never_automatically_dispatched(self):
        row,_=self.store.create(self.jobs.workspace_id,CID,KEY,fingerprint(REQUEST),REQUEST)
        with self.store.transaction() as connection:
            connection.execute('UPDATE jobs SET owner_pid=0,lease_expires_at=0 WHERE job_id=?',(row['job_id'],))
        restored=self.coordinator(self.service,store=self.store)
        state=restored.get(row['job_id'])
        self.assertEqual(state['execution_state'],'queued')
        self.assertTrue(state['recovery_required']);self.assertTrue(state['can_resume'])
        self.assertEqual(self.service.calls,0)
        self.service.release.set()
        restored.resume(row['job_id'])
        self.assertEqual(self.finish(restored,row['job_id'])['execution_state'],'succeeded')

    def test_heartbeat_renews_the_persisted_worker_lease(self):
        jobs=self.coordinator(self.service,store=self.store,lease_seconds=1,heartbeat_interval=.02)
        submitted=jobs.submit(CID,'Hello',client_message_id=KEY)
        self.assertTrue(self.service.started.wait(1))
        first=self.store.get(jobs.workspace_id,submitted['id'])['lease_expires_at']
        threading.Event().wait(.07)
        second=self.store.get(jobs.workspace_id,submitted['id'])['lease_expires_at']
        self.assertGreater(second,first)
        self.service.release.set();self.finish(jobs,submitted['id']);jobs.shutdown(1)

    def test_global_capacity_is_atomic_across_distinct_visitor_coordinators(self):
        coordinators=[]
        try:
            for number in range(5):
                service=Service(Path(self.folder.name)/('visitor-'+str(number)))
                jobs=self.coordinator(service,store=JobStore(self.store.state_root))
                coordinators.append((service,jobs))
                if number<4:
                    jobs.submit(CID,'Hello',client_message_id=KEY)
                else:
                    with self.assertRaises(ConversationBusy):jobs.submit(CID,'Hello',client_message_id=KEY)
                    self.assertEqual(service.calls,0)
            with self.store.transaction() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0],4)
        finally:
            for service,jobs in coordinators:
                service.release.set();jobs.shutdown(2)

    def test_failed_external_result_is_preserved_as_that_attempt_not_replaced_by_success(self):
        row,ctx,attempt=self.seed(external=True)
        ctx.external_finish(attempt,{'status':'failed','error_code':'refused','content':''})
        restored=self.coordinator(self.service,store=self.store)
        def retain_failure(context):
            saved=context.external_cached('planned-call',{'request_hash':'fixed'})
            self.assertEqual(saved['error_code'],'refused')
            with self.assertRaises(ReplayAvailable):
                context.external_start('model','planned-call',{'request_hash':'fixed'})
            return {'conversation_id':CID,'message':{'content':'The recorded attempt was refused.','status':'ok'}}
        self.service.handler=retain_failure
        restored.resume(row['job_id'])
        self.assertEqual(self.finish(restored,row['job_id'])['execution_state'],'succeeded')
        self.assertEqual(self.store.external(self.jobs.workspace_id,row['job_id'],'planned-call')['result']['error_code'],'refused')


if __name__=='__main__':unittest.main()
