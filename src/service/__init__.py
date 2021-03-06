#!/usr/bin/env python

# Copyright (c) 2014, 2015 Florian Brucker
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import errno
import logging
from logging.handlers import SysLogHandler
import os
import os.path
import signal
import socket
import sys
import threading
import time

from daemon import DaemonContext
import lockfile.pidlockfile
import setproctitle


__version__ = '0.4.1'

__all__ = ['find_syslog', 'Service']


# Custom log level below logging.DEBUG for logging internal debug
# messages.
SERVICE_DEBUG = logging.DEBUG - 1


def _detach_process():
    """
    Detach daemon process.

    Forks the current process into a parent and a detached child. The
    child process resides in its own process group, has no controlling
    terminal attached and is cleaned up by the init process.

    Returns ``True`` for the parent and ``False`` for the child.
    """
    # To detach from our process group we need to call ``setsid``. We
    # can only do that if we aren't a process group leader. Therefore
    # we fork once, which makes sure that the new child process is not
    # a process group leader.
    pid = os.fork()
    if pid > 0:
        # Parent process
        return True
    os.setsid()
    # We now fork a second time and let the second's fork parent exit.
    # This makes the second fork's child process an orphan. Orphans are
    # cleaned up by the init process, so we won't end up with a zombie.
    # In addition, the second fork's child is no longer a session
    # leader and can therefore never acquire a controlling terminal.
    pid = os.fork()
    if pid > 0:
        os._exit(os.EX_OK)
    return False


class _PIDFile(lockfile.pidlockfile.PIDLockFile):
    """
    A locked PID file.

    This is basically ``lockfile.pidlockfile.PIDLockfile``, with the
    small modification that the PID is obtained only when the lock is
    acquired. This makes sure that the PID in the PID file is always
    that of the process that actually acquired the lock (even if the
    instance was created in another process, for example before
    forking).
    """
    def __init__(self, *args, **kwargs):
        lockfile.pidlockfile.PIDLockFile.__init__(self, *args, **kwargs)
        self.pid = None

    def acquire(self, *args, **kwargs):
        self.pid = os.getpid()
        return lockfile.pidlockfile.PIDLockFile.acquire(self, *args, **kwargs)


def find_syslog():
    """
    Find Syslog.

    Returns Syslog's location on the current system in a form that can
    be passed on to :py:class:`logging.handlers.SysLogHandler`::

        handler = SysLogHandler(address=find_syslog(),
                                facility=SysLogHandler.LOG_DAEMON)
    """
    for path in ['/dev/log', '/var/run/syslog']:
        if os.path.exists(path):
            return path
    return ('127.0.0.1', 514)


class Service(object):
    """
    A background service.

    This class provides the basic framework for running and controlling
    a background daemon. This includes methods for starting the daemon
    (including things like proper setup of a detached deamon process),
    checking whether the daemon is running, asking the daemon to
    terminate and for killing the daemon should that become necessary.

    .. py:attribute:: logger

        A :py:class:`logging.Logger` instance.

    .. py:attribute:: files_preserve

        A list of file handles that should be preserved by the daemon
        process. File handles of built-in Python logging handlers
        attached to :py:attr:`logger` are automatically preserved.
    """

    def __init__(self, name, pid_dir='/var/run', **dameon_context_args):
        """
        Constructor.

        ``name`` is a string that identifies the daemon. The name is
        used for the name of the daemon process, the PID file and for
        the messages to syslog.

        ``pid_dir`` is the directory in which the PID file is stored.
        """
        self.name = name
        self.pid_file = _PIDFile(os.path.join(pid_dir, name + '.pid'))
        self._got_sigterm = threading.Event()
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            self.logger.addHandler(logging.NullHandler())
        self.files_preserve = []
        self.daemon_context_args = dameon_context_args

    def _debug(self, msg):
        """
        Log an internal debug message.

        Logs a debug message with the :py:data:SERVICE_DEBUG logging
        level.
        """
        self.logger.log(SERVICE_DEBUG, msg)

    def _get_logger_file_handles(self):
        """
        Find the file handles used by our logger's handlers.
        """
        handles = []
        for handler in self.logger.handlers:
            # The following code works for logging's SysLogHandler,
            # StreamHandler, SocketHandler, and their subclasses.
            for attr in ['sock', 'socket', 'stream']:
                try:
                    handle = getattr(handler, attr)
                    if handle:
                        handles.append(handle)
                    break
                except AttributeError:
                    continue
        return handles

    def is_running(self):
        """
        Check if the daemon is running.
        """
        return self.get_pid() is not None

    def get_pid(self):
        """
        Get PID of daemon process or ``None`` if daemon is not running.
        """
        return self.pid_file.read_pid()

    def got_sigterm(self):
        """
        Check if SIGTERM signal was received.

        Returns ``True`` if the daemon process has received the SIGTERM
        signal (for example because :py:meth:`stop` was called).

        .. note::
            This function always returns ``False`` when it is not called
            from the daemon process.
        """
        return self._got_sigterm.is_set()

    def wait_for_sigterm(self, timeout=None):
        """
        Wait until a SIGTERM signal has been received.

        This function blocks until the daemon process has received the
        SIGTERM signal (for example because :py:meth:`stop` was called).

        If ``timeout`` is given and not ``None`` it specifies a timeout
        for the block.

        The return value is ``True`` if SIGTERM was received and
        ``False`` otherwise (the latter only occurs if a timeout was
        given and the signal was not received).

        .. warning::
            This function blocks indefinitely (or until the given
            timeout) when it is not called from the daemon process.
        """
        return self._got_sigterm.wait(timeout)

    def stop(self, block=False):
        """
        Tell the daemon process to stop.

        Sends the SIGTERM signal to the daemon process, requesting it
        to terminate.

        If ``block`` is true then the call blocks until the daemon
        process has exited. This may take some time since the daemon
        process will complete its on-going backup activities before
        shutting down. ``block`` can either be ``True`` (in which case
        it blocks indefinitely) or a timeout in seconds.

        The return value is ``True`` if the daemon process has been
        stopped and ``False`` otherwise.

        .. versionadded:: 0.3
            The ``block`` parameter
        """
        pid = self.get_pid()
        if not pid:
            raise ValueError('Daemon is not running.')
        os.kill(pid, signal.SIGTERM)
        return self._block(lambda: not self.is_running(), block)

    def kill(self):
        """
        Kill the daemon process.

        Sends the SIGKILL signal to the daemon process, killing it. You
        probably want to try :py:meth:`stop` first.

        After the process is killed its PID file is removed.
        """
        pid = self.get_pid()
        if not pid:
            raise ValueError('Daemon is not running.')
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:
            if e.errno == errno.ESRCH:
                raise ValueError('Daemon is not running.')
        finally:
            self.pid_file.break_lock()

    def _block(self, predicate, timeout):
        """
        Block until a predicate becomes true.

        ``predicate`` is a function taking no arguments. The call to
        ``_block`` blocks until ``predicate`` returns a true value. This
        is done by polling ``predicate``.

        ``timeout`` is either ``True`` (block indefinitely) or a timeout
        in seconds.

        The return value is the value of the predicate after the
        timeout.
        """
        if timeout:
            if timeout is True:
                timeout = float('Inf')
            timeout = time.time() + timeout
            while not predicate() and time.time() < timeout:
                time.sleep(0.1)
        return predicate()

    def start(self, block=False):
        """
        Start the daemon process.

        The daemon process is started in the background and the calling
        process returns.

        Once the daemon process is initialized it calls the
        :py:meth:`run` method.

        If ``block`` is true then the call blocks until the daemon
        process has started. ``block`` can either be ``True`` (in which
        case it blocks indefinitely) or a timeout in seconds.

        The return value is ``True`` if the daemon process has been
        started and ``False`` otherwise.

        .. versionadded:: 0.3
            The ``block`` parameter
        """
        pid = self.get_pid()
        if pid:
            raise ValueError('Daemon is already running at PID %d.' % pid)

        # The default is to place the PID file into ``/var/run``. This
        # requires root privileges. Since not having these is a common
        # problem we check a priori whether we can create the lock file.
        try:
            self.pid_file.acquire(timeout=0)
        finally:
            try:
                self.pid_file.release()
            except lockfile.NotLocked:
                pass

        # Clear previously received SIGTERMs. This must be done before
        # the calling process returns so that the calling process can
        # call ``stop`` directly after ``start`` returns without the
        # signal being lost.
        self._got_sigterm.clear()

        if _detach_process():
            # Calling process returns
            return self._block(lambda: self.is_running(), block)
        # Daemon process continues here
        self._debug('Daemon has detached')

        def on_sigterm(signum, frame):
            self._debug('Received SIGTERM signal')
            self._got_sigterm.set()

        def runner():
            try:
                # We acquire the PID as late as possible, since its
                # existence is used to verify whether the service
                # is running.
                self.pid_file.acquire(timeout=0)
                self._debug('PID file has been acquired')
                self._debug('Calling `run`')
                self.run()
                self._debug('`run` returned without exception')
            except Exception as e:
                self.logger.exception(e)
            except SystemExit:
                self._debug('`run` called `sys.exit`')
            try:
                self.pid_file.release()
                self._debug('PID file has been released')
            except Exception as e:
                self.logger.exception(e)
            os._exit(os.EX_OK)  # FIXME: This seems redundant

        try:
            setproctitle.setproctitle(self.name)
            self._debug('Process title has been set')
            files_preserve = (self.files_preserve +
                              self._get_logger_file_handles())
            self.daemon_context_args['files_preserve'] = files_preserve
            self.daemon_context_args['detach_process'] = False
            self.daemon_context_args['signal_map'] = {
                signal.SIGTTIN: None,
                signal.SIGTTOU: None,
                signal.SIGTSTP: None,
                signal.SIGTERM: on_sigterm,
            }
            with DaemonContext(**self.daemon_context_args):
                self._debug('Daemon context has been established')

                # Python's signal handling mechanism only forwards signals to
                # the main thread and only when that thread is doing something
                # (e.g. not when it's waiting for a lock, etc.). If we use the
                # main thread for the ``run`` method this means that we cannot
                # use the synchronization devices from ``threading`` for
                # communicating the reception of SIGTERM to ``run``. Hence we
                # use  a separate thread for ``run`` and make sure that the
                # main loop receives signals. See
                # https://bugs.python.org/issue1167930
                thread = threading.Thread(target=runner)
                thread.start()
                while thread.is_alive():
                    time.sleep(1)
        except Exception as e:
            self.logger.exception(e)

        # We need to shutdown the daemon process at this point, because
        # otherwise it will continue executing from after the original
        # call to ``start``.
        os._exit(os.EX_OK)

    def run(self):
        """
        Main daemon method.

        This method is called once the daemon is initialized and
        running. Subclasses should override this method and provide the
        implementation of the daemon's functionality. The default
        implementation does nothing and immediately returns.

        Once this method returns the daemon process automatically exits.
        Typical implementations therefore contain some kind of loop.

        The daemon may also be terminated by sending it the SIGTERM
        signal, in which case :py:meth:`run` should terminate after
        performing any necessary clean up routines. You can use
        :py:meth:`got_sigterm` and :py:meth:`wait_for_sigterm` to
        check whether SIGTERM has been received.
        """
        pass

