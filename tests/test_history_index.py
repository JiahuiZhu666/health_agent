"""Derived history tests use temporary run metadata only, never personal state."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nhis_agent import history_index
from nhis_agent.history_index import HistoryIndex
from nhis_agent.engine import Agent


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.index=HistoryIndex(self.root)

    def write(self,number,*,status='ok',question=None):
        identifier='20261116T120000Z-{:010x}'.format(number)
        directory=self.root/'runs'/identifier;directory.mkdir(parents=True,exist_ok=True)
        registration={'spec':{'analysis':'proportion','group_by':'education','outcome':'insured'},
                      'question':question or 'FIXTURE_QUESTION_'+str(number),
                      'registered_at':'2026-11-16T12:00:00+00:00'}
        result={'run_id':identifier,'status':status,'measurement':{'model':'PRIVATE_MODEL_FIXTURE',
                'calls':[{'raw_response':'RAW_PROVIDER_BODY_NOT_FOR_INDEX'}]}}
        (directory/'registration.json').write_text(json.dumps(registration))
        (directory/'result.json').write_text(json.dumps(result))
        return identifier,directory

    def test_page_shape_order_cursor_and_model_metadata_are_backward_compatible(self):
        identifiers=[self.write(number)[0] for number in range(7)]
        first=self.index.page(3)
        self.assertEqual([item['run_id'] for item in first['runs']],list(reversed(identifiers))[:3])
        self.assertEqual(first['next_cursor'],identifiers[4])
        self.assertEqual(first['schema_version'],1)
        second=self.index.page(3,before=first['next_cursor'])
        third=self.index.page(3,before=second['next_cursor'])
        self.assertEqual([item['run_id'] for page in (first,second,third) for item in page['runs']],list(reversed(identifiers)))
        self.assertIsNone(third['next_cursor'])
        item=first['runs'][0]
        self.assertEqual(set(item),{'run_id','question','status','group_by','analysis','outcome','model','created_at'})
        self.assertEqual(item['model'],'PRIVATE_MODEL_FIXTURE')
        with sqlite3.connect(self.index.db_path) as connection:
            text=' '.join(row[0] for row in connection.execute('SELECT summary_json FROM run_summaries'))
        self.assertNotIn('RAW_PROVIDER_BODY_NOT_FOR_INDEX',text)

    def test_unchanged_valid_and_invalid_files_are_not_parsed_again(self):
        self.write(1);_,bad=self.write(2,status='running')
        with patch.object(history_index,'_json',wraps=history_index._json) as parse:
            self.assertEqual(len(self.index.page()['runs']),1)
            self.assertEqual(parse.call_count,4)
        with patch.object(history_index,'_json',side_effect=AssertionError('Unchanged files must not be parsed')):
            self.assertEqual(len(self.index.page()['runs']),1)
        (bad/'report.md').write_text('An unrelated report file does not change result/registration fingerprints.')
        with patch.object(history_index,'_json',side_effect=AssertionError('Directory mtime is not the file fingerprint')):
            self.index.page()

    def test_changed_result_is_reindexed_and_pending_run_can_finish(self):
        identifier,directory=self.write(1,status='running')
        self.assertEqual(self.index.page()['runs'],[])
        value=json.loads((directory/'result.json').read_text());value['status']='blocked'
        (directory/'result.json').write_text(json.dumps(value))
        page=self.index.page()
        self.assertEqual(page['runs'][0]['run_id'],identifier)
        self.assertEqual(page['runs'][0]['status'],'blocked')

    def test_deleted_or_now_malformed_source_cannot_keep_a_stale_summary(self):
        _,first=self.write(1);_,second=self.write(2)
        self.assertEqual(len(self.index.page()['runs']),2)
        (first/'result.json').write_text('{invalid')
        shutil.rmtree(second)
        self.assertEqual(self.index.page()['runs'],[])
        shutil.rmtree(self.root/'runs')
        self.assertEqual(self.index.page()['runs'],[])

    def test_symlinked_run_artifacts_are_never_read(self):
        identifier,directory=self.write(1)
        outside=self.root/'outside.json';outside.write_text('{"PRIVATE":"MUST_NOT_READ"}')
        (directory/'result.json').unlink();(directory/'result.json').symlink_to(outside)
        with patch.object(history_index,'_json',side_effect=AssertionError('A link must not be parsed')):
            self.assertEqual(self.index.page()['runs'],[])
        shutil.rmtree(directory)
        directory.symlink_to(self.root,target_is_directory=True)
        self.assertEqual(self.index.page()['runs'],[])
        self.assertEqual(outside.read_text(),'{"PRIVATE":"MUST_NOT_READ"}')

    def test_large_and_nonregular_artifacts_are_skipped_before_json_read(self):
        _,directory=self.write(1)
        with (directory/'result.json').open('wb') as stream:
            stream.seek(history_index.MAX_RESULT_BYTES);stream.write(b'x')
        with patch.object(history_index,'_json',side_effect=AssertionError('Large files must not be read')):
            self.assertEqual(self.index.page()['runs'],[])
        (directory/'result.json').unlink()
        if hasattr(os,'mkfifo'):
            os.mkfifo(directory/'result.json')
            self.assertEqual(self.index.page()['runs'],[])

    def test_malformed_duplicates_nonfinite_values_and_wrong_run_ids_are_skipped(self):
        for number,text in enumerate(('[1,2]','{"status":"ok","status":"blocked"}',
                                      '{"status":"ok","value":NaN}','{"status":"ok","run_id":"other"}')):
            _,directory=self.write(number)
            (directory/'result.json').write_text(text)
        self.assertEqual(self.index.page()['runs'],[])

    def test_cursor_is_owned_by_this_workspace_and_cannot_be_a_path(self):
        self.write(1)
        for cursor in ('../data/adult24.csv','bad','20261116T120000Z-0000000022',True):
            with self.subTest(cursor=cursor),self.assertRaises(ValueError):self.index.page(before=cursor)
        with TemporaryDirectory() as other:
            foreign=HistoryIndex(other)
            self.assertEqual(foreign.page()['runs'],[])
            with self.assertRaises(ValueError):foreign.page(before='20261116T120000Z-0000000001')

    def test_bounded_import_progress_can_be_paged_without_skipping_older_runs(self):
        identifiers=[self.write(number)[0] for number in range(6)]
        index=HistoryIndex(self.root,import_limit=2)
        cursor=None;found=[]
        for _ in range(5):
            page=index.page(30,before=cursor)
            found.extend(item['run_id'] for item in page['runs'])
            cursor=page['next_cursor']
            if cursor is None:break
        self.assertEqual(found,list(reversed(identifiers)))
        self.assertEqual(len(found),len(set(found)))

    def test_metadata_directory_scan_is_bounded(self):
        for number in range(3):self.write(number)
        index=HistoryIndex(self.root,scan_limit=2)
        with self.assertRaisesRegex(ValueError,'bounded'):index.page()

    def test_write_through_hook_stores_only_small_summary_without_reparsing_results(self):
        identifier,_=self.write(1)
        summary={'run_id':identifier,'question':'Trusted saved fixture summary','status':'ok',
                 'group_by':'income','analysis':'proportion','outcome':'uninsured','model':None,
                 'created_at':'2026-11-16T12:00:00+00:00'}
        with patch.object(history_index,'_json',side_effect=AssertionError('Write-through summaries need no body read')):
            self.assertTrue(self.index.record(identifier,summary))
            self.assertEqual(self.index.page()['runs'],[summary])

    def test_agent_history_uses_index_without_initializing_a_model_or_reading_environment(self):
        identifier,_=self.write(1)
        agent=Agent.__new__(Agent);agent.root=self.root
        self.assertEqual(agent.history(1)[0]['run_id'],identifier)
        self.assertEqual(agent.history_page(1)['runs'],agent.history(1))
        for value in (True,0,101):
            with self.assertRaises(ValueError):agent.history(value)

    def test_index_permissions_and_future_schema_guard(self):
        self.assertEqual(self.index.db_path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.index.db_path.parent.stat().st_mode & 0o777,0o700)
        with sqlite3.connect(self.index.db_path) as connection:connection.execute('UPDATE history_schema SET version=99')
        with self.assertRaisesRegex(ValueError,'schema'):HistoryIndex(self.root)


if __name__=='__main__':unittest.main()
