#!/usr/bin/env python3
"""CI-only container checks against loopback; no model generation or data download.

Diagnostics contain an allowlist of Docker State and PID 1 security fields, never
Config/Env. The authentication password is deliberately inert fixture material.
"""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SMOKE_PASSWORD='ci-smoke-inert-local-password-0123456789'
_RUNTIME_SCRIPT='''import json
from pathlib import Path
keep={"Uid","CapEff","CapPrm","CapAmb","NoNewPrivs"}
fields={}
for line in Path("/proc/1/status").read_text().splitlines():
    name,_,value=line.partition(":")
    if name in keep:fields[name]=value.strip()
print(json.dumps(fields))
'''
_SECRET=re.compile(r'\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+[^\s,;]+|'
                   r'(?:api[_ -]?key|access[_ -]?token|secret|password)\s*[:=]\s*[^\s,;]+',re.I)


class SmokeFailure(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,new_url):return None


def safe_state(value):
    if not isinstance(value,dict):raise SmokeFailure('invalid_container_state')
    result={}
    status=value.get('Status')
    if status in ('created','running','paused','restarting','removing','exited','dead'):result['Status']=status
    for name in ('Running','Paused','Restarting','OOMKilled','Dead'):
        if type(value.get(name)) is bool:result[name]=value[name]
    for name in ('ExitCode','Pid'):
        if type(value.get(name)) is int and 0<=value[name]<=2**31:result[name]=value[name]
    for name in ('StartedAt','FinishedAt'):
        item=value.get(name)
        if isinstance(item,str) and re.fullmatch(r'[0-9TZ:.+\-]{1,64}',item):result[name]=item
    if 'Status' not in result or 'Running' not in result:raise SmokeFailure('invalid_container_state')
    return result


def _docker_json(arguments):
    try:result=subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=5,check=False)
    except (OSError,subprocess.TimeoutExpired):raise SmokeFailure('docker_observation_failed') from None
    if result.returncode:raise SmokeFailure('docker_observation_failed')
    try:return json.loads(result.stdout)
    except (ValueError,TypeError):raise SmokeFailure('invalid_docker_observation') from None


def inspect_container(container):
    return _docker_json(['inspect','--format','{{json .State}}',container])


def runtime_security(container):
    return _docker_json(['exec',container,'python','-c',_RUNTIME_SCRIPT])


def container_logs(container):
    try:
        result=subprocess.run(['docker','logs','--tail','80','--timestamps',container],
                              capture_output=True,text=True,timeout=5,check=False)
        value=result.stdout+'\n'+result.stderr
    except (OSError,subprocess.TimeoutExpired):return '[Container logs unavailable.]'
    return sanitize_logs(value)


def sanitize_logs(value):
    if not isinstance(value,str):return '[Container logs unavailable.]'
    return _SECRET.sub('[REDACTED]',value.replace(SMOKE_PASSWORD,'[INERT_CI_PASSWORD]'))[-16000:]


def http_get(base_url,path,*,authenticated=False,timeout=5):
    headers={}
    if authenticated:
        headers['Authorization']='Basic '+base64.b64encode(('owner:'+SMOKE_PASSWORD).encode()).decode()
    request=urllib.request.Request(base_url+path,headers=headers)
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        with opener.open(request,timeout=timeout) as response:
            body=response.read(1_000_001)
            if len(body)>1_000_000:raise SmokeFailure('oversized_http_response')
            return response.status,body
    except urllib.error.HTTPError as error:
        status=error.code;error.close()
        return status,b''


def run_smoke(container,base_url,*,startup_timeout=30,readiness_timeout=40,interval=.25,
              inspect_state=None,get_http=None,read_runtime=None,read_logs=None,
              clock=None,sleep=None):
    """Return a bounded report on success or failure; dependencies are test seams."""
    if not isinstance(container,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',container):
        raise ValueError('An explicit safe container name is required.')
    parsed=urllib.parse.urlsplit(base_url)
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1')
            or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment):
        raise ValueError('The smoke target must be a loopback HTTP origin.')
    try:parsed.port
    except ValueError:raise ValueError('Invalid smoke target port.') from None
    if not 0<startup_timeout<=120 or not 0<readiness_timeout<=120 or not 0<interval<=5:
        raise ValueError('Invalid smoke deadlines.')
    base_url=base_url.rstrip('/')
    clock,sleep=clock or time.monotonic,sleep or time.sleep
    inspect_state=inspect_state or (lambda:inspect_container(container))
    get_http=get_http or (lambda path,**kwargs:http_get(base_url,path,**kwargs))
    read_runtime=read_runtime or (lambda:runtime_security(container))
    read_logs=read_logs or (lambda:container_logs(container))
    started=clock()
    report={'status':'failed','checks':[],'state_observations':[],'state_inspections':0,
            'phase':'startup','error_code':None,'elapsed_ms':0}

    def observe():
        state=safe_state(inspect_state());report['state_inspections']+=1
        report['last_container_state']=state
        record={'phase':report['phase'],'state':state}
        if not report['state_observations'] or report['state_observations'][-1]!=record:
            report['state_observations'].append(record)
            report['state_observations']=report['state_observations'][-64:]
        if state.get('OOMKilled'):raise SmokeFailure('container_oom_killed')
        if state.get('Dead') or state['Status']=='dead':raise SmokeFailure('container_dead')
        if state['Status'] in ('exited','removing') or (not state['Running'] and state['Status'] not in ('created','restarting')):
            raise SmokeFailure('container_exited')
        if state.get('Paused') or state['Status']=='paused':raise SmokeFailure('container_paused')
        return state

    def payload(body):
        try:value=json.loads(body)
        except (ValueError,TypeError,UnicodeError):raise SmokeFailure('invalid_http_json') from None
        if not isinstance(value,dict):raise SmokeFailure('invalid_http_json')
        return value

    def record(name):report['checks'].append({'name':name,'passed':True})

    def retry(deadline,code):
        observe()  # Detect an exited/OOM process immediately after a refusal.
        remaining=deadline-clock()
        if remaining<=0:raise SmokeFailure(code)
        sleep(min(interval,remaining))

    try:
        deadline=clock()+startup_timeout
        while True:
            observe();remaining=deadline-clock()
            if remaining<=0:raise SmokeFailure('startup_timeout')
            try:status,body=get_http('/healthz',timeout=min(2,remaining))
            except (OSError,TimeoutError):
                retry(deadline,'startup_timeout');continue
            if status==200:
                if payload(body).get('status')!='ok':raise SmokeFailure('invalid_health_status')
                record('unauthenticated_liveness');break
            if status!=503:raise SmokeFailure('unexpected_health_http_status')
            retry(deadline,'startup_timeout')
        report['phase']='authentication';observe()
        try:status,_=get_http('/api/status',timeout=5)
        except (OSError,TimeoutError):
            observe();raise SmokeFailure('private_api_transport_failed') from None
        if status!=401:raise SmokeFailure('private_api_authentication_not_enforced')
        record('private_api_requires_authentication')
        report['phase']='readiness';deadline=clock()+readiness_timeout
        while True:
            observe();remaining=deadline-clock()
            if remaining<=0:raise SmokeFailure('statistics_readiness_timeout')
            try:status,body=get_http('/api/status',authenticated=True,timeout=min(5,remaining))
            except (OSError,TimeoutError):
                retry(deadline,'statistics_readiness_timeout');continue
            if status!=200:raise SmokeFailure('authenticated_api_http_failure')
            state=payload(body)
            statistics,data,dialogue=(state.get(name) for name in ('statistics','data','dialogue'))
            if not all(isinstance(item,dict) for item in (statistics,data,dialogue)):raise SmokeFailure('invalid_status_payload')
            if data.get('ready') is not False:raise SmokeFailure('unexpected_verified_survey_data')
            if dialogue.get('configured') is not False:raise SmokeFailure('unexpected_model_configuration')
            if statistics.get('ready') is True:
                record('real_r_dependency_observation');record('verified_survey_data_unavailable');record('model_connection_not_configured');break
            if statistics.get('status') in ('blocked','failed','unavailable'):raise SmokeFailure('statistics_dependency_check_failed')
            retry(deadline,'statistics_readiness_timeout')
        report['phase']='security';observe()
        security=read_runtime()
        if not isinstance(security,dict):raise SmokeFailure('invalid_runtime_security_observation')
        try:
            uids=[int(value) for value in security['Uid'].split()]
            capabilities={name:int(security[name],16) for name in ('CapEff','CapPrm','CapAmb')}
            no_new_privileges=int(security['NoNewPrivs'])
        except (KeyError,ValueError,TypeError,AttributeError):raise SmokeFailure('invalid_runtime_security_observation') from None
        report['runtime_security']={'uids':uids,**capabilities,'NoNewPrivs':no_new_privileges}
        if len(uids)!=4 or any(value!=10001 for value in uids):raise SmokeFailure('application_uid_not_dropped')
        if any(capabilities.values()):raise SmokeFailure('application_capabilities_not_dropped')
        if no_new_privileges!=1:raise SmokeFailure('no_new_privileges_not_enforced')
        record('pid1_all_uids_10001');record('pid1_effective_permitted_ambient_capabilities_zero');record('no_new_privileges')
        report.update(status='ok',phase='complete')
    except SmokeFailure as error:
        report['error_code']=error.code
    except (OSError,subprocess.SubprocessError,ValueError,TypeError):
        report['error_code']='smoke_observation_failed'
    finally:
        report['elapsed_ms']=round((clock()-started)*1000)
    if report['status']!='ok':
        try:report['container_logs']=sanitize_logs(read_logs())
        except Exception:report['container_logs']='[Container logs unavailable.]'
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container',required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:10000')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:report=run_smoke(args.container,args.base_url)
    except ValueError:
        parser.error('Use a safe container name and a loopback HTTP origin.')
    args.output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)+'\n')
    args.output.chmod(0o600)
    print(json.dumps({key:report[key] for key in ('status','phase','error_code','elapsed_ms')}))
    if report['status']!='ok':
        print('Safe container State: '+json.dumps(report.get('last_container_state',{})),file=sys.stderr)
        print(report.get('container_logs','[Container logs unavailable.]'),file=sys.stderr)
    return 0 if report['status']=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
