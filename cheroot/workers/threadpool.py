"""A thread-based worker pool."""

from __future__ import absolute_import, division, print_function
__metaclass__ = type


import threading
import time
import socket
import select
import functools

from six.moves import queue


__all__ = ('WorkerThread', 'ThreadPool')


class TrueyZero:
    """Object which equals and does math like the integer 0 but evals True."""

    def __add__(self, other):
        return other

    def __radd__(self, other):
        return other


trueyzero = TrueyZero()

_SHUTDOWNREQUEST = None


class WorkerThread(threading.Thread):
    """Thread which continuously polls a Queue for Connection objects.

    Due to the timing issues of polling a Queue, a WorkerThread does not
    check its own 'ready' flag after it has started. To stop the thread,
    it is necessary to stick a _SHUTDOWNREQUEST object onto the Queue
    (one for each running WorkerThread).
    """

    conn = None
    """The current connection pulled off the Queue, or None."""

    server = None
    """The HTTP Server which spawned this thread, and which owns the
    Queue and is placing active connections into it."""

    ready = False
    """A simple flag for the calling server to know when this thread
    has begun polling the Queue."""

    def __init__(self, server):
        """Initialize WorkerThread instance.

        Args:
            server (cheroot.server.HTTPServer): web server object
                receiving this request
        """
        self.ready = False
        self.server = server

        self.requests_seen = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.start_time = None
        self.work_time = 0
        self.stats = {
            'Requests': lambda s: self.requests_seen + (
                self.start_time is None
                and trueyzero
                or self.conn.requests_seen
            ),
            'Bytes Read': lambda s: self.bytes_read + (
                self.start_time is None
                and trueyzero
                or self.conn.rfile.bytes_read
            ),
            'Bytes Written': lambda s: self.bytes_written + (
                self.start_time is None
                and trueyzero
                or self.conn.wfile.bytes_written
            ),
            'Work Time': lambda s: self.work_time + (
                self.start_time is None
                and trueyzero
                or time.time() - self.start_time
            ),
            'Read Throughput': lambda s: s['Bytes Read'](s) / (
                s['Work Time'](s) or 1e-6
            ),
            'Write Throughput': lambda s: s['Bytes Written'](s) / (
                s['Work Time'](s) or 1e-6
            ),
        }
        threading.Thread.__init__(self)

    def if_stats(func):
        """Decorate the function to only invoke if stats are enabled."""
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            return self.server.stats['Enabled'] and func(self, *args, **kwargs)
        return wrapper

    @if_stats
    def log_start_stats(self):
        """Record the start time."""
        self.start_time = time.time()

    @if_stats
    def log_close_stats(self):
        """On close, record the stats."""
        self.requests_seen += self.conn.requests_seen
        self.bytes_read += self.conn.rfile.bytes_read
        self.bytes_written += self.conn.wfile.bytes_written
        self.work_time += time.time() - self.start_time
        self.start_time = None

    def conn_expired(self, last_active, cur_time):
        """Return True if the connection has expired."""
        srv_timeout = self.server.timeout
        return cur_time - last_active > srv_timeout

    def get_expired_conns(self, conn_socks, cur_time):
        """Generate all expired connections."""
        for conn, last_active in tuple(conn_socks.values()):
            if self.conn_expired(last_active, cur_time):
                yield conn

    def close_conns(self, conn_list, conn_socks):
        """Close all connections and associated sockets."""
        for conn in conn_list:
            conn.communicate()  # allow for 408 to be sent
            conn.close()
            conn_socks.pop(conn.socket)

    def process_conns(self, conn_socks):
        """Process connections."""
        rlist = []
        socks = [sck for sck in conn_socks.keys() if sck.fileno() > -1]
        if socks:
            rlist = select.select(socks, [], [], 0)[0]
        for sock in rlist:
            conn, conn_start_time = conn_socks[sock]
            self.conn = conn
            self.log_start_stats()
            try:
                conn.communicate()
            except Exception:
                conn.close()
                conn_socks.pop(conn.socket)
            else:
                conn_socks[conn.socket] = (conn, time.time())
            self.log_close_stats()
            self.conn = None

    def run(self):
        """Process incoming HTTP connections.

        Retrieves incoming connections from thread pool.
        """
        self.server.stats['Worker Threads'][self.getName()] = self.stats
        try:
            self.ready = True
            conn_socks = {}
            while True:
                try:
                    conn = self.server.requests.get(block=True, timeout=0.01)
                except queue.Empty:
                    pass
                else:
                    if conn is _SHUTDOWNREQUEST:
                        return
                    conn_socks[conn.socket] = (conn, time.time())
                self.process_conns(conn_socks)
                expired_conns = self.get_expired_conns(conn_socks, time.time())
                self.close_conns(expired_conns, conn_socks)
        except (KeyboardInterrupt, SystemExit) as ex:
            self.server.interrupt = ex


class ThreadPool:
    """A Request Queue for an HTTPServer which pools threads.

    ThreadPool objects must provide min, get(), put(obj), start()
    and stop(timeout) attributes.
    """

    def __init__(
            self, server, min=10, max=-1, accepted_queue_size=-1,
            accepted_queue_timeout=10,
    ):
        """Initialize HTTP requests queue instance.

        Args:
            server (cheroot.server.HTTPServer): web server object
                receiving this request
            min (int): minimum number of worker threads
            max (int): maximum number of worker threads
            accepted_queue_size (int): maximum number of active
                requests in queue
            accepted_queue_timeout (int): timeout for putting request
                into queue
        """
        self.server = server
        self.min = min
        self.max = max
        self._threads = []
        self._queue = queue.Queue(maxsize=accepted_queue_size)
        self._queue_put_timeout = accepted_queue_timeout
        self.get = self._queue.get

    def start(self):
        """Start the pool of threads."""
        for i in range(self.min):
            self._threads.append(WorkerThread(self.server))
        for worker in self._threads:
            worker.setName('CP Server ' + worker.getName())
            worker.start()
        for worker in self._threads:
            while not worker.ready:
                time.sleep(.1)

    @property
    def idle(self):  # noqa: D401; irrelevant for properties
        """Number of worker threads which are idle. Read-only."""
        return len([t for t in self._threads if t.conn is None])

    def put(self, obj):
        """Put request into queue.

        Args:
            obj (cheroot.server.HTTPConnection): HTTP connection
                waiting to be processed
        """
        self._queue.put(obj, block=True, timeout=self._queue_put_timeout)
        if obj is _SHUTDOWNREQUEST:
            return

    def grow(self, amount):
        """Spawn new worker threads (not above self.max)."""
        if self.max > 0:
            budget = max(self.max - len(self._threads), 0)
        else:
            # self.max <= 0 indicates no maximum
            budget = float('inf')

        n_new = min(amount, budget)

        workers = [self._spawn_worker() for i in range(n_new)]
        while not all(worker.ready for worker in workers):
            time.sleep(.1)
        self._threads.extend(workers)

    def _spawn_worker(self):
        worker = WorkerThread(self.server)
        worker.setName('CP Server ' + worker.getName())
        worker.start()
        return worker

    def shrink(self, amount):
        """Kill off worker threads (not below self.min)."""
        # Grow/shrink the pool if necessary.
        # Remove any dead threads from our list
        for t in self._threads:
            if not t.isAlive():
                self._threads.remove(t)
                amount -= 1

        # calculate the number of threads above the minimum
        n_extra = max(len(self._threads) - self.min, 0)

        # don't remove more than amount
        n_to_remove = min(amount, n_extra)

        # put shutdown requests on the queue equal to the number of threads
        # to remove. As each request is processed by a worker, that worker
        # will terminate and be culled from the list.
        for n in range(n_to_remove):
            self._queue.put(_SHUTDOWNREQUEST)

    def stop(self, timeout=5):
        """Terminate all worker threads.

        Args:
            timeout (int): time to wait for threads to stop gracefully
        """
        # Must shut down threads here so the code that calls
        # this method can know when all threads are stopped.
        for worker in self._threads:
            self._queue.put(_SHUTDOWNREQUEST)

        # Don't join currentThread (when stop is called inside a request).
        current = threading.currentThread()
        if timeout is not None and timeout >= 0:
            endtime = time.time() + timeout
        while self._threads:
            worker = self._threads.pop()
            if worker is not current and worker.isAlive():
                try:
                    if timeout is None or timeout < 0:
                        worker.join()
                    else:
                        remaining_time = endtime - time.time()
                        if remaining_time > 0:
                            worker.join(remaining_time)
                        if worker.isAlive():
                            # We exhausted the timeout.
                            # Forcibly shut down the socket.
                            c = worker.conn
                            if c and not c.rfile.closed:
                                try:
                                    c.socket.shutdown(socket.SHUT_RD)
                                except TypeError:
                                    # pyOpenSSL sockets don't take an arg
                                    c.socket.shutdown()
                            worker.join()
                except (
                    AssertionError,
                    # Ignore repeated Ctrl-C.
                    # See
                    # https://github.com/cherrypy/cherrypy/issues/691.
                    KeyboardInterrupt,
                ):
                    pass

    @property
    def qsize(self):
        """Return the queue size."""
        return self._queue.qsize()
