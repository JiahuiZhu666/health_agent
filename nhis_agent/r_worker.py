"""Resource-limited child execution for the fixed R analysis script.

Linux uses address-space/data limits. macOS deliberately avoids those limits
because its R mappings reserve large virtual ranges; R_MAX_VSIZE instead bounds
the R vector heap, not total RSS. CPU, output and wall limits apply on both.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_BYTES=16*1024*1024
PROBE_EXPRESSION = ('p <- c("survey","jsonlite","mitools"); ok <- vapply(p,requireNamespace,logical(1),quietly=TRUE); '
                    'if(!all(ok)){cat(paste(p[!ok],collapse=","));quit(status=2)}; '
                    'cat(as.character(getRversion()),as.character(packageVersion("survey")),'
                    'as.character(packageVersion("jsonlite")),as.character(packageVersion("mitools")),sep=intToUtf8(10))')
_CAPACITIES={}
_CAPACITY_LOCK=threading.Lock()


class RWorkerBusy(RuntimeError):pass
class RWorkerTimeout(subprocess.TimeoutExpired):pass
class RWorkerOutputLimit(ValueError):pass
class RStepBudgetExceeded(ValueError):pass


def _limit(name,default,minimum,maximum):
    value=os.environ.get(name,str(default))
    if not isinstance(value,str) or not value.isascii() or not value.isdigit():
        raise ValueError(name+' must be an integer.')
    number=int(value)
    if not minimum<=number<=maximum:raise ValueError(name+' is outside its allowed range.')
    return number


def limits():
    return {'max_workers':_limit('NHIS_R_MAX_WORKERS',1,1,4),
            'cpu_seconds':_limit('NHIS_R_CPU_SECONDS',240,1,3600),
            'wall_seconds':_limit('NHIS_R_WALL_SECONDS',240,1,3600),
            'memory_mb':_limit('NHIS_R_MEMORY_MB',1536,32,32768),
            'output_bytes':_limit('NHIS_R_OUTPUT_BYTES',DEFAULT_OUTPUT_BYTES,1024,64*1024*1024),
            'steps_per_turn':_limit('NHIS_R_STEPS_PER_TURN',12,1,100)}


def child_environment(*,memory_mb,temporary_directory):
    allowed=('PATH','LANG','LC_ALL','LC_CTYPE','TZ','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH','SYSTEMROOT')
    value={key:os.environ[key] for key in allowed if key in os.environ}
    value.setdefault('PATH',os.defpath)
    value.update(R_USER=str(temporary_directory),TMPDIR=str(temporary_directory),
                 R_MAX_VSIZE=str(memory_mb)+'M',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',
                 MKL_NUM_THREADS='1',VECLIB_MAXIMUM_THREADS='1')
    return value


def _terminate(process):
    if process.poll() is not None:return
    try:os.killpg(process.pid,signal.SIGTERM)
    except (ProcessLookupError,PermissionError):pass
    try:process.wait(timeout=.3)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid,signal.SIGKILL)
        except (ProcessLookupError,PermissionError):pass
        process.wait(timeout=2)


@contextmanager
def _slot(max_workers):
    with _CAPACITY_LOCK:
        semaphore=_CAPACITIES.setdefault(max_workers,threading.BoundedSemaphore(max_workers))
    if not semaphore.acquire(blocking=False):
        raise RWorkerBusy('The statistical worker is busy. Try this analysis again after it finishes.')
    try:yield
    finally:semaphore.release()


def _reserve_step(context,maximum):
    if context is None or context.store is None:return
    context.check_cancelled()
    with context.store.transaction() as connection:
        context.store.assert_owner(connection,context.workspace_id,context.job_id,context.owner)
        connection.execute('''CREATE TABLE IF NOT EXISTS statistical_attempts (
            attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            started_at REAL NOT NULL)''')
        count=connection.execute('SELECT COUNT(*) FROM statistical_attempts WHERE turn_id=?',(context.turn_id,)).fetchone()[0]
        if count>=maximum:
            raise RStepBudgetExceeded('This turn reached its registered statistical-step limit.')
        connection.execute('INSERT INTO statistical_attempts VALUES (?,?,?,?)',
                           (uuid.uuid4().hex,context.job_id,context.turn_id,time.time()))


def run_limited(command,*,cwd=None,timeout=240,cpu_seconds=240,memory_mb=1536,
                output_bytes=DEFAULT_OUTPUT_BYTES,max_workers=1,context=None,output_path=None,
                step_limit=None):
    """Run trusted local test/analysis commands under a separate limiter child.

    Browser input never selects this command. Production callers use run_r,
    which additionally checks the fixed Rscript invocation shape and paths.
    """
    if not isinstance(command,(list,tuple)) or not command or any(not isinstance(item,str) or '\x00' in item for item in command):
        raise ValueError('A trusted argv sequence is required.')
    if not 0<float(timeout)<=3600 or not 1<=int(cpu_seconds)<=3600 or not 32<=int(memory_mb)<=32768:
        raise ValueError('Invalid statistical process limits.')
    if not 128<=int(output_bytes)<=64*1024*1024 or not 1<=int(max_workers)<=4:
        raise ValueError('Invalid statistical output or worker limit.')
    if context is not None:context.check_cancelled()
    with _slot(int(max_workers)):
        if step_limit is not None:_reserve_step(context,step_limit)
        with tempfile.TemporaryDirectory(prefix='nhis-r-worker-') as temporary, \
             tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
            wrapper=[sys.executable,str(Path(__file__).resolve()),'--child',
                     '--cpu',str(int(cpu_seconds)),'--memory-mb',str(int(memory_mb)),
                     '--output-bytes',str(int(output_bytes)),'--',*command]
            process=subprocess.Popen(wrapper,cwd=cwd,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                env=child_environment(memory_mb=int(memory_mb),temporary_directory=temporary),
                start_new_session=True,close_fds=True)
            deadline=time.monotonic()+float(timeout)
            exceeded=False
            try:
                while True:
                    if context is not None:context.check_cancelled()
                    size=os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size
                    if output_path is not None:
                        try:size+=Path(output_path).stat().st_size
                        except FileNotFoundError:pass
                    if size>output_bytes:
                        exceeded=True
                        raise RWorkerOutputLimit('The statistical engine exceeded its output-size limit.')
                    if process.poll() is not None:break
                    if time.monotonic()>=deadline:
                        raise RWorkerTimeout(command,timeout)
                    time.sleep(.025)
                if process.returncode in (-getattr(signal,'SIGXFSZ',25),):
                    exceeded=True
                stdout.seek(0);stderr.seek(0)
                out=stdout.read(output_bytes+1);err=stderr.read(output_bytes+1)
                if exceeded or len(out)+len(err)>output_bytes:
                    raise RWorkerOutputLimit('The statistical engine exceeded its output-size limit.')
                if context is not None:context.check_cancelled()
                return subprocess.CompletedProcess(list(command),process.returncode,
                    out.decode('utf-8',errors='replace'),err.decode('utf-8',errors='replace'))
            finally:
                _terminate(process)


def run_r(command,*,cwd=None,timeout=None,context=None):
    """Execute only the reviewed analysis entrypoint with fixed interpreter flags."""
    if (not isinstance(command,(list,tuple)) or len(command)!=7 or command[1]!='--vanilla'
            or Path(command[2]).resolve()!=ROOT/'scripts'/'analyze.R'
            or Path(command[6]).resolve()!=ROOT/'config'/'registry.json'
            or not Path(command[0]).name.lower().startswith('rscript')):
        raise ValueError('Only the fixed reviewed R analysis command is accepted.')
    if context is None:
        from .execution_context import current
        context=current()
    config=limits()
    wall=min(float(timeout),config['wall_seconds']) if timeout is not None else config['wall_seconds']
    return run_limited(list(command),cwd=cwd,timeout=wall,cpu_seconds=config['cpu_seconds'],
        memory_mb=config['memory_mb'],output_bytes=config['output_bytes'],max_workers=config['max_workers'],
        context=context,output_path=command[5],step_limit=config['steps_per_turn'])


def run_probe(executable, *, timeout=30, context=None):
    """Run only the literal dependency probe under the shared R resource slot.

    A dependency observation is never a research attempt. It receives the same
    sanitized child environment, CPU/memory/output limits and cancellation fence.
    """
    source=Path(executable).resolve()
    if not source.name.lower().startswith('rscript') or not source.is_file():
        raise ValueError('The runtime probe requires a local Rscript executable.')
    if context is None:
        from .execution_context import current
        context=current()
    config=limits()
    wall=min(float(timeout),30.0,config['wall_seconds'])
    if context is not None:
        context.check_cancelled()
        if getattr(context,'deadline',None) is not None:
            wall=min(wall,context.deadline-time.monotonic())
            if wall<=0:
                from .execution_context import ExecutionDeadline
                raise ExecutionDeadline('The runtime probe did not start before the job deadline.')
    return run_limited([str(source),'--vanilla','-e',PROBE_EXPRESSION],timeout=wall,
        cpu_seconds=min(15,config['cpu_seconds']),memory_mb=config['memory_mb'],
        output_bytes=min(64*1024,config['output_bytes']),max_workers=config['max_workers'],
        context=context,step_limit=None)


def _child_main(argv):
    parser=argparse.ArgumentParser()
    parser.add_argument('--child',action='store_true')
    parser.add_argument('--cpu',type=int,required=True)
    parser.add_argument('--memory-mb',type=int,required=True)
    parser.add_argument('--output-bytes',type=int,required=True)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not args.child or not command:parser.error('A trusted child command is required.')
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_CPU,(args.cpu,args.cpu+1))
    resource.setrlimit(resource.RLIMIT_FSIZE,(args.output_bytes,args.output_bytes))
    if sys.platform.startswith('linux'):
        memory=args.memory_mb*1024*1024
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
        resource.setrlimit(resource.RLIMIT_DATA,(memory,memory))
    os.execvpe(command[0],command,os.environ)


if __name__=='__main__':_child_main(sys.argv[1:])
