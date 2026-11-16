#!/usr/bin/env python3
"""Loopback-only web front end. No Python web packages are required."""
import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import webbrowser

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
               'WEB_AUTH_SECRET','NHIS_STATE_DIR','NHIS_HOST','NHIS_ALLOWED_HOSTS','NHIS_ALLOWED_ORIGINS','PORT','NHIS_AUTO_DOWNLOAD_DATA'}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def handler_for(agent, *, allowed_hosts=None, allowed_origins=None, auth_secret=None):
    from nhis_agent.conversation import ConversationService
    from nhis_agent.jobs import ChatJobs, ConversationBusy
    # The factory is created once per server; jobs remain discoverable across requests.
    conversations = ConversationService(agent) if agent is not None else None
    chat_jobs = ChatJobs(conversations) if conversations is not None else None
    artifact_root = Path(agent.root) if agent is not None else ROOT
    configured_hosts = {str(value).strip().lower() for value in (allowed_hosts or []) if str(value).strip()}
    configured_origins = {str(value).strip().rstrip('/') for value in (allowed_origins or []) if str(value).strip()}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'NHISLocal/0.1'

        def log_message(self, fmt, *args):
            # Request bodies and secret config fields never enter HTTP logs.
            sys.stderr.write('%s %s\n' % (self.log_date_time_string(), fmt % args))

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

        def _send(self, body, content_type, status=200, filename=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            if filename:
                self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value, status=200):
            self._send(json.dumps(browser_payload(value), ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8', status)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                self._json({'status':'ok'})
                return
            if not self._same_origin() or not self._authenticated():
                return
            if path == '/readyz':
                configured = bool(agent.dialogue_status().get('configured'))
                self._json({'status':'configured' if configured else 'not_configured'}, 200 if configured else 503)
                return
            if path == '/api/conversations':
                try:
                    self._json({'conversations':conversations.list()})
                except Exception:
                    self._json({'error':'Conversation records could not be loaded. Try again later.'}, 500)
                return
            chat_match = re.fullmatch(r'/api/conversations/([a-f0-9]{32})', path)
            job_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', path)
            if chat_match or job_match:
                try:
                    if chat_match:
                        conversation = conversations.get(chat_match[1])
                        conversation['active_job_id'] = chat_jobs.active_job(chat_match[1])
                        self._json(conversation)
                    else:
                        self._json(chat_jobs.get(job_match[1]))
                except FileNotFoundError as error:
                    self._json({'error':str(error)}, 404)
                except ValueError as error:
                    self._json({'error':str(error)}, 400)
                except Exception:
                    self._json({'error':'This conversation or job could not be loaded. Try again later.'}, 500)
                return
            if path == '/api/status':
                try:
                    self._json(agent.status())
                except Exception:
                    self._json({'error': 'The status check failed. See the local service log.'}, 500)
                return
            if path == '/api/runs':
                try:
                    self._json({'runs': agent.history()})
                except Exception:
                    self._json({'error': 'History could not be loaded. Try again later.'}, 500)
                return
            match = re.fullmatch(r'/api/runs/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{10})(?:/(report|csv))?', path)
            if match:
                name = {'report': 'report.md', 'csv': 'estimates.csv', None: 'result.json'}[match[2]]
                target = artifact_root / 'runs' / match[1] / name
                if not target.is_file():
                    self._json({'error': 'The record does not exist or did not pass release checks.'}, 404)
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
                if path == '/api/conversations':
                    self._json(conversations.create(), 201)
                elif message_match:
                    self._json(chat_jobs.submit(message_match[1], value.get('message', ''),
                        value.get('measure_model', False), value.get('repeats', 5), value.get('prompt_versions', 3)), 202)
                elif path == '/api/ask':
                    self._json(agent.ask(value.get('question', ''), value.get('measure_model', False),
                                         value.get('repeats', 5), value.get('prompt_versions', 3)))
                elif path in ('/api/config', '/api/config/test'):
                    self._json({'error':'Connection settings are managed by the server environment.'}, 405)
                elif path == '/api/setup-data':
                    self._json(agent.setup_data())
                else:
                    self._json({'error': 'Not found'}, 404)
            except ConversationBusy as error:
                self._json({'error':str(error)}, 409)
            except FileNotFoundError as error:
                self._json({'error':str(error)}, 404)
            except (ValueError, TypeError) as error:
                self._json({'error': str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._json({'error': 'The analysis did not finish and no results were released. See the registration and statistical logs under runs.'}, 500)

        def do_DELETE(self):
            if not self._same_origin() or not self._authenticated():
                return
            path = urlsplit(self.path).path
            match = re.fullmatch(r'/api/conversations/([a-f0-9]{32})', path)
            try:
                if path == '/api/conversations':
                    self._json(chat_jobs.clear_conversations())
                elif match:
                    self._json(chat_jobs.delete_conversation(match[1]))
                else:
                    self._json({'error':'Not found'},404)
            except (ConversationBusy, RuntimeError) as error:
                self._json({'error':str(error)},409)
            except FileNotFoundError as error:
                self._json({'error':str(error)},404)
            except ValueError as error:
                self._json({'error':str(error)},400)
            except Exception:
                self._json({'error':'The conversation could not be deleted. Refresh and try again.'},500)
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
        print(json.dumps(agent.status(), ensure_ascii=False, indent=2))
        return
    allowed_hosts = [value.strip() for value in os.environ.get('NHIS_ALLOWED_HOSTS','').split(',') if value.strip()]
    allowed_origins = [value.strip().rstrip('/') for value in os.environ.get('NHIS_ALLOWED_ORIGINS','').split(',') if value.strip()]
    auth_secret = os.environ.get('WEB_AUTH_SECRET','')
    if args.host not in ('127.0.0.1','localhost','::1'):
        if len(auth_secret) < 32 or not allowed_hosts or not allowed_origins:
            parser.exit(1,'Remote hosting requires WEB_AUTH_SECRET (at least 32 characters), NHIS_ALLOWED_HOSTS, and NHIS_ALLOWED_ORIGINS.\n')
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler_for(agent, allowed_hosts=allowed_hosts,
                    allowed_origins=allowed_origins, auth_secret=auth_secret))
    except OSError as error:
        hint = 'This execution environment does not permit listening on a port. Run the startup script in a local terminal.' if error.errno in (1,13) else 'Use --port 8766 to select another port.'
        parser.exit(1, 'Unable to start the local service: {}. {}\n'.format(error, hint))
    print('NHIS research and chat agent: http://127.0.0.1:{}/'.format(server.server_port), flush=True)
    if args.open:
        webbrowser.open('http://127.0.0.1:{}/'.format(server.server_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nThe service has stopped.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
