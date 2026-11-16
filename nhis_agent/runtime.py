"""Bounded stdlib HTTP concurrency and connection lifetime.

The fixed overload response contains no request data. One watchdog closes slow
connections at an absolute deadline; read activity cannot extend that deadline.
"""
from http.server import ThreadingHTTPServer
import os
import socket
import threading
import time


def _integer(name,default,minimum,maximum):
    value=os.environ.get(name,str(default))
    if not isinstance(value,str) or not value.isascii() or not value.isdigit():
        raise ValueError(name+' must be an integer.')
    number=int(value)
    if not minimum<=number<=maximum:
        raise ValueError(name+' is outside its allowed range.')
    return number


def _seconds(name,default,minimum,maximum):
    try:value=float(os.environ.get(name,str(default)))
    except (ValueError,TypeError):raise ValueError(name+' must be a number.') from None
    if not minimum<=value<=maximum:
        raise ValueError(name+' is outside its allowed range.')
    return value


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    block_on_close=False
    allow_reuse_address=True

    def __init__(self,server_address,RequestHandlerClass,bind_and_activate=True,*,
                 max_requests=None,listen_backlog=None,read_timeout=None,
                 connection_deadline=None,clock=None):
        self.max_requests=max_requests if max_requests is not None else _integer('NHIS_HTTP_MAX_THREADS',32,1,256)
        self.request_queue_size=listen_backlog if listen_backlog is not None else _integer('NHIS_HTTP_LISTEN_BACKLOG',64,1,1024)
        self.read_timeout=read_timeout if read_timeout is not None else _seconds('NHIS_HTTP_READ_TIMEOUT',10,.01,120)
        self.connection_deadline=connection_deadline if connection_deadline is not None else _seconds('NHIS_HTTP_CONNECTION_DEADLINE',30,.01,600)
        if (type(self.max_requests) is not int or not 1<=self.max_requests<=256 or
                type(self.request_queue_size) is not int or not 1<=self.request_queue_size<=1024 or
                not .01<=self.read_timeout<=120 or not .01<=self.connection_deadline<=600):
            raise ValueError('Invalid HTTP resource limits.')
        self._slots=threading.BoundedSemaphore(self.max_requests)
        self._connections={}
        self._connections_lock=threading.Lock()
        self._closing=threading.Event()
        self._clock=clock or time.monotonic
        self._watchdog=None
        self._serving=threading.Event()
        self._serving_thread=None
        super().__init__(server_address,RequestHandlerClass,bind_and_activate)
        self._watchdog=threading.Thread(target=self._watch,daemon=True,name='nhis-http-deadlines')
        self._watchdog.start()

    @staticmethod
    def _close_connection(request):
        try:request.shutdown(socket.SHUT_RDWR)
        except (OSError,AttributeError):pass
        try:request.close()
        except (OSError,AttributeError):pass

    def _reject(self,request):
        body=b'{"error":"The service is busy. Try again shortly.","code":"overloaded"}'
        response=(b'HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n'
                  b'Retry-After: 1\r\nConnection: close\r\nCache-Control: no-store\r\n'
                  b'X-Content-Type-Options: nosniff\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        try:
            request.settimeout(.1)
            request.sendall(response)
        except (OSError,AttributeError):pass
        finally:self._close_connection(request)

    def process_request(self,request,client_address):
        if self._closing.is_set() or not self._slots.acquire(blocking=False):
            self._reject(request)
            return
        try:
            request.settimeout(self.read_timeout)
            with self._connections_lock:
                if self._closing.is_set():
                    self._slots.release()
                    self._reject(request)
                    return
                self._connections[id(request)]=(request,self._clock()+self.connection_deadline)
            super().process_request(request,client_address)
        except BaseException:
            with self._connections_lock:self._connections.pop(id(request),None)
            self._slots.release()
            self._close_connection(request)
            raise

    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:
            with self._connections_lock:self._connections.pop(id(request),None)
            self._slots.release()

    def expire_connections(self,now=None):
        now=self._clock() if now is None else now
        with self._connections_lock:
            expired=[request for request,deadline in self._connections.values() if deadline<=now]
        for request in expired:self._close_connection(request)
        return len(expired)

    def _watch(self):
        interval=min(.25,self.connection_deadline/4)
        while not self._closing.wait(interval):self.expire_connections()

    @property
    def active_connections(self):
        with self._connections_lock:return len(self._connections)

    def close_connections(self):
        with self._connections_lock:connections=[request for request,_ in self._connections.values()]
        for request in connections:self._close_connection(request)

    def shutdown(self):
        self._closing.set()
        self.close_connections()
        if not self._serving.is_set():return
        if self._serving_thread is threading.current_thread():
            # Python signal handlers run on the serving thread. Waiting there
            # for BaseServer.shutdown would deadlock its own finally block.
            threading.Thread(target=super().shutdown,daemon=True,name='nhis-http-shutdown').start()
            return
        return super().shutdown()

    def serve_forever(self,poll_interval=.2):
        if self._closing.is_set():return
        self._serving_thread=threading.current_thread()
        self._serving.set()
        try:return super().serve_forever(poll_interval=min(.2,max(.01,poll_interval)))
        finally:
            self._serving.clear()
            self._serving_thread=None

    def server_close(self):
        self._closing.set()
        self.close_connections()
        if self._watchdog is not None and self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=1)
        super().server_close()
