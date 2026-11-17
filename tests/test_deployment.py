"""Deployment isolation checks: no real listeners, API calls, R, or downloads.

Credentials below are inert unit-test strings. These checks do not establish
that a cloud service, domain, container build, or live account is available.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from deploy import entrypoint
from nhis_agent import engine
import server


SECRET = 'UNIT_DEPLOYMENT_PASSWORD_NOT_A_REAL_SECRET_12345'


def environment(**changes):
    values = {'WEB_AUTH_SECRET':SECRET,'NHIS_PUBLIC_URL':'https://nhis.example'}
    values.update(changes)
    return values


class DeploymentEnvironmentTests(unittest.TestCase):
    def test_public_https_identity_sets_exact_host_origin_and_safe_defaults(self):
        values=environment()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            port,download=entrypoint.configure_environment(values)
        self.assertEqual(port,10000)
        self.assertFalse(download)
        self.assertEqual(values['PORT'],'10000')
        self.assertEqual(values['NHIS_HOST'],'0.0.0.0')
        self.assertEqual(values['NHIS_STATE_DIR'],'/var/lib/nhis')
        self.assertEqual(values['NHIS_ALLOWED_HOSTS'],'nhis.example')
        self.assertEqual(values['NHIS_ALLOWED_ORIGINS'],'https://nhis.example')
        self.assertEqual(values['NHIS_PUBLIC_ACCESS'],'false')
        self.assertEqual(output.getvalue(),'')

    def test_public_opt_in_preserves_signing_secret_and_sets_bounded_defaults(self):
        values=environment(NHIS_PUBLIC_ACCESS=' TRUE ')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            entrypoint.configure_environment(values)
        self.assertEqual(values['NHIS_PUBLIC_ACCESS'],'true')
        self.assertEqual(values['WEB_AUTH_SECRET'],SECRET)
        self.assertEqual({name:values[name] for name in entrypoint.PUBLIC_LIMITS}, {
            'NHIS_PUBLIC_MESSAGES_PER_HOUR':'20', 'NHIS_PUBLIC_MESSAGES_PER_DAY':'200',
            'NHIS_PUBLIC_CHATS_PER_HOUR':'10', 'NHIS_PUBLIC_CHATS_PER_DAY':'200'})
        self.assertEqual(output.getvalue(),'')

    def test_public_flag_rejects_ambiguous_truthy_and_falsey_values(self):
        for value in ('', '1', '0', 'yes', 'no', 'on', 'off', 'tru', True, False, None):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'NHIS_PUBLIC_ACCESS'):
                entrypoint.configure_environment(environment(NHIS_PUBLIC_ACCESS=value))
        for value in ('false','FALSE',' false '):
            values=environment(NHIS_PUBLIC_ACCESS=value)
            entrypoint.configure_environment(values)
            self.assertEqual(values['NHIS_PUBLIC_ACCESS'],'false')

    def test_public_mode_still_requires_the_server_signing_secret(self):
        for secret in ('','short',' '*40):
            with self.subTest(length=len(secret)),self.assertRaisesRegex(ValueError,'WEB_AUTH_SECRET'):
                entrypoint.configure_environment(environment(NHIS_PUBLIC_ACCESS='true',WEB_AUTH_SECRET=secret))

    def test_public_limit_bounds_and_canonical_integer_values(self):
        for name in entrypoint.PUBLIC_LIMITS:
            for value,expected in [('1','1'),('100000','100000'),(' 020 ','20')]:
                values=environment(**{name:value})
                entrypoint.configure_environment(values)
                self.assertEqual(values[name],expected)
            for value in ('0','-1','100001','1000000','1.5','1e2','+20','20/day','true','',True,20,'２０'):
                with self.subTest(name=name,value=value),self.assertRaisesRegex(ValueError,name) as caught:
                    entrypoint.configure_environment(environment(**{name:value}))
                self.assertNotIn(SECRET,str(caught.exception))

    def test_blueprint_is_public_free_and_generates_a_separate_signing_secret(self):
        text=(entrypoint.CODE_ROOT/'render.yaml').read_text(encoding='utf-8')
        fields={name:(kind,value.strip('"')) for name,kind,value in re.findall(
            r'- key: (\w+)\n\s+(value|sync|generateValue): ([^\n]+)',text)}
        self.assertRegex(text,r'(?m)^    plan: free$')
        self.assertNotRegex(text,r'(?m)^    disk:')
        self.assertEqual(fields['NHIS_PUBLIC_ACCESS'],('value','true'))
        self.assertEqual(fields['WEB_AUTH_SECRET'],('generateValue','true'))
        self.assertEqual(fields['OPENAI_API_KEY'],('sync','false'))
        for name,default in entrypoint.PUBLIC_LIMITS.items():
            self.assertEqual(fields[name],('value',str(default)))

    def test_example_keeps_local_public_access_opt_in_and_contains_no_key(self):
        text=(entrypoint.CODE_ROOT/'.env.example').read_text(encoding='utf-8')
        fields=dict(line.split('=',1) for line in text.splitlines() if line and not line.startswith('#'))
        self.assertEqual(fields['NHIS_PUBLIC_ACCESS'],'false')
        self.assertEqual(fields['WEB_AUTH_SECRET'],'')
        self.assertEqual(fields['OPENAI_API_KEY'],'')
        for name,default in entrypoint.PUBLIC_LIMITS.items():
            self.assertEqual(fields[name],str(default))

    def test_platform_url_and_explicit_custom_allowlists_are_preserved(self):
        values={'WEB_AUTH_SECRET':SECRET,'RENDER_EXTERNAL_URL':'https://unit-platform.example:8443/',
                'PORT':'09000','NHIS_AUTO_DOWNLOAD_DATA':'yes'}
        port,download=entrypoint.configure_environment(values)
        self.assertEqual((port,download),(9000,True))
        self.assertEqual(values['NHIS_ALLOWED_HOSTS'],'unit-platform.example:8443')
        self.assertEqual(values['NHIS_ALLOWED_ORIGINS'],'https://unit-platform.example:8443')
        values=environment(NHIS_ALLOWED_HOSTS='custom.example',NHIS_ALLOWED_ORIGINS='https://custom.example')
        entrypoint.configure_environment(values)
        self.assertEqual(values['NHIS_ALLOWED_HOSTS'],'custom.example')
        self.assertEqual(values['NHIS_ALLOWED_ORIGINS'],'https://custom.example')

    def test_weak_auth_missing_identity_invalid_port_and_download_option_fail(self):
        cases=[{},environment(WEB_AUTH_SECRET='short'),environment(WEB_AUTH_SECRET=' '*40),
               {'WEB_AUTH_SECRET':SECRET},environment(PORT='not-an-integer'),environment(PORT='0'),
               environment(PORT='65536'),environment(NHIS_AUTO_DOWNLOAD_DATA='perhaps')]
        for values in cases:
            with self.subTest(keys=sorted(values)),self.assertRaises(ValueError) as caught:
                entrypoint.configure_environment(dict(values))
            self.assertNotIn(SECRET,str(caught.exception))

    def test_public_url_rejects_credentials_paths_and_non_https_origins(self):
        for url in ('http://nhis.example','https://user:password@nhis.example',
                    'https://nhis.example/path','https://nhis.example/?token=private',
                    'https://nhis.example/#fragment','https://nhis.example:invalid'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                entrypoint.configure_environment(environment(NHIS_PUBLIC_URL=url))


class PersistentStateTests(unittest.TestCase):
    def test_new_state_is_private_and_separate_without_copying_source_or_secrets(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'runtime-state'
            with patch.object(entrypoint.os,'geteuid',return_value=1000), \
                 patch.object(entrypoint.pwd,'getpwnam') as account, \
                 patch.object(entrypoint.os,'setuid') as setuid:
                actual=entrypoint.prepare_state(state)
            self.assertEqual(actual,state.resolve())
            self.assertEqual(set(path.name for path in state.iterdir()),set(entrypoint.STATE_SUBDIRECTORIES))
            for path in (state,*(state/name for name in entrypoint.STATE_SUBDIRECTORIES)):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777,0o700)
            self.assertFalse((state/'.env').exists())
            self.assertFalse((state/'config').exists())
            account.assert_not_called();setuid.assert_not_called()

    def test_source_directory_ancestors_and_descendants_are_rejected(self):
        with TemporaryDirectory() as folder:
            source=(Path(folder)/'application').resolve();source.mkdir()
            with patch.object(entrypoint,'CODE_ROOT',source):
                for path in (source,source/'state',source.parent):
                    with self.subTest(path=str(path)),self.assertRaises(ValueError):
                        entrypoint.prepare_state(path)
            self.assertFalse((source/'state').exists())

    def test_symbolic_link_state_children_are_rejected_without_following_them(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'state';state.mkdir()
            outside=Path(folder)/'outside';outside.mkdir()
            sentinel=outside/'keep.txt';sentinel.write_text('PRESERVED_FIXTURE')
            (state/'data').symlink_to(outside,target_is_directory=True)
            with patch.object(entrypoint.os,'geteuid',return_value=1000),self.assertRaises(ValueError):
                entrypoint.prepare_state(state)
            self.assertEqual(sentinel.read_text(),'PRESERVED_FIXTURE')
            self.assertEqual(list(outside.iterdir()),[sentinel])

    def test_initial_root_setup_drops_identity_with_only_shallow_ownership_changes(self):
        with TemporaryDirectory() as folder:
            state=(Path(folder)/'state').resolve()
            account=SimpleNamespace(pw_uid=1777,pw_gid=1888)
            calls=[]
            with patch.object(entrypoint.os,'geteuid',return_value=0), \
                 patch.object(entrypoint.pwd,'getpwnam',return_value=account), \
                 patch.object(entrypoint.os,'chown') as chown, \
                 patch.object(entrypoint.os,'setgroups',side_effect=lambda value:calls.append(('groups',value))), \
                 patch.object(entrypoint.os,'setgid',side_effect=lambda value:calls.append(('gid',value))), \
                 patch.object(entrypoint.os,'setuid',side_effect=lambda value:calls.append(('uid',value))):
                entrypoint.prepare_state(state)
            self.assertEqual(chown.call_count,1+len(entrypoint.STATE_SUBDIRECTORIES))
            changed={Path(item.args[0]) for item in chown.call_args_list}
            self.assertEqual(changed,{state,*(state/name for name in entrypoint.STATE_SUBDIRECTORIES)})
            self.assertEqual(calls,[('groups',[]),('gid',1888),('uid',1777)])

    def test_unwritable_state_does_not_report_success(self):
        with TemporaryDirectory() as folder:
            with patch.object(entrypoint.os,'geteuid',return_value=1000), \
                 patch.object(entrypoint.os,'access',return_value=False),self.assertRaises(ValueError):
                entrypoint.prepare_state(Path(folder)/'state')

    def test_engine_uses_code_registry_when_state_has_no_source_tree(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'state';source=Path(folder)/'source'
            (source/'config').mkdir(parents=True)
            registry={'test_registry':'SOURCE_FIXTURE','year':2024}
            (source/'config'/'registry.json').write_text(json.dumps(registry))
            with patch.dict(os.environ,{},clear=True),patch.object(engine,'ROOT',source), \
                 patch.object(engine.data,'status',return_value={'ready':False}), \
                 patch.object(engine.stats,'readiness',return_value={'ready':False}):
                agent=engine.Agent(root=state)
                # The captured probe is already mocked; join that observation
                # so this unit fixture also leaves no readiness thread behind.
                actual=agent.status(wait_for_statistics=True)
            self.assertEqual(actual['registry'],registry)
            self.assertTrue((state/'runs').is_dir())
            self.assertFalse((state/'config').exists())
            self.assertFalse((source/'runs').exists())


class ServerStartupTests(unittest.TestCase):
    def test_public_flag_and_signing_secret_are_validated_even_on_loopback(self):
        cases=[{'NHIS_PUBLIC_ACCESS':value,'WEB_AUTH_SECRET':SECRET}
               for value in ('yes','1','','perhaps')]
        cases += [{'NHIS_PUBLIC_ACCESS':'true'},
                  {'NHIS_PUBLIC_ACCESS':'true','WEB_AUTH_SECRET':'short'},
                  {'NHIS_PUBLIC_ACCESS':'true','WEB_AUTH_SECRET':' '*40}]
        for values in cases:
            with self.subTest(keys=sorted(values)),TemporaryDirectory() as folder:
                values=dict(values,NHIS_STATE_DIR=str(Path(folder)/'state'))
                with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment'), \
                     patch('sys.argv',['server.py','--host','127.0.0.1']),patch.object(engine,'Agent'), \
                     patch.object(server,'ThreadingHTTPServer') as listener,patch.object(server,'handler_for') as handler, \
                     contextlib.redirect_stderr(io.StringIO()) as errors,self.assertRaises(SystemExit) as stopped:
                    server.main()
                self.assertEqual(stopped.exception.code,1)
                listener.assert_not_called();handler.assert_not_called()
                self.assertNotIn(SECRET,errors.getvalue())

    def test_public_startup_passes_exact_scope_and_parsed_limits_to_handler(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'runtime'
            values={'NHIS_STATE_DIR':str(state),'NHIS_HOST':'0.0.0.0','WEB_AUTH_SECRET':SECRET,
                    'NHIS_PUBLIC_ACCESS':'true','NHIS_ALLOWED_HOSTS':'nhis.example',
                    'NHIS_ALLOWED_ORIGINS':'https://nhis.example/',
                    'NHIS_PUBLIC_MESSAGES_PER_HOUR':'7','NHIS_PUBLIC_MESSAGES_PER_DAY':'17',
                    'NHIS_PUBLIC_CHATS_PER_HOUR':'3','NHIS_PUBLIC_CHATS_PER_DAY':'13'}
            fixture=MagicMock();listener=MagicMock();listener.server_port=10000
            with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment'), \
                 patch('sys.argv',['server.py','--port','10000']),patch.object(engine,'Agent',return_value=fixture), \
                 patch.object(server,'handler_for',return_value='PUBLIC_FIXTURE_HANDLER') as handler, \
                 patch.object(server,'ThreadingHTTPServer',return_value=listener) as bind, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                server.main()
            handler.assert_called_once_with(fixture,allowed_hosts=['nhis.example'],
                allowed_origins=['https://nhis.example'],auth_secret=SECRET,public_access=True,
                public_limits={'messages_per_hour':7,'messages_per_day':17,
                               'chats_per_hour':3,'chats_per_day':13})
            bind.assert_called_once_with(('0.0.0.0',10000),'PUBLIC_FIXTURE_HANDLER')
            listener.serve_forever.assert_called_once();listener.server_close.assert_called_once()
            self.assertNotIn(SECRET,output.getvalue())

    def test_remote_public_mode_rejects_any_http_origin_before_binding(self):
        for origins in ('http://nhis.example','https://nhis.example,http://other.example'):
            with self.subTest(origins=origins),TemporaryDirectory() as folder:
                values={'NHIS_STATE_DIR':str(Path(folder)/'state'),'NHIS_PUBLIC_ACCESS':'true',
                        'WEB_AUTH_SECRET':SECRET,'NHIS_ALLOWED_HOSTS':'nhis.example',
                        'NHIS_ALLOWED_ORIGINS':origins}
                with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment'), \
                     patch('sys.argv',['server.py','--host','0.0.0.0']),patch.object(engine,'Agent'), \
                     patch.object(server,'ThreadingHTTPServer') as listener,patch.object(server,'handler_for') as handler, \
                     contextlib.redirect_stderr(io.StringIO()) as errors,self.assertRaises(SystemExit) as stopped:
                    server.main()
                self.assertEqual(stopped.exception.code,1)
                self.assertIn('HTTPS',errors.getvalue())
                listener.assert_not_called();handler.assert_not_called()

    def test_public_startup_rejects_invalid_limits_before_binding(self):
        for name in entrypoint.PUBLIC_LIMITS:
            values={'NHIS_PUBLIC_ACCESS':'true','WEB_AUTH_SECRET':SECRET,name:'0'}
            with self.subTest(name=name),TemporaryDirectory() as folder:
                values['NHIS_STATE_DIR']=str(Path(folder)/'state')
                with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment'), \
                     patch('sys.argv',['server.py','--host','127.0.0.1']),patch.object(engine,'Agent'), \
                     patch.object(server,'ThreadingHTTPServer') as listener,patch.object(server,'handler_for') as handler, \
                     contextlib.redirect_stderr(io.StringIO()) as errors,self.assertRaises(SystemExit) as stopped:
                    server.main()
                self.assertEqual(stopped.exception.code,1)
                self.assertIn(name,errors.getvalue())
                listener.assert_not_called();handler.assert_not_called()

    def test_non_loopback_without_all_auth_requirements_never_binds(self):
        cases=[{}, {'WEB_AUTH_SECRET':'short','NHIS_ALLOWED_HOSTS':'nhis.example','NHIS_ALLOWED_ORIGINS':'https://nhis.example'},
               {'WEB_AUTH_SECRET':SECRET,'NHIS_ALLOWED_HOSTS':'nhis.example'},
               {'WEB_AUTH_SECRET':SECRET,'NHIS_ALLOWED_ORIGINS':'https://nhis.example'}]
        for values in cases:
            with self.subTest(keys=sorted(values)),TemporaryDirectory() as folder:
                values=dict(values,NHIS_STATE_DIR=str(Path(folder)/'state'))
                with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment') as loader, \
                     patch('sys.argv',['server.py','--host','0.0.0.0']),patch.object(engine,'Agent') as agent, \
                     patch.object(server,'ThreadingHTTPServer') as listener,patch.object(server,'handler_for') as handler, \
                     contextlib.redirect_stderr(io.StringIO()) as errors,self.assertRaises(SystemExit) as stopped:
                    server.main()
                self.assertEqual(stopped.exception.code,1)
                listener.assert_not_called();handler.assert_not_called();loader.assert_called_once()
                self.assertNotIn(SECRET,errors.getvalue())

    def test_hosted_listener_receives_explicit_state_and_access_configuration(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'runtime'
            values={'NHIS_STATE_DIR':str(state),'NHIS_HOST':'0.0.0.0','WEB_AUTH_SECRET':SECRET,
                    'NHIS_ALLOWED_HOSTS':'nhis.example, second.example',
                    'NHIS_ALLOWED_ORIGINS':'https://nhis.example/,https://second.example'}
            fixture=MagicMock();listener=MagicMock();listener.server_port=10000
            with patch.dict(os.environ,values,clear=True),patch.object(server,'load_environment'), \
                 patch('sys.argv',['server.py','--port','10000']),patch.object(engine,'Agent',return_value=fixture) as constructor, \
                 patch.object(server,'handler_for',return_value='ISOLATED_HANDLER') as handler, \
                 patch.object(server,'ThreadingHTTPServer',return_value=listener) as bind, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                server.main()
            constructor.assert_called_once_with(root=state.resolve())
            handler.assert_called_once_with(fixture,allowed_hosts=['nhis.example','second.example'],
                allowed_origins=['https://nhis.example','https://second.example'],auth_secret=SECRET)
            bind.assert_called_once_with(('0.0.0.0',10000),'ISOLATED_HANDLER')
            listener.serve_forever.assert_called_once();listener.server_close.assert_called_once()
            self.assertNotIn(SECRET,output.getvalue())

    def test_connection_check_artifact_goes_to_state_without_live_generation(self):
        with TemporaryDirectory() as folder:
            state=Path(folder)/'state';source=Path(folder)/'source';source.mkdir()
            fixture=MagicMock()
            fixture.test_connection.return_value={'status':'failed','message':'ISOLATED_TEST_ONLY'}
            with patch.dict(os.environ,{'NHIS_STATE_DIR':str(state)},clear=True),patch.object(server,'load_environment'), \
                 patch.object(server,'ROOT',source),patch('sys.argv',['server.py','--test-api']), \
                 patch.object(engine,'Agent',return_value=fixture) as constructor, \
                 patch.object(server,'ThreadingHTTPServer') as listener, \
                 contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as stopped:
                server.main()
            self.assertEqual(stopped.exception.code,1)
            constructor.assert_called_once_with(root=state.resolve())
            self.assertEqual(json.loads((state/'.cache'/'dialogue-connection-check.json').read_text()),
                             {'status':'failed','message':'ISOLATED_TEST_ONLY'})
            self.assertFalse((source/'.cache').exists())
            listener.assert_not_called()


if __name__=='__main__':
    unittest.main()
