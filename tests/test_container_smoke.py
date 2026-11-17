"""Inert Docker/HTTP fixture checks; no container, listener, API or data is used."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts import smoke_container as smoke


RUNNING={'Status':'running','Running':True,'Paused':False,'Restarting':False,
         'OOMKilled':False,'Dead':False,'Pid':123,'ExitCode':0}
SECURITY={'Uid':'10001\t10001\t10001\t10001','CapEff':'0000000000000000',
          'CapPrm':'0000000000000000','CapAmb':'0000000000000000','NoNewPrivs':'1'}


class Clock:
    def __init__(self):self.now=0
    def __call__(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class Harness:
    def __init__(self):
        self.clock=Clock();self.requests=[];self.refusals=0;self.pending=0
        self.state=dict(RUNNING);self.security=dict(SECURITY)
        self.status={'statistics':{'ready':True,'status':'ok'},'data':{'ready':False},'dialogue':{'configured':False}}
        self.inspect=Mock(side_effect=lambda:dict(self.state))
        self.logs=Mock(return_value='Fixture initialization diagnostic.')
        self.read_runtime=Mock(side_effect=lambda:dict(self.security))

    def get(self,path,*,authenticated=False,timeout=5):
        self.requests.append((path,authenticated,timeout))
        if path=='/healthz':
            if self.refusals:
                self.refusals-=1;raise ConnectionRefusedError('INERT_REFUSAL_DETAIL')
            return 200,b'{"status":"ok"}'
        if not authenticated:return 401,b''
        value=json.loads(json.dumps(self.status))
        if self.pending:
            self.pending-=1;value['statistics']={'ready':False,'status':'checking'}
        return 200,json.dumps(value).encode()

    def run(self,**kwargs):
        options={'startup_timeout':.5,'readiness_timeout':.5,'interval':.1,
                 'inspect_state':self.inspect,'get_http':self.get,'read_runtime':self.read_runtime,
                 'read_logs':self.logs,'clock':self.clock,'sleep':self.clock.sleep}
        options.update(kwargs)
        return smoke.run_smoke('inert-smoke','http://127.0.0.1:10000',**options)


class ContainerSmokeTests(unittest.TestCase):
    def test_connection_refusal_then_healthy_and_observed_readiness_succeed(self):
        fixture=Harness();fixture.refusals=2;fixture.pending=1
        report=fixture.run()
        self.assertEqual(report['status'],'ok');self.assertIsNone(report['error_code'])
        self.assertEqual(report['runtime_security']['uids'],[10001]*4)
        self.assertEqual(report['runtime_security']['CapEff'],0)
        self.assertEqual(report['runtime_security']['CapPrm'],0)
        self.assertEqual(report['runtime_security']['CapAmb'],0)
        self.assertEqual(report['runtime_security']['NoNewPrivs'],1)
        names={item['name'] for item in report['checks']}
        self.assertTrue({'unauthenticated_liveness','private_api_requires_authentication',
                         'real_r_dependency_observation','verified_survey_data_unavailable',
                         'model_connection_not_configured'}.issubset(names))
        self.assertEqual([path for path,_,_ in fixture.requests[:3]],['/healthz']*3)
        self.assertGreaterEqual(report['state_inspections'],len(fixture.requests))
        fixture.logs.assert_not_called()

    def test_exited_container_after_first_refusal_stops_without_sleeping_or_retrying(self):
        fixture=Harness();fixture.refusals=10
        fixture.inspect=Mock(side_effect=[dict(RUNNING),dict(RUNNING,Status='exited',Running=False,ExitCode=1,
                                               Error='PRIVATE_DOCKER_ERROR',Config={'Env':['PRIVATE_ENV']})])
        report=fixture.run()
        self.assertEqual(report['error_code'],'container_exited')
        self.assertEqual(report['phase'],'startup');self.assertEqual(report['elapsed_ms'],0)
        self.assertEqual(len(fixture.requests),1);fixture.logs.assert_called_once()
        self.assertEqual(report['last_container_state']['ExitCode'],1)
        self.assertNotIn('PRIVATE_',json.dumps(report));self.assertNotIn('Config',json.dumps(report))

    def test_oom_is_immediate_before_any_http_attempt(self):
        fixture=Harness();fixture.state.update(Status='exited',Running=False,OOMKilled=True,ExitCode=137)
        report=fixture.run()
        self.assertEqual(report['error_code'],'container_oom_killed')
        self.assertEqual(report['last_container_state']['ExitCode'],137)
        self.assertEqual(fixture.requests,[]);fixture.read_runtime.assert_not_called()

    def test_running_container_with_no_listener_reaches_a_real_bounded_failure(self):
        fixture=Harness();fixture.refusals=10000
        report=fixture.run()
        self.assertEqual(report['status'],'failed');self.assertEqual(report['error_code'],'startup_timeout')
        self.assertLessEqual(report['elapsed_ms'],600)
        self.assertLessEqual(len(fixture.requests),6)
        self.assertNotIn('INERT_REFUSAL_DETAIL',json.dumps(report))

    def test_readiness_timeout_and_failed_r_observation_never_pass(self):
        fixture=Harness();fixture.pending=10000
        report=fixture.run()
        self.assertEqual(report['error_code'],'statistics_readiness_timeout')
        self.assertEqual(report['phase'],'readiness');fixture.read_runtime.assert_not_called()
        fixture=Harness();fixture.status['statistics']={'ready':False,'status':'blocked'}
        report=fixture.run()
        self.assertEqual(report['error_code'],'statistics_dependency_check_failed')
        self.assertEqual(report['elapsed_ms'],0)

    def test_private_api_auth_and_configured_data_model_checks_remain_mandatory(self):
        fixture=Harness();original=fixture.get
        def no_auth(path,**kwargs):
            return (200,b'{}') if path=='/api/status' and not kwargs.get('authenticated') else original(path,**kwargs)
        self.assertEqual(fixture.run(get_http=no_auth)['error_code'],'private_api_authentication_not_enforced')
        fixture=Harness();fixture.status['data']['ready']=True
        self.assertEqual(fixture.run()['error_code'],'unexpected_verified_survey_data')
        fixture=Harness();fixture.status['dialogue']['configured']=True
        self.assertEqual(fixture.run()['error_code'],'unexpected_model_configuration')
        fixture=Harness();original=fixture.get
        def rejected(path,**kwargs):
            return (401,b'') if kwargs.get('authenticated') else original(path,**kwargs)
        self.assertEqual(fixture.run(get_http=rejected)['error_code'],'authenticated_api_http_failure')

    def test_every_pid1_uid_capability_and_no_new_privileges_is_enforced(self):
        cases=[('Uid','0 10001 10001 10001','application_uid_not_dropped'),
               ('Uid','10001 10001 10001','application_uid_not_dropped'),
               ('CapEff','1','application_capabilities_not_dropped'),
               ('CapPrm','1','application_capabilities_not_dropped'),
               ('CapAmb','1','application_capabilities_not_dropped'),
               ('NoNewPrivs','0','no_new_privileges_not_enforced')]
        for name,value,expected in cases:
            with self.subTest(name=name,value=value):
                fixture=Harness();fixture.security[name]=value
                self.assertEqual(fixture.run()['error_code'],expected)

    def test_state_and_logs_exclude_environment_and_redact_credential_shapes(self):
        fixture=Harness();fixture.state.update(Status='exited',Running=False,ExitCode=1,
                                             Env=['PRIVATE_ENV'],Config={'Env':['PRIVATE_CONFIG_ENV']})
        fixture.logs=Mock(return_value='permission denied\napi_key=INERT_PRIVATE_CREDENTIAL\n'+smoke.SMOKE_PASSWORD)
        report=fixture.run();serialized=json.dumps(report)
        for private in ('PRIVATE_ENV','PRIVATE_CONFIG_ENV','INERT_PRIVATE_CREDENTIAL',smoke.SMOKE_PASSWORD):
            self.assertNotIn(private,serialized)
        self.assertIn('permission denied',report['container_logs'])
        self.assertLessEqual(len(smoke.sanitize_logs('x'*20000)),16000)

    def test_http_bypasses_proxy_environment_and_refuses_redirects(self):
        response=Mock();response.status=200;response.read.return_value=b'{"status":"ok"}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch.dict(os.environ,{'HTTP_PROXY':'http://inert.invalid:3128','HTTPS_PROXY':'http://inert.invalid:3128'}), \
             patch.object(smoke.urllib.request,'build_opener',return_value=opener) as build:
            self.assertEqual(smoke.http_get('http://127.0.0.1:10000','/healthz')[0],200)
        handlers=build.call_args.args
        self.assertTrue(any(isinstance(item,smoke.urllib.request.ProxyHandler) and item.proxies=={} for item in handlers))
        self.assertTrue(any(isinstance(item,smoke._NoRedirect) for item in handlers))
        self.assertIsNone(smoke._NoRedirect().redirect_request(None,None,302,'redirect',{},'https://external.invalid'))

    def test_cli_failure_writes_safe_state_artifact_and_returns_nonzero(self):
        fixture=Harness();fixture.state.update(Status='exited',Running=False,ExitCode=1)
        report=fixture.run()
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'safe-state.json';stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(smoke,'run_smoke',return_value=report),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=smoke.main(['--container','inert-smoke','--output',str(output)])
            self.assertEqual(code,1);self.assertEqual(json.loads(output.read_text())['error_code'],'container_exited')
            self.assertIn('Safe container State',stderr.getvalue());self.assertIn('Fixture initialization diagnostic',stderr.getvalue())
            self.assertEqual(output.stat().st_mode & 0o777,0o600)

    def test_remote_urls_or_unsafe_container_names_are_rejected_before_observation(self):
        for name,url in [('--privileged','http://127.0.0.1:10000'),('safe','https://external.invalid'),
                         ('safe','http://user:password@127.0.0.1:10000'),('safe','http://127.0.0.1:10000/path')]:
            with self.subTest(name=name,url=url),self.assertRaises(ValueError):smoke.run_smoke(name,url)
