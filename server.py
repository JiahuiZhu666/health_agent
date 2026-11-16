#!/usr/bin/env python3
"""Local or explicitly configured hosted chat. No web packages are required."""
import argparse
import base64
from concurrent.futures import Future
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
import time
import signal
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import webbrowser
from nhis_agent.runtime import BoundedHTTPServer as ThreadingHTTPServer
from nhis_agent.storage_policy import StorageLimit
from nhis_agent.telemetry import log_error
from nhis_agent import data as survey_data
from nhis_agent.execution_context import ExecutionDeadline, ExecutionCancelled

ROOT = Path(__file__).resolve().parent


def browser_payload(value):
    """Keep server connection settings and API identifiers out of browser JSON."""
    private = {'api_key','base_url','provider','model','returned_model','response_id','request_id',
               'usage','input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens',
               'calls','raw_response'}
    if isinstance(value, dict):
        return {key:browser_payload(item) for key,item in value.items() if key not in private}
    if isinstance(value, list):
        return [browser_payload(item) for item in value]
    return value


def load_environment():
    """Read known keys as plain text, never execute a .env as shell code."""
    path = ROOT / '.env'
    if not path.exists():
        return
    allowed = {'OPENAI_API_KEY', 'MODEL_PROVIDER', 'MODEL_BASE_URL', 'MODEL_NAME', 'NHIS_MODEL_PROVIDER', 'NHIS_MODEL_BASE_URL', 'NHIS_MODEL_NAME',
               'WEB_AUTH_SECRET','NHIS_STATE_DIR','NHIS_HOST','NHIS_ALLOWED_HOSTS','NHIS_ALLOWED_ORIGINS','PORT','NHIS_AUTO_DOWNLOAD_DATA',
               'NHIS_PUBLIC_ACCESS','NHIS_PUBLIC_MESSAGES_PER_HOUR','NHIS_PUBLIC_MESSAGES_PER_DAY',
               'NHIS_PUBLIC_CHATS_PER_HOUR','NHIS_PUBLIC_CHATS_PER_DAY'}
    allowed.update({'NHIS_MODEL_KILL_SWITCH','NHIS_BUDGET_INPUT_USD_PER_MILLION','NHIS_BUDGET_OUTPUT_USD_PER_MILLION',
        'NHIS_HTTP_MAX_THREADS','NHIS_HTTP_LISTEN_BACKLOG','NHIS_HTTP_READ_TIMEOUT','NHIS_HTTP_CONNECTION_DEADLINE','NHIS_JOB_WALL_SECONDS',
        'NHIS_R_MAX_WORKERS','NHIS_R_CPU_SECONDS','NHIS_R_WALL_SECONDS','NHIS_R_MEMORY_MB','NHIS_R_OUTPUT_BYTES','NHIS_R_STEPS_PER_TURN',
        'NHIS_STORAGE_CONVERSATION_BYTES','NHIS_STORAGE_WORKSPACE_BYTES','NHIS_STORAGE_RUN_BYTES','NHIS_STORAGE_INDEX_MAX_FILES'})
    allowed.update({'NHIS_STORAGE_BACKUP_BYTES','NHIS_RETENTION_ANONYMOUS_DAYS','NHIS_RETENTION_COMPLETED_JOBS_DAYS',
                    'NHIS_RETENTION_CACHE_DAYS','NHIS_RETENTION_AUDIT_DAYS'})
    allowed.update('NHIS_BUDGET_'+scope+'_'+metric for scope in ('TURN','DAY')
        for metric in ('CALLS','INPUT_TOKENS','OUTPUT_TOKENS','COST_MICROS'))
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def handler_for(agent, *, allowed_hosts=None, allowed_origins=None, auth_secret=None,
                public_access=False, public_limits=None):
    from nhis_agent.conversation import ConversationService
    from nhis_agent.jobs import ChatJobs, ConversationBusy, SubmissionConflict
    # The factory is created once per server; jobs remain discoverable across requests.
    conversations = ConversationService(agent) if agent is not None else None
    chat_jobs = ChatJobs(conversations, state_dir=agent.root) if conversations is not None else None
    if chat_jobs is not None:
        chat_jobs.store.reconcile_all()
    configured_hosts = {str(value).strip().lower() for value in (allowed_hosts or []) if str(value).strip()}
    configured_origins = {str(value).strip().rstrip('/') for value in (allowed_origins or []) if str(value).strip()}
    access = workspaces = None
    status_cache = {}
    status_lock = threading.Lock()
    if public_access:
        from nhis_agent.access import PublicAccess
        from nhis_agent.workspaces import VisitorWorkspaces
        access = PublicAccess(auth_secret, state_dir=agent.root,
                              secure=any(value.startswith('https://') for value in configured_origins),
                              **(public_limits or {}))
        workspaces = VisitorWorkspaces(agent)

    def public_status():
        # A status refresh must not spawn one R probe for every public request.
        with status_lock:
            if time.monotonic() - status_cache.get('at', float('-inf')) >= 30:
                state = agent.status()
                dataset = state.get('data', {})
                statistics = state.get('statistics', {})
                keep = ('ready', 'income_ready', 'year', 'source', 'row_count', 'sha256')
                clean_data = {key:dataset[key] for key in keep if key in dataset}
                clean_data['datasets'] = {name:{key:item[key] for key in keep if key in item}
                                          for name,item in dataset.get('datasets', {}).items() if isinstance(item,dict)}
                clean_data['message'] = ('The official NHIS files are verified.' if dataset.get('ready') else
                                         'The site owner has not yet prepared the official NHIS data.')
                clean_stats = {key:statistics[key] for key in ('ready','r_version','survey_version','jsonlite_version','mitools_version') if key in statistics}
                if not statistics.get('ready'):
                    clean_stats['reason'] = 'The site owner needs to prepare the statistical environment.'
                configured = bool(state.get('dialogue', {}).get('configured'))
                status_cache['value'] = {
                    'data':clean_data, 'statistics':clean_stats,
                    'dialogue':{'configured':configured, 'connection':{'status':'unverified' if configured else 'not_configured'},
                                'message':'The model connection is configured; availability is checked when you send a message.' if configured else
                                          'The site owner has not configured the model connection.'},
                    'capabilities':state.get('capabilities', []), 'registry':state.get('registry', {}),
                    'validation':state.get('validation', {}),
                }
                status_cache['at'] = time.monotonic()
            return dict(status_cache['value'])

    class Handler(BaseHTTPRequestHandler):
        server_version = 'NHISLocal/0.1'

        def setup(self):
            self._trace_id = uuid.uuid4().hex
            self._visitor_id = None
            self._set_cookie = None
            self._lease = None
            super().setup()
            if hasattr(self.connection,'settimeout'):
                self.connection.settimeout(getattr(self.server,'read_timeout',10))

        def finish(self):
            try:
                super().finish()
            finally:
                if self._lease is not None:
                    workspaces.release(self._visitor_id)

        def _workspace(self, *, create=False, connection=None):
            if not public_access:
                return SimpleNamespace(agent=agent, conversations=conversations, jobs=chat_jobs)
            if self._lease is None:
                self._lease = workspaces.acquire(self._visitor_id, create=create, connection=connection)
            return self._lease

        def log_message(self, fmt, *args):
            # BaseHTTPRequestHandler normally includes raw URLs and query
            # strings, including on malformed requests. Never format them.
            status = str(args[1]) if len(args)>1 and str(args[1]).isdigit() else '-'
            method = self.command if getattr(self,'command','') in ('GET','POST','DELETE','HEAD') else 'OTHER'
            sys.stderr.write('%s trace=%s method=%s status=%s\n' %
                (self.log_date_time_string(),self._trace_id,method,status))

        def _record_error(self,event,error):
            try:log_error(agent.root,event,error,{'trace_id':self._trace_id})
            except Exception:pass

        def _page_parameters(self):
            query=parse_qs(urlsplit(self.path).query,keep_blank_values=True,max_num_fields=8)
            if any(len(values)!=1 for values in query.values()):
                raise ValueError('Pagination parameters must occur once.')
            raw=query.get('limit',['30'])[0]
            if not raw.isascii() or not raw.isdigit() or not 1<=int(raw)<=100:
                raise ValueError('The page limit must be an integer from 1 to 100.')
            return query,int(raw)

        def _same_origin(self):
            port = self.server.server_port
            allowed = {'127.0.0.1:' + str(port), 'localhost:' + str(port)} | configured_hosts
            if (self.headers.get('Host') or '').strip().lower() not in allowed:
                self._json({'error': 'This service hostname is not allowed.'}, 403)
                return False
            origin = self.headers.get('Origin')
            origins = {'http://127.0.0.1:' + str(port), 'http://localhost:' + str(port)} | configured_origins
            if origin and origin.rstrip('/') not in origins:
                self._json({'error': 'Cross-site requests are not allowed.'}, 403)
                return False
            return True

        def _authenticated(self):
            if public_access:
                self._visitor_id, self._set_cookie = access.visitor(self.headers.get('Cookie', ''))
                if self._set_cookie and self.command in ('POST', 'DELETE'):
                    self._json({'error':'Enable cookies and refresh the page before continuing.',
                                'code':'cookies_required'}, 401)
                    return False
                return True
            if not auth_secret:
                return True
            supplied = self.headers.get('Authorization') or ''
            valid = False
            if supplied.startswith('Basic ') and len(supplied) <= 2048:
                try:
                    decoded = base64.b64decode(supplied[6:], validate=True)
                    username, separator, password = decoded.partition(b':')
                    valid = bool(separator) and username == b'owner' and hmac.compare_digest(password, auth_secret.encode())
                except (ValueError, UnicodeError):
                    pass
            if valid:
                return True
            body = b'{"error":"Sign in to access this private research workspace."}'
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="NHIS Lab", charset="UTF-8"')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(body)
            return False

        def _send(self, body, content_type, status=200, filename=None, headers=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Trace-ID',self._trace_id)
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'")
            if self._set_cookie:
                self.send_header('Set-Cookie', self._set_cookie)
            for key,value in (headers or {}).items():
                self.send_header(key, str(value))
            if filename:
                self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value, status=200, *, headers=None):
            self._send(json.dumps(browser_payload(value), ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8', status, headers=headers)

        def _admit(self, kind, action, *, lookup=None):
            if not public_access:
                return action(None,None)
            # Paid work cannot begin until the durable quota transaction commits.
            admission = Future() if kind == 'message' else None
            try:
                reservation = access.reserve(self._visitor_id, kind=kind, lookup=lookup,
                    accept_transaction=lambda connection:action(admission,connection))
            except BaseException:
                if admission is not None:
                    admission.set_result(False)
                raise
            if admission is not None:
                admission.set_result(reservation['allowed'])
            if not reservation['allowed']:
                retry = reservation['retry_after']
                self._json({'error':'The public usage limit has been reached. Please try again later.',
                            'code':'rate_limited', 'retry_after':retry}, 429, headers={'Retry-After':retry})
                return None
            return reservation['value']

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                self._json({'status':'ok'})
                return
            if not self._same_origin() or not self._authenticated():
                return
            if path == '/readyz':
                configured = bool(agent.dialogue_status().get('configured'))
                runtime=agent._statistics_health.snapshot()
                connection=agent.dialogue_status().get('connection',{})
                self._json({'status':'configured' if configured else 'not_configured',
                    'chat':{'configured':configured,'last_observed_status':connection.get('status','unverified')},
                    'statistics':{key:runtime.get(key) for key in ('ready','status','observed_at','stale','refreshing')},
                    'research':{'ready':bool(runtime.get('ready') and survey_data.status(agent.data_root).get('ready'))},
                    'probe':'Local dependency observations only; this endpoint sends no model request.'},200 if configured else 503)
                return
            if path == '/api/conversations':
                try:
                    query,limit=self._page_parameters()
                    self._json(self._workspace().conversations.page(limit,query.get('cursor',[None])[0]))
                except FileNotFoundError:
                    self._json({'conversations':[],'next_cursor':None})
                except ValueError as error:
                    self._json({'error':str(error)},400)
                except Exception as error:
                    self._record_error('http.conversations_failed',error)
                    self._json({'error':'Conversation records could not be loaded. Try again later.'}, 500)
                return
            chat_match = re.fullmatch(r'/api/conversations/([a-f0-9]{32})', path)
            job_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', path)
            if chat_match or job_match:
                try:
                    if chat_match:
                        workspace = self._workspace()
                        query,limit=self._page_parameters()
                        if 'limit' not in query:limit=50
                        conversation = workspace.conversations.get(chat_match[1],limit=limit,before=query.get('before',[None])[0])
                        conversation['active_job_id'] = workspace.jobs.active_job(chat_match[1])
                        latest = workspace.jobs.latest_job(chat_match[1])
                        if latest:
                            conversation['latest_job']={key:value for key,value in latest.items()
                                if key not in ('response','events','external_attempts')}
                        self._json(conversation)
                    else:
                        query,_=self._page_parameters()
                        event_limit=query.get('event_limit',['200'])[0]
                        before=query.get('before_event',[None])[0]
                        if not event_limit.isascii() or not event_limit.isdigit() or (before is not None and (not before.isascii() or not before.isdigit())):
                            raise ValueError('Event page parameters must be positive integers.')
                        self._json(self._workspace().jobs.get(job_match[1],event_limit=int(event_limit),before_event=int(before) if before is not None else None))
                except FileNotFoundError as error:
                    self._json({'error':str(error)}, 404)
                except ValueError as error:
                    self._json({'error':str(error)}, 400)
                except Exception as error:
                    self._record_error('http.record_failed',error)
                    self._json({'error':'This conversation or job could not be loaded. Try again later.'}, 500)
                return
            if path == '/api/status':
                try:
                    state = public_status() if public_access else agent.status()
                    state['access'] = {'public':public_access, 'scope':('public-' + hashlib.sha256(self._visitor_id.encode()).hexdigest()[:16]) if public_access else 'private'}
                    self._json(state)
                except Exception as error:
                    self._record_error('http.status_failed',error)
                    self._json({'error': 'The status check failed. See the local service log.'}, 500)
                return
            if path == '/api/runs':
                try:
                    query,limit=self._page_parameters()
                    self._json(self._workspace().agent.history_page(limit,query.get('before',[None])[0]))
                except FileNotFoundError:
                    self._json({'runs':[],'next_cursor':None})
                except ValueError as error:
                    self._json({'error':str(error)},400)
                except Exception as error:
                    self._record_error('http.history_failed',error)
                    self._json({'error': 'History could not be loaded. Try again later.'}, 500)
                return
            match = re.fullmatch(r'/api/runs/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{10})(?:/(report|csv))?', path)
            if match:
                name = {'report': 'report.md', 'csv': 'estimates.csv', None: 'result.json'}[match[2]]
                try:
                    root = Path(self._workspace().agent.root)
                except FileNotFoundError:
                    self._json({'error':'This analysis does not exist.'}, 404)
                    return
                target = root / 'runs' / match[1] / name
                if target.is_symlink() or target.parent.is_symlink() or not target.is_file():
                    self._json({'error': 'The record does not exist or did not pass release checks.'}, 404)
                    return
                if target.stat().st_size>16*1024*1024:
                    self._json({'error':'The saved artifact exceeds its release limit.'},404)
                    return
                if match[2] is not None:
                    result_path=target.parent/'result.json'
                    try:
                        if result_path.is_symlink() or result_path.stat().st_size>16*1024*1024:
                            raise ValueError('Unreleased result.')
                        if json.loads(result_path.read_text(encoding='utf-8')).get('status')!='ok':
                            raise ValueError('Unreleased result.')
                    except (OSError,ValueError,AttributeError):
                        self._json({'error':'The record did not pass release checks.'},404)
                        return
                ctype = {'report': 'text/markdown; charset=utf-8', 'csv': 'text/csv; charset=utf-8', None: 'application/json; charset=utf-8'}[match[2]]
                body = target.read_bytes()
                if match[2] is None:
                    body = json.dumps(browser_payload(json.loads(body)), ensure_ascii=False, allow_nan=False).encode()
                self._send(body, ctype, filename=match[1] + '-' + name)
                return
            static = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js'}
            if path not in static:
                self._json({'error': 'Not found'}, 404)
                return
            target = ROOT / 'web' / static[path]
            self._send(target.read_bytes(), (mimetypes.guess_type(target.name)[0] or 'text/plain') + '; charset=utf-8')

        def do_POST(self):
            if not self._same_origin() or not self._authenticated():
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16000:
                    self._json({'error': 'The request body must contain 1–16000 bytes.'}, 413)
                    return
                if self.headers.get_content_type() != 'application/json':
                    self._json({'error': 'Use application/json.'}, 415)
                    return
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError('The request must be a JSON object.')
                path = urlsplit(self.path).path
                message_match = re.fullmatch(r'/api/conversations/([a-f0-9]{32})/messages', path)
                control_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/(cancel|resume)',path)
                if public_access and (path in ('/api/ask', '/api/setup-data') or (message_match and value.get('measure_model', False) is not False)):
                    self._json({'error':'This operation is available only in a private workspace.'}, 403)
                    return
                if path == '/api/conversations':
                    result = self._admit('create', lambda admission,connection:self._workspace(create=True,connection=connection).conversations.create())
                    if result is not None:
                        self._json(result, 201)
                elif message_match:
                    parameters=(message_match[1],value.get('message',''),value.get('measure_model',False),value.get('repeats',5),value.get('prompt_versions',3))
                    client_id=value.get('client_message_id')
                    lookup=(lambda connection:self._workspace(connection=connection).jobs.lookup_submission(*parameters,
                        client_message_id=client_id,connection=connection)) if client_id is not None else None
                    result = self._admit('message', lambda admission,connection:self._workspace(connection=connection).jobs.submit(*parameters,
                        admission=admission,client_message_id=client_id,connection=connection),lookup=lookup)
                    if result is not None:
                        self._json(result, 202)
                elif control_match:
                    coordinator=self._workspace().jobs
                    self._json(coordinator.request_cancel(control_match[1]) if control_match[2]=='cancel' else coordinator.resume(control_match[1]),202)
                elif path == '/api/ask':
                    self._json(agent.ask(value.get('question', ''), value.get('measure_model', False),
                                         value.get('repeats', 5), value.get('prompt_versions', 3)))
                elif path in ('/api/config', '/api/config/test'):
                    self._json({'error':'Connection settings are managed by the server environment.'}, 405)
                elif path == '/api/setup-data':
                    self._json(agent.setup_data())
                else:
                    self._json({'error': 'Not found'}, 404)
            except (ConversationBusy,SubmissionConflict) as error:
                self._json({'error':str(error)}, 409)
            except FileNotFoundError as error:
                self._json({'error':str(error)}, 404)
            except (ExecutionDeadline,ExecutionCancelled):
                self._json({'error':'Execution stopped at its configured deadline or cancellation checkpoint. Saved research records are retained.','code':'execution_stopped'},408)
            except StorageLimit as error:
                self._json({'error':str(error),'code':error.code},507)
            except (ValueError, TypeError) as error:
                self._json({'error': str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self._record_error('http.request_failed',error)
                self._json({'error': 'The analysis did not finish and no results were released. See the registration and statistical logs under runs.'}, 500)

        def do_DELETE(self):
            if not self._same_origin() or not self._authenticated():
                return
            path = urlsplit(self.path).path
            match = re.fullmatch(r'/api/conversations/([a-f0-9]{32})', path)
            try:
                if path == '/api/conversations':
                    try:
                        self._json(self._workspace().jobs.clear_conversations())
                    except FileNotFoundError:
                        self._json({'deleted':[], 'deleted_count':0})
                elif match:
                    workspace = self._workspace()
                    if public_access:
                        workspace.conversations.get(match[1])
                    self._json(workspace.jobs.delete_conversation(match[1]))
                else:
                    self._json({'error':'Not found'},404)
            except (ConversationBusy, RuntimeError) as error:
                self._json({'error':str(error)},409)
            except FileNotFoundError as error:
                self._json({'error':str(error)},404)
            except ValueError as error:
                self._json({'error':str(error)},400)
            except Exception as error:
                self._record_error('http.delete_failed',error)
                self._json({'error':'The conversation could not be deleted. Refresh and try again.'},500)
    def shutdown_jobs(timeout=10):
        if chat_jobs is not None:
            chat_jobs.shutdown(timeout=timeout)
        if workspaces is not None:
            workspaces.shutdown(timeout=timeout)
    Handler.shutdown_jobs=staticmethod(shutdown_jobs)
    return Handler


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default=os.environ.get('NHIS_HOST','127.0.0.1'))
    parser.add_argument('--open', action='store_true', help='Open the local browser after startup')
    parser.add_argument('--check', action='store_true', help='Check dependencies and real-data availability')
    parser.add_argument('--test-api', action='store_true', help='Verify the server-side .env connection with one real request')
    args = parser.parse_args()
    from nhis_agent.engine import Agent
    state_root = Path(os.environ.get('NHIS_STATE_DIR') or ROOT).expanduser().resolve()
    agent = Agent(root=state_root)
    if args.test_api:
        report = agent.test_connection()
        destination = state_root / '.cache' / 'dialogue-connection-check.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report['status']=='ok' else 1)
    if args.check:
        print(json.dumps(agent.status(wait_for_statistics=True), ensure_ascii=False, indent=2))
        return
    allowed_hosts = [value.strip() for value in os.environ.get('NHIS_ALLOWED_HOSTS','').split(',') if value.strip()]
    allowed_origins = [value.strip().rstrip('/') for value in os.environ.get('NHIS_ALLOWED_ORIGINS','').split(',') if value.strip()]
    auth_secret = os.environ.get('WEB_AUTH_SECRET','')
    from deploy.entrypoint import public_access as parse_public_access, public_limit, PUBLIC_LIMITS
    try:
        public = parse_public_access(os.environ.get('NHIS_PUBLIC_ACCESS', 'false'))
        limits = {name.removeprefix('NHIS_PUBLIC_').lower():public_limit(name, os.environ.get(name, str(default)))
                  for name,default in PUBLIC_LIMITS.items()} if public else {}
    except ValueError as error:
        parser.exit(1, str(error) + '\n')
    if args.host not in ('127.0.0.1','localhost','::1'):
        if len(auth_secret.strip()) < 32 or not allowed_hosts or not allowed_origins:
            parser.exit(1,'Remote hosting requires WEB_AUTH_SECRET (at least 32 characters), NHIS_ALLOWED_HOSTS, and NHIS_ALLOWED_ORIGINS.\n')
        if public and any(not origin.startswith('https://') for origin in allowed_origins):
            parser.exit(1,'Public hosting requires HTTPS origins.\n')
    if public and len(auth_secret.strip()) < 32:
        parser.exit(1,'Public access requires WEB_AUTH_SECRET (at least 32 characters) to sign visitor sessions.\n')
    public_options = {'public_access':True, 'public_limits':limits} if public else {}
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler_for(agent, allowed_hosts=allowed_hosts,
                    allowed_origins=allowed_origins, auth_secret=auth_secret, **public_options))
    except OSError as error:
        hint = 'This execution environment does not permit listening on a port. Run the startup script in a local terminal.' if error.errno in (1,13) else 'Use --port 8766 to select another port.'
        parser.exit(1, 'Unable to start the local service: {}. {}\n'.format(error, hint))
    print('NHIS research and chat agent: http://127.0.0.1:{}/'.format(server.server_port), flush=True)
    if args.open:
        webbrowser.open('http://127.0.0.1:{}/'.format(server.server_port))
    previous_signals={}
    if threading.current_thread() is threading.main_thread():
        def stop_service(signum,frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGTERM,signal.SIGINT):
            previous_signals[signum]=signal.signal(signum,stop_service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nThe service has stopped.')
    finally:
        server.server_close()
        server.RequestHandlerClass.shutdown_jobs(timeout=10)
        for signum,previous in previous_signals.items():
            signal.signal(signum,previous)


if __name__ == '__main__':
    main()
