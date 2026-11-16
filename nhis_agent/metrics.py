"""Read-only, bounded operational summaries without conversation text."""
import json
from pathlib import Path
import shutil
import sqlite3
import time


def _percentile(values,fraction):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*fraction))] if values else None


def snapshot(root,*,now=None):
    root=Path(root).resolve();now=time.time() if now is None else now
    result={'window_seconds':86400,'jobs':{},'models':{},'alerts':[],
            'disk':dict(zip(('total_bytes','used_bytes','free_bytes'),shutil.disk_usage(root)))}
    path=root/'.cache/public-access.sqlite3'
    if path.is_file() and not path.is_symlink():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                states=dict(db.execute('SELECT state,count(*) FROM jobs WHERE created_at>? GROUP BY state',(now-86400,)))
                # Quantiles are explicitly from the most recent 1,000 jobs.
                rows=db.execute('SELECT created_at,started_at,completed_at FROM jobs WHERE created_at>? ORDER BY created_at DESC LIMIT 1000',(now-86400,)).fetchall()
                queue=[max(0,row[1]-row[0]) for row in rows if row[1] is not None]
                durations=[max(0,row[2]-row[1]) for row in rows if row[1] is not None and row[2] is not None]
                result['jobs']={'states_24h':states,'active':db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0],
                    'quantile_sample_limit':1000,'queue_seconds_p95':_percentile(queue,.95),
                    'duration_seconds_p50':_percentile(durations,.5),'duration_seconds_p95':_percentile(durations,.95)}
                if states.get('unknown',0):result['alerts'].append('external_outcome_unknown')
    path=root/'.cache/model_telemetry.sqlite3'
    if path.is_file() and not path.is_symlink():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            rows=db.execute("SELECT metadata FROM events WHERE created>? AND event='model.finished' ORDER BY id DESC LIMIT 20000",(now-86400,)).fetchall()
            errors={};latencies=[];usage={'input_tokens':0,'output_tokens':0}
            for row in rows:
                value=json.loads(row[0]);code=value.get('error_code')
                if code:errors[code]=errors.get(code,0)+1
                if isinstance(value.get('elapsed_ms'),(int,float)):latencies.append(value['elapsed_ms'])
                for name in usage:usage[name]+=value.get(name,0)
            result['models']={'retained_attempts_24h':len(rows),'error_counts':errors,
                'latency_ms_p95':_percentile(latencies,.95),'observed_tokens':usage,
                'basis':'Retained metadata only; not a provider bill or a complete historical invoice.'}
            if errors.get('budget_exceeded'):result['alerts'].append('model_budget_exceeded')
            if len(rows)>=10 and sum(errors.values())/len(rows)>.2:result['alerts'].append('model_error_ratio_above_20_percent')
            stages={};stage_errors={};sources={}
            for row in db.execute("SELECT metadata FROM events WHERE created>? AND event='process.stage' ORDER BY id DESC LIMIT 20000",(now-86400,)):
                value=json.loads(row[0]);name=value.get('stage','unknown')+'.'+value.get('status','unknown')
                stages[name]=stages.get(name,0)+1
                code=value.get('error_code')
                if code:stage_errors[code]=stage_errors.get(code,0)+1
                if value.get('stage')=='statistics' and value.get('status')=='complete':
                    source=value.get('purpose','execution');sources[source]=sources.get(source,0)+1
            result['process']={'retained_stage_events_24h':stages,'error_counts':stage_errors,'statistical_completion_sources':sources}
            if stage_errors.get('r_timeout'):result['alerts'].append('r_worker_timeout')
            if stages.get('validation.blocked'):result['alerts'].append('evidence_validation_blocked')
    if result['disk']['free_bytes']<1024**3:result['alerts'].append('host_disk_free_below_1gb')
    result['memory_note']='Host/container memory and RSS alerts require the deployment monitoring agent; no per-process RSS cap is claimed on macOS.'
    return result
