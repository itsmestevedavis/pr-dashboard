"""app/jobs/job.py — Job class, the _jobs registry, and get_or_create_job."""

import queue
import threading
import time

_jobs = {}  # (repo, number, kind) -> Job
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, repo, number, kind):
        self.repo = repo
        self.number = number
        self.kind = kind  # review | re_review | merge | address | fix_pipeline | rebase
        self.status = "running"  # running | done | failed | stopped
        self.result = None
        self.log = []
        self.subscribers = []
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.log_path = None
        self.proc = None
        self._stop_requested = False

    def stop(self):
        with self.lock:
            self._stop_requested = True
            proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def append(self, text):
        line = {"ts": time.time(), "type": "line", "text": text}
        with self.lock:
            self.log.append(line)
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            for line in self.log:
                q.put_nowait(line)
            self.subscribers.append(q)
            if self.status != "running":
                q.put_nowait({"ts": time.time(), "type": "done",
                              "status": self.status, "result": self.result})
        return q

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def finish(self, status, result):
        with self.lock:
            self.status = status
            self.result = result
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait({"ts": time.time(), "type": "done",
                              "status": status, "result": result})
            except queue.Full:
                pass


def get_or_create_job(repo, number, kind):
    key = (repo, number, kind)
    with _jobs_lock:
        existing = _jobs.get(key)
        if existing and existing.status == "running":
            return existing, False
        new_job = Job(repo, number, kind)
        _jobs[key] = new_job
    return new_job, True
