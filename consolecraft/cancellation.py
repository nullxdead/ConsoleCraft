"""Cooperative cancellation for terminal downloads and their worker threads."""
from contextlib import contextmanager
import signal
import threading


class DownloadCancelled(KeyboardInterrupt):
    """The user cancelled the active download."""


_active = None


def current_event():
    return _active


def check_cancelled(event=None):
    event = event if event is not None else _active
    if event is not None and event.is_set():
        raise DownloadCancelled()


@contextmanager
def cancellable_download(on_cancel=None):
    global _active
    if _active is not None:
        yield _active
        return
    event = threading.Event()
    _active = event
    main = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGINT) if main else None
    def cancel(signum, frame):
        if not event.is_set():
            event.set()
            if on_cancel:
                try: on_cancel()
                except Exception: pass
            raise DownloadCancelled()
    if main:
        signal.signal(signal.SIGINT, cancel)
    try:
        yield event
        check_cancelled(event)
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
        _active = None
