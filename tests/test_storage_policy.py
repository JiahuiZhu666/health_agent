"""All state, archives and cleanup targets are isolated temporary fixtures.

No user state, real survey rows, credentials, API or R process is accessed.
"""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from nhis_agent import storage_policy as storage
from nhis_agent.job_store import JobStore, fingerprint, workspace_identity
from scripts import state_admin


RUN='20261116T065024Z-14468f42ea'


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)
        self.root=self.base/'state';self.root.mkdir()

    def conversation(self, number=1, *, root=None, run=None, stamp=None):
        root=root or self.root;cid=f'{number:032x}'
        value={'id':cid,'title':'Synthetic chat '+str(number),'created_at':'2026-01-01T00:00:00Z',
               'updated_at':stamp or f'2026-01-{number:02}T00:00:00Z',
               'messages':[{'role':'user','content':'Synthetic fixture only'}],
               'context':{},'last_run_id':run}
        path=root/'conversations'/(cid+'.json');path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value))
        return value,path

    def make_run(self, *, root=None, measured=False):
        directory=(root or self.root)/'runs'/RUN;directory.mkdir(parents=True,exist_ok=True)
        (directory/'registration.json').write_text(json.dumps({'spec':{},'measure_model':measured}))
        (directory/'result.json').write_text(json.dumps({'run_id':RUN,'status':'ok','measurement':{'status':'ok' if measured else 'not_requested','planned_calls':1 if measured else 0}}))
        return directory

    @staticmethod
    def old(root, days=500):
        timestamp=time.time()-days*86400
        for base,directories,files in os.walk(root,topdown=False):
            for name in files+directories:os.utime(Path(base)/name,(timestamp,timestamp))
        os.utime(root,(timestamp,timestamp))

    def test_byte_quotas_replace_existing_size_and_exclude_inputs_and_backups(self):
        with patch.dict(os.environ,{'NHIS_STORAGE_CONVERSATION_BYTES':'15','NHIS_STORAGE_WORKSPACE_BYTES':'20','NHIS_STORAGE_RUN_BYTES':'8'}):
            policy=storage.StoragePolicy(self.root)
        path=self.root/'conversations'/('a'*32+'.json');path.parent.mkdir();path.write_bytes(b'x'*10)
        other=self.root/'runs'/RUN/'fixture.bin';other.parent.mkdir(parents=True);other.write_bytes(b'x'*8)
        for directory in ('data','backups','previews','upgrades','.venv','.r-env'):
            excluded=self.root/directory/'excluded';excluded.parent.mkdir(exist_ok=True);excluded.write_bytes(b'x'*100)
        (self.root/'.env').write_text('PRIVATE_NOT_READ_OR_COPIED')
        self.assertEqual(policy.status()['bytes'],18)
        policy.check_conversation_write(path,12)
        with self.assertRaises(storage.StorageLimit):policy.check_conversation_write(path,13)
        with self.assertRaises(storage.StorageLimit) as raised:policy.check_conversation_write(path,16)
        self.assertEqual(raised.exception.code,'conversation_storage_limit')
        with self.assertRaises(storage.StorageLimit):policy.check_run_admission()
        other.write_bytes(b'x'*9)
        with self.assertRaises(storage.StorageLimit):policy.check_run_size(other.parent)

    def test_paths_and_symlinks_cannot_escape_managed_workspace(self):
        policy=storage.StoragePolicy(self.root)
        for path in (self.base/'outside.json',self.root/'conversations'/'../bad.json',self.root/'data'/('a'*32+'.json')):
            with self.assertRaises(ValueError):policy.check_conversation_write(path,1)
        outside=self.base/'outside';outside.mkdir();(outside/'preserve').write_text('keep')
        (self.root/'conversations').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):policy.check_conversation_write(self.root/'conversations'/('a'*32+'.json'),1)
        with self.assertRaises(ValueError):storage.ConversationIndex(self.root)
        with self.assertRaises(ValueError):storage.create_backup(self.root,self.base/'forbidden.tar.gz')
        self.assertEqual((outside/'preserve').read_text(),'keep')

    def test_write_guard_serializes_quota_check_and_replace(self):
        with patch.dict(os.environ,{'NHIS_STORAGE_CONVERSATION_BYTES':'100','NHIS_STORAGE_WORKSPACE_BYTES':'100'}):
            first,second=storage.StoragePolicy(self.root),storage.StoragePolicy(self.root)
        (self.root/'conversations').mkdir()
        def save(item):
            policy=first if item==1 else second
            path=self.root/'conversations'/(f'{item:032x}'+'.json')
            try:
                with policy.write_guard():
                    policy.check_conversation_write(path,75)
                    time.sleep(.01)
                    path.write_bytes(b'x'*75)
                return True
            except storage.StorageLimit:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            accepted=list(pool.map(save,[1,2]))
        self.assertEqual(sum(accepted),1)
        self.assertEqual(first.status()['bytes'],75)

    def test_index_migrates_only_changed_files_and_removes_deleted_entries(self):
        one,path=self.conversation(1);self.conversation(2)
        index=storage.ConversationIndex(self.root)
        with patch.object(storage,'_read_json',wraps=storage._read_json) as read:
            self.assertEqual(index.ensure()['updated'],2)
            self.assertEqual(read.call_count,2)
        with patch.object(storage,'_read_json',side_effect=AssertionError('Unchanged JSON must not be parsed')):
            self.assertEqual(len(index.page()['conversations']),2)
        one['title']='Changed title';path.write_text(json.dumps(one))
        with patch.object(storage,'_read_json',wraps=storage._read_json) as read:
            self.assertEqual(index.ensure()['updated'],1)
            self.assertEqual(read.call_count,1)
        path.unlink();self.assertEqual(len(index.all_summaries()),1)
        self.assertEqual(os.stat(index.path).st_mode & 0o777,0o600)

    def test_index_skips_corrupt_mismatched_and_symlink_files_and_is_bounded(self):
        self.conversation(1)
        directory=self.root/'conversations'
        (directory/('2'*32+'.json')).write_text('broken')
        wrong,_=self.conversation(3);wrong['id']='f'*32
        (directory/(f'{3:032x}'+'.json')).write_text(json.dumps(wrong))
        (directory/('4'*32+'.json')).symlink_to(self.base/'missing')
        (directory/'arbitrary.json').write_text('{}')
        index=storage.ConversationIndex(self.root)
        self.assertEqual(len(index.all_summaries()),1)
        with patch.dict(os.environ,{'NHIS_STORAGE_INDEX_MAX_FILES':'1'}):
            bounded=storage.ConversationIndex(self.root)
        with self.assertRaises(storage.StorageLimit):bounded.ensure()

    def test_index_keyset_pagination_and_workspace_isolation(self):
        for number in range(1,6):self.conversation(number,stamp='2026-01-01T00:00:00Z')
        index=storage.ConversationIndex(self.root)
        found=[];cursor=None
        for _ in range(3):
            page=index.page(2,cursor);found.extend(row['id'] for row in page['conversations']);cursor=page['next_cursor']
        self.assertIsNone(cursor);self.assertEqual(len(found),5);self.assertEqual(len(set(found)),5)
        self.assertEqual(found,sorted(found,reverse=True))
        other=self.base/'other';other.mkdir();self.conversation(99,root=other)
        self.assertEqual(len(storage.ConversationIndex(other).all_summaries()),1)
        for cursor in ('../escape','***','W10','null'):
            with self.assertRaises(ValueError):index.page(cursor=cursor)
        for limit in (0,True,201):
            with self.assertRaises(ValueError):index.page(limit)

    def test_index_record_accepts_metadata_without_reopening_payload_and_validates_path(self):
        value,path=self.conversation();index=storage.ConversationIndex(self.root)
        with patch.object(storage,'_read_json',side_effect=AssertionError('record must reuse saved object')):
            index.record(value,path)
            self.assertEqual(index.page()['conversations'][0]['message_count'],1)
        with self.assertRaises(ValueError):index.record(value,self.base/'other.json')
        index.remove([value['id']]);path.unlink()
        self.assertEqual(index.all_summaries(),[])

    def test_backup_and_independent_restore_include_committed_wal_exclude_secrets_and_inputs(self):
        value,path=self.conversation();self.make_run()
        visitor=self.root/'visitors'/('b'*32);self.conversation(2,root=visitor)
        cache=self.root/'.cache';cache.mkdir()
        db=sqlite3.connect(cache/'public-access.sqlite3')
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE fixture(value INTEGER)')
            db.execute('INSERT INTO fixture VALUES(42)');db.commit()
            self.assertTrue((cache/'public-access.sqlite3-wal').exists())
            for name in ('.env','data/adult24.csv','.venv/excluded','backups/excluded','upgrades/excluded'):
                excluded=self.root/name;excluded.parent.mkdir(exist_ok=True,parents=True);excluded.write_text('PRIVATE_EXCLUDED_FIXTURE')
            archive=self.base/'state.tar.gz';created=storage.create_backup(self.root,archive)
            self.assertGreater(created['file_count'],0)
            with tarfile.open(archive) as tar:
                names=tar.getnames()
                self.assertFalse(any('.env' in name or name.startswith('data/') or name.endswith('-wal') for name in names))
            restored=self.base/'restored';result=storage.restore_backup(archive,restored)
            self.assertTrue(result['valid']);self.assertEqual(result['conversations'],2)
            copy=sqlite3.connect(restored/'.cache'/'public-access.sqlite3')
            try:self.assertEqual(copy.execute('SELECT value FROM fixture').fetchone()[0],42)
            finally:copy.close()
            self.assertEqual(json.loads((restored/'conversations'/path.name).read_text())['id'],value['id'])
            self.assertTrue(path.exists());self.assertFalse((restored/'.env').exists())
            self.assertEqual(os.stat(restored).st_mode & 0o777,0o700)
            for directory,children,_ in os.walk(restored):
                for child in children:self.assertEqual(os.stat(Path(directory)/child).st_mode & 0o777,0o700)
            self.assertEqual(os.stat(archive).st_mode & 0o777,0o600)
        finally:db.close()

    def malicious_archive(self, name, content=b'{}', *, kind=None, digest=None, schema=1, duplicate=False):
        archive=self.base/('attack-'+str(len(list(self.base.glob('attack-*'))))+'.tar.gz')
        item={'path':name,'bytes':len(content),'sha256':digest or hashlib.sha256(content).hexdigest(),'sqlite':False}
        manifest=json.dumps({'format':'nhis-agent-state','schema_version':schema,'files':[item]}).encode()
        with tarfile.open(archive,'w:gz') as tar:
            header=tarfile.TarInfo('manifest.json');header.size=len(manifest);tar.addfile(header,io.BytesIO(manifest))
            for _ in range(2 if duplicate else 1):
                header=tarfile.TarInfo(name)
                if kind is not None:header.type=kind;header.linkname='../outside'
                else:header.size=len(content)
                tar.addfile(header,None if kind is not None else io.BytesIO(content))
        return archive

    def test_restore_rejects_traversal_symlinks_duplicates_checksums_and_future_schema(self):
        safe='conversations/'+('a'*32)+'.json'
        attacks=[('../outside',{}),('/absolute',{}),('.env',{}),('data/adult24.csv',{}),
                 ('visitors/invalid/conversations/'+('a'*32)+'.json',{}),
                 (safe,{'kind':tarfile.SYMTYPE}),(safe,{'kind':tarfile.LNKTYPE}),
                 (safe,{'duplicate':True}),(safe,{'digest':'0'*64}),(safe,{'schema':2}),(safe,{'schema':True})]
        for index,(name,options) in enumerate(attacks):
            with self.subTest(name=name,options=options):
                archive=self.malicious_archive(name,**options)
                destination=self.base/('rejected-'+str(index))
                with self.assertRaises((ValueError,storage.StorageLimit)):storage.restore_backup(archive,destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.base/'outside').exists())

    def test_backup_and_restore_never_overwrite_and_fail_atomically(self):
        self.conversation();archive=self.base/'archive.tar.gz';storage.create_backup(self.root,archive)
        before=archive.read_bytes()
        with self.assertRaises(FileExistsError):storage.create_backup(self.root,archive)
        self.assertEqual(archive.read_bytes(),before)
        existing=self.base/'existing';existing.mkdir();(existing/'preserve').write_text('keep')
        with self.assertRaises(FileExistsError):storage.restore_backup(archive,existing)
        self.assertEqual((existing/'preserve').read_text(),'keep')
        with self.assertRaises(ValueError):storage.create_backup(self.root,self.root/'.cache'/'unsafe.tar.gz')
        cache=self.root/'.cache';cache.mkdir(exist_ok=True);(cache/'broken.sqlite3').write_bytes(b'not a database')
        failed=self.base/'failed.tar.gz'
        with self.assertRaises(sqlite3.DatabaseError):storage.create_backup(self.root,failed)
        self.assertFalse(failed.exists())

    def test_restore_validates_database_migration_before_publication(self):
        cache=self.root/'.cache';cache.mkdir();db=sqlite3.connect(cache/'public-access.sqlite3')
        db.execute('CREATE TABLE execution_schema(component TEXT,version INTEGER)');db.execute("INSERT INTO execution_schema VALUES('durable_jobs',999)");db.commit();db.close()
        archive=self.base/'future.tar.gz';storage.create_backup(self.root,archive)
        with self.assertRaises(ValueError):storage.restore_backup(archive,self.base/'unsupported')
        self.assertFalse((self.base/'unsupported').exists())

    def test_retention_is_dry_run_and_preserves_active_references_and_measurement_audit(self):
        expired=self.root/'visitors'/('a'*32);self.conversation(1,root=expired);self.old(expired)
        measured=self.root/'visitors'/('b'*32);self.conversation(2,root=measured);self.make_run(root=measured,measured=True);self.old(measured)
        referenced=self.root/'visitors'/('c'*32);self.conversation(3,root=referenced,run=RUN);self.make_run(root=referenced);self.old(referenced)
        active=self.root/'visitors'/('d'*32);self.conversation(4,root=active);self.old(active)
        store=JobStore(self.root);store.create(workspace_identity(active),'4'*32,'active-message',fingerprint({}),{})
        cache=self.root/'.cache'/'survey'/('e'*64+'.json');cache.parent.mkdir();cache.write_text('{}');self.old(cache.parent)
        (self.root/'.env').write_text('PRIVATE_PRESERVE')
        plan=storage.plan_retention(self.root)
        self.assertTrue(plan['dry_run']);self.assertTrue(expired.exists());self.assertTrue(cache.exists())
        paths={item.get('path') for item in plan['actions']}
        self.assertIn('visitors/'+expired.name,paths)
        self.assertIn('.cache/survey/'+cache.name,paths)
        protected={item['reason'] for item in plan['protected']}
        self.assertTrue({'active_jobs','referenced_results','measurement_audit'}.issubset(protected))
        self.assertEqual(plan['archive_candidate_count'],1)
        applied=storage.apply_retention(self.root,plan)
        self.assertEqual(applied['applied_count'],2)
        self.assertFalse(expired.exists());self.assertFalse(cache.exists())
        for path in (measured,referenced,active,self.root/'.env'):self.assertTrue(path.exists())

    def test_retention_revalidates_changed_state_and_never_deletes_unknown_inputs(self):
        expired=self.root/'visitors'/('a'*32);_,path=self.conversation(1,root=expired);self.old(expired)
        plan=storage.plan_retention(self.root);path.write_text(path.read_text()+' ')
        applied=storage.apply_retention(self.root,plan)
        self.assertEqual(applied['applied_count'],0);self.assertTrue(expired.exists())
        (expired/'.env').write_text('PRESERVE');self.old(expired)
        plan=storage.plan_retention(self.root)
        self.assertFalse(any(item.get('path')=='visitors/'+expired.name for item in plan['actions']))

    def test_job_payload_retention_keeps_idempotency_and_all_external_audit(self):
        old=time.time()-400*86400;store=JobStore(self.root,clock=lambda:old)
        jobs=[]
        for number in (1,2):
            row,_=store.create('workspace',f'{number:032x}','message-'+str(number),fingerprint({'number':number}),{'text':'PRIVATE_PAYLOAD'})
            self.assertTrue(store.claim('workspace',row['job_id'],'owner'))
            if number==2:
                attempt=store.start_external('workspace',row['job_id'],'owner','blind:fixture','model',{'purpose':'blind'})
                store.finish_external('workspace',row['job_id'],attempt,{'status':'ok','content':'PRIVATE_AUDIT'})
            store.finish('workspace',row['job_id'],'owner','succeeded',response={'content':'PRIVATE_RESPONSE'})
            jobs.append(row)
        plan=storage.plan_retention(self.root)
        self.assertEqual([item['job_id'] for item in plan['actions'] if item['kind']=='prune_job_payload'],[jobs[0]['job_id']])
        storage.apply_retention(self.root,plan)
        self.assertEqual(store.get('workspace',jobs[0]['job_id'])['state'],'succeeded')
        self.assertEqual(store.request('workspace',jobs[0]['job_id']),{})
        self.assertEqual(store.request('workspace',jobs[1]['job_id'])['text'],'PRIVATE_PAYLOAD')
        self.assertEqual(store.get('workspace',jobs[1]['job_id'])['response']['content'],'PRIVATE_RESPONSE')

    def test_cli_retention_previews_by_default_and_errors_never_print_bodies(self):
        expired=self.root/'visitors'/('a'*32);self.conversation(1,root=expired);self.old(expired)
        output=io.StringIO()
        with patch('sys.stdout',output):
            status=state_admin.main(['retention','--root',str(self.root)])
        self.assertEqual(status,0);self.assertTrue(json.loads(output.getvalue())['dry_run'])
        self.assertTrue(expired.exists())
        bad=self.base/'bad.tar.gz';bad.write_text('PRIVATE_RAW_BODY_AND_CONFIGURATION')
        error=io.StringIO()
        with patch('sys.stderr',error):
            status=state_admin.main(['restore','--archive',str(bad),'--destination',str(self.base/'new-copy')])
        self.assertEqual(status,2)
        self.assertNotIn('PRIVATE',error.getvalue());self.assertNotIn('Traceback',error.getvalue())
        self.assertFalse((self.base/'new-copy').exists())
