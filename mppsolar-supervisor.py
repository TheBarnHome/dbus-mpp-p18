#!/usr/bin/env python3
"""Keep the MPP Solar manager alive and forward controlled shutdowns."""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


STABLE_RUNTIME = 300
MAX_BACKOFF = 60
STOP_TIMEOUT = 25
DEFAULT_PYTHON = "/usr/bin/python3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def restart_delay(failures: int) -> int:
    return min(MAX_BACKOFF, 2 ** min(max(1, int(failures)), 6))


def python_executable() -> str:
    """Return an absolute interpreter even when rcS starts without PATH."""
    return sys.executable or DEFAULT_PYTHON


class ManagerSupervisor:
    def __init__(self, command):
        self.command = list(command)
        self.stop_event = threading.Event()
        self.child: subprocess.Popen | None = None

    def request_stop(self, signum=None, frame=None):
        if self.stop_event.is_set():
            return
        logging.info("Supervisor received signal %s", signum)
        self.stop_event.set()
        child = self.child
        if child is not None and child.poll() is None:
            child.terminate()

    def _wait_for_child(self):
        while not self.stop_event.is_set():
            try:
                return self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
        return None

    def _stop_child(self):
        child = self.child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            logging.error("Manager did not stop in %s seconds; killing it", STOP_TIMEOUT)
            child.kill()
            child.wait(timeout=5)

    def run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                started_at = time.monotonic()
                try:
                    self.child = subprocess.Popen(self.command)
                except OSError:
                    logging.exception("Unable to start MPP Solar manager")
                    failures += 1
                else:
                    logging.info("Started MPP Solar manager PID %s", self.child.pid)
                    returncode = self._wait_for_child()
                    lived = time.monotonic() - started_at
                    if self.stop_event.is_set():
                        break
                    failures = 1 if lived >= STABLE_RUNTIME else failures + 1
                    logging.error(
                        "MPP Solar manager PID %s exited with status %s after %.1fs",
                        self.child.pid,
                        returncode,
                        lived,
                    )
                delay = restart_delay(failures)
                logging.info("Restarting MPP Solar manager in %s seconds", delay)
                self.stop_event.wait(delay)
        finally:
            self._stop_child()
        logging.info("MPP Solar supervisor stopped")
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manager",
        default=str(Path(__file__).with_name("mppsolar-manager.py")),
    )
    args = parser.parse_args()
    supervisor = ManagerSupervisor([python_executable(), args.manager])
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
