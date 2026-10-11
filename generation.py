"""Cancellation belongs to one request, never to the shared model process."""
import socket
import threading


class Cancelled(Exception):
    pass


class RequestControl:
    def __init__(self, on_stage=None):
        self.guard = threading.RLock()
        self.event = threading.Event()
        self.transport = None
        self.socket = None
        self.on_stage = on_stage or (lambda stage: None)

    def check(self):
        if self.event.is_set():
            raise Cancelled('本次提问已取消。')

    def stage(self, value):
        with self.guard:
            self.check()
            self.on_stage(value)

    def attach(self, connection):
        with self.guard:
            self.check()
            self.transport = connection
            self.socket = connection.sock

    def detach(self):
        with self.guard:
            self.transport = None
            self.socket = None

    def cancel(self):
        with self.guard:
            self.event.set()
            connection = self.transport
            if connection and self.socket:
                # shutdown wakes a read in another thread before close releases it.
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
