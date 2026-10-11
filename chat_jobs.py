"""Durable request identities and atomic publication of checked answers."""
import hashlib
import json
import re
import threading
from datetime import datetime

from core import compact, now
from generation import Cancelled, RequestControl


class ChatJobs:
    def __init__(self, store, tutor):
        self.store, self.tutor = store, tutor
        self.lock = threading.RLock()
        self.controls = {}
        with store.connect() as cx:
            cx.execute('''CREATE TABLE IF NOT EXISTS chat_jobs(
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, result TEXT, error TEXT)''')
            cx.execute("UPDATE chat_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('running','cancelling')",
                       ('本地服务已重启，这次提问未完成。原问题保留，可以重新发送。', now()))

    def get(self, job_id):
        with self.store.connect() as cx:
            row = cx.execute('SELECT * FROM chat_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise LookupError('没有找到这次提问。它可能尚未送达，请重新检查或用原请求重试。')
        row = dict(row)
        payload = json.loads(row['payload'])
        end = now() if row['status'] in {'running', 'cancelling'} else row['updated_at']
        elapsed = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(row['created_at'])).total_seconds()))
        return {'id': row['id'], 'status': row['status'], 'stage': row['stage'],
                'elapsed_seconds': elapsed, 'question': payload['message'], 'course': payload['course'],
                'session_id': payload.get('session_id'),
                'result': json.loads(row['result']) if row['result'] else None, 'error': row['error']}

    def start(self, job_id, payload):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', job_id):
            raise ValueError('提问标识无效，请重新发送。')
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        with self.lock:
            with self.store.connect() as cx:
                cx.execute('BEGIN IMMEDIATE')
                row = cx.execute('SELECT fingerprint FROM chat_jobs WHERE id=?', (job_id,)).fetchone()
                if row:
                    if row['fingerprint'] != fingerprint:
                        raise ValueError('同一次提问标识对应了不同内容，请重新发送。')
                else:
                    if cx.execute("SELECT 1 FROM chat_jobs WHERE status IN ('running','cancelling') LIMIT 1").fetchone():
                        raise ValueError('上一题仍在处理，请完成或取消后再发送。')
                    stamp = now()
                    cx.execute('INSERT INTO chat_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                               (job_id, fingerprint, encoded, 'running', 'retrieving', stamp, stamp, None, None))
            if not row:
                control = RequestControl(lambda stage: self._stage(job_id, stage))
                self.controls[job_id] = control
                threading.Thread(target=self._run, args=(job_id, payload, control), daemon=True).start()
        return self.get(job_id)

    def _stage(self, job_id, stage):
        with self.store.connect() as cx:
            cx.execute("UPDATE chat_jobs SET stage=? WHERE id=? AND status='running'", (stage, job_id))

    def cancel(self, job_id):
        with self.lock:
            control = self.controls.get(job_id)
            if control:
                with control.guard:
                    with self.store.connect() as cx:
                        row = cx.execute('SELECT status FROM chat_jobs WHERE id=?', (job_id,)).fetchone()
                        if row and row['status'] in {'running', 'cancelling'}:
                            cx.execute("UPDATE chat_jobs SET status='cancelling',updated_at=? WHERE id=?", (now(), job_id))
                            control.cancel()
        return self.get(job_id)

    def _run(self, job_id, payload, control):
        try:
            result = self.tutor.answer(**payload, control=control, persist=False)
            with control.guard:
                control.check()
                # Completion and cancellation compete at this lock. The answer pair
                # and terminal state share one transaction, including crash recovery.
                with self.store.connect() as cx:
                    cx.execute('BEGIN IMMEDIATE')
                    if cx.execute('SELECT status FROM chat_jobs WHERE id=?', (job_id,)).fetchone()[0] != 'running':
                        raise Cancelled()
                    stamp = now()
                    cx.executemany('INSERT INTO messages(session_id,role,content,created_at,sources) VALUES(?,?,?,?,?)', [
                        (result['session_id'], 'user', payload['message'], stamp, '[]'),
                        (result['session_id'], 'assistant', result['answer'], stamp, compact(result['sources']))])
                    cx.execute("UPDATE chat_jobs SET status='completed',stage='verifying',updated_at=?,result=? WHERE id=?",
                               (stamp, compact(result), job_id))
        except Exception as exc:
            with control.guard:
                cancelled = control.event.is_set() or isinstance(exc, Cancelled)
                error = None if cancelled else (str(exc)[:300] if isinstance(exc, (ValueError, OSError)) else '处理失败，原问题已保留，请重试。')
                with self.store.connect() as cx:
                    cx.execute('UPDATE chat_jobs SET status=?,updated_at=?,error=? WHERE id=?',
                               ('cancelled' if cancelled else 'failed', now(), error, job_id))
        finally:
            with self.lock:
                self.controls.pop(job_id, None)

    def shutdown(self):
        with self.lock:
            for control in self.controls.values():
                control.cancel()
