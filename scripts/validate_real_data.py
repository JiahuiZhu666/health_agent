#!/usr/bin/env python3
"""Controlled real-NHIS bridge regression against a local Git baseline.

No model calls, downloads, environment-file reads, or production run writes.
Baseline code must already exist in this local, trusted Git repository. Outputs
are private validation artifacts, not independent R/Stata certification.
"""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from nhis_agent import data, stats

CASES=(
    {'name':'education_original','spec':{'year':2024,'outcome':'fair_poor','analysis':'proportion','group_by':'education','filters':{}}},
    {'name':'uninsured_income_gradient','spec':{'year':2024,'outcome':'uninsured','analysis':'gradient','group_by':'income','filters':{}}},
    {'name':'delayed_care_overall','spec':{'year':2024,'outcome':'delayed_care_cost','analysis':'proportion','group_by':'overall','filters':{}}},
)


def numerical_checks(actual,expected,path='root'):
    checks=[]
    if isinstance(expected,bool):
        if actual!=expected:raise ValueError('Boolean validation mismatch at '+path)
    elif isinstance(expected,(int,float)):
        if not isinstance(actual,(int,float)) or not math.isfinite(actual) or not math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-12):
            raise ValueError('Numerical validation mismatch at '+path)
        checks.append(path)
    elif isinstance(expected,list):
        if not isinstance(actual,list) or len(actual)!=len(expected):raise ValueError('Array validation mismatch at '+path)
        for index,(left,right) in enumerate(zip(actual,expected)):checks.extend(numerical_checks(left,right,path+'.'+str(index)))
    elif isinstance(expected,dict):
        for key,value in expected.items():
            if key in ('created_at','provenance','runtime','artifacts'):continue
            if key not in actual:raise ValueError('Missing validation field at '+path+'.'+key)
            checks.extend(numerical_checks(actual[key],value,path+'.'+key))
    elif actual!=expected:
        raise ValueError('Categorical validation mismatch at '+path)
    return checks


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',default=str(ROOT))
    parser.add_argument('--baseline-ref',default='upgrade-baseline-caa5825')
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    report={'schema_version':1,'status':'failed','data_kind':'official NHIS 2024','independent_stata':'pending',
            'purpose':'Same-input numerical regression of the upgraded Python/R execution boundary; not independent variance validation.','cases':[]}
    destination=Path(args.output).expanduser().resolve()
    if destination.exists():parser.error('The report destination must be a new file.')
    stage='official_input_check'
    try:
        dataset=data.status(args.data_root)
        if not dataset.get('ready') or not dataset.get('income_ready'):raise ValueError('Verified official adult and income releases are required.')
        stage='runtime_check';runtime=stats.readiness()
        if not runtime.get('ready'):raise ValueError('The real statistical runtime is required.')
        stage='baseline_export'
        reference=subprocess.run(['git','rev-parse','--verify',args.baseline_ref+'^{commit}'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
        archive=subprocess.run(['git','archive',reference],cwd=ROOT,capture_output=True,check=True).stdout
        report.update(baseline_commit=reference,runtime=runtime,
            data_sha256={name:item.get('sha256') for name,item in dataset.get('datasets',{}).items()},
            source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                ('nhis_agent/stats.py','nhis_agent/r_worker.py','scripts/analyze.R','config/registry.json')})
        environment={key:value for key,value in os.environ.items() if not any(word in key.upper() for word in ('KEY','SECRET','TOKEN','PASSWORD','AUTH','MODEL','PROVIDER'))}
        environment['OPENAI_API_KEY']=''
        with tempfile.TemporaryDirectory(prefix='nhis-controlled-real-') as temporary:
            base=(Path(temporary)/'baseline').resolve();base.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                for member in source.getmembers():
                    target=base/member.name
                    if member.issym() or member.islnk() or not target.resolve().is_relative_to(base):raise ValueError('Unsafe baseline archive member.')
                source.extractall(base)
            local_r=ROOT/'.r-env'
            if local_r.is_dir():(base/'.r-env').symlink_to(local_r,target_is_directory=True)
            for case in CASES:
                stage=case['name']+':baseline'
                baseline_dir=Path(temporary)/(case['name']+'-baseline')
                current_dir=Path(temporary)/(case['name']+'-current')
                code='import json,sys;from nhis_agent.stats import analyze;print(json.dumps(analyze(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3]),allow_nan=False))'
                process=subprocess.run([sys.executable,'-c',code,dataset['file'],json.dumps(case['spec']),str(baseline_dir)],
                    cwd=base,env=environment,capture_output=True,text=True,timeout=300,check=True)
                baseline=json.loads(process.stdout)
                stage=case['name']+':upgraded'
                actual=stats.analyze(dataset['file'],case['spec'],str(current_dir))
                stage=case['name']+':compare'
                if baseline.get('status')!='ok' or actual.get('status')!='ok':raise ValueError('A registered real-data analysis was blocked.')
                checks=numerical_checks(actual,baseline)
                report['cases'].append({'name':case['name'],'spec':case['spec'],'status':'passed','numerical_checks':len(checks),
                    'rows':actual.get('rows',[]),'denominators':actual.get('denominators',{}),'warnings':actual.get('warnings',[])})
            report['status']='ok'
    except Exception as error:
        report['error_type']=type(error).__name__
        report['error_stage']=stage
        if isinstance(error,ValueError) and str(error).startswith(('Numerical validation mismatch at ','Categorical validation mismatch at ','Missing validation field at ','Array validation mismatch at ','Boolean validation mismatch at ')):
            report['mismatch']=str(error)[:200]
        report['error']='The controlled validation did not complete. No synthetic or model result was substituted.'
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(str(destination),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as output:json.dump(report,output,indent=2,allow_nan=False)
    print(json.dumps({'status':report['status'],'case_count':len(report['cases']),
        'numerical_checks':sum(case['numerical_checks'] for case in report['cases']),'independent_stata':'pending'}))
    return 0 if report['status']=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
