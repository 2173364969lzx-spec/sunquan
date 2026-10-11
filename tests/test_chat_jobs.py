"""Deterministic async publication and HTTP checks; no Ollama or real records."""
import hashlib
import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_core
import app
from chat_jobs import ChatJobs
from core import compact, now


class ControlledTutor:
    process = None
    model = 'controlled-test-model'

    def __init__(self, store):
        self.store = store
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def answer(self, **payload):
        self.calls.append(payload)
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError('Test must release the fake model')
        # Intentionally ignore cancellation to simulate a late transport result.
        session = self.store.session(payload['course'], payload.get('session_id'))
        return {'session_id': session, 'answer': '合成测试回答', 'sources': []}


class ObservedJobs(ChatJobs):
    def __init__(self, *args):
        self.finished = threading.Event()
        super().__init__(*args)

    def _run(self, *args):
        try:
            super()._run(*args)
        finally:
            self.finished.set()


class ChatJobTests(test_core.Fixture):
    def setUp(self):
        super().setUp()
        self.tutor = ControlledTutor(self.store)
        self.jobs = ObservedJobs(self.store, self.tutor)
        self.payload = dict(message='合成测试问题', course='Java', session_id=None, web=False, url='')
        self.initial_revision = self.store.revision()
        self.addCleanup(self.clean_worker)

    def clean_worker(self):
        self.tutor.release.set()
        if self.tutor.calls:
            self.assertTrue(self.jobs.finished.wait(5), 'Worker must stop before temporary data removal')

    def begin(self, key='request_0001'):
        result = self.jobs.start(key, self.payload)
        self.assertTrue(self.tutor.entered.wait(5))
        return result

    def complete(self):
        self.tutor.release.set()
        self.assertTrue(self.jobs.finished.wait(5))

    def assert_no_memories(self):
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.revision(), self.initial_revision)

    def message_count(self):
        with self.store.connect() as cx:
            return cx.execute('SELECT COUNT(*) FROM messages').fetchone()[0]

    def test_duplicate_start_calls_model_and_publishes_message_pair_once(self):
        self.begin()
        for _ in range(3):
            self.assertEqual(self.jobs.start('request_0001', dict(self.payload))['status'], 'running')
        self.complete()
        result = self.jobs.get('request_0001')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.tutor.calls), 1)
        self.assertFalse(self.tutor.calls[0]['persist'])
        history = self.store.history(result['result']['session_id'])
        self.assertEqual([(m['role'], m['content']) for m in history],
                         [('user', self.payload['message']), ('assistant', '合成测试回答')])
        self.assertEqual(self.jobs.start('request_0001', self.payload), result)
        self.assertEqual(self.message_count(), 2)
        self.assert_no_memories()

    def test_same_id_different_payload_is_rejected_before_model_call(self):
        self.begin()
        for change in ({'message':'不同问题'}, {'course':'综合'}, {'web':True}, {'session_id':'different-session'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '不同内容'):
                self.jobs.start('request_0001', {**self.payload, **change})
        self.assertEqual(len(self.tutor.calls), 1)
        self.jobs.cancel('request_0001')
        self.complete()
        self.assertEqual(self.message_count(), 0)
        self.assert_no_memories()

    def test_cancelled_late_result_never_publishes_messages_or_memories(self):
        self.begin()
        self.assertEqual(self.jobs.cancel('request_0001')['status'], 'cancelling')
        self.complete()
        final = self.jobs.get('request_0001')
        self.assertEqual(final['status'], 'cancelled')
        self.assertIsNone(final['result'])
        self.assertEqual(self.jobs.cancel('request_0001')['status'], 'cancelled')
        self.assertEqual(self.message_count(), 0)
        self.assert_no_memories()

    def test_completion_holds_publication_guard_before_cancel_and_stays_completed(self):
        publishing, release_publish, cancel_started = (threading.Event() for _ in range(3))
        original_connect = self.store.connect

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def executemany(self, sql, values):
                if sql.startswith('INSERT INTO messages'):
                    publishing.set()
                    if not release_publish.wait(5):
                        raise AssertionError('Publication gate not released')
                return self.connection.executemany(sql, values)

        @contextmanager
        def gated_connect():
            with original_connect() as cx:
                yield ConnectionProxy(cx)

        cancelled = []
        def cancel():
            cancel_started.set()
            cancelled.append(self.jobs.cancel('request_0001'))

        with patch.object(self.store, 'connect', gated_connect):
            self.begin()
            self.tutor.release.set()
            self.assertTrue(publishing.wait(5))
            contender = threading.Thread(target=cancel)
            contender.start()
            self.assertTrue(cancel_started.wait(5))
            release_publish.set()
            self.assertTrue(self.jobs.finished.wait(5))
            contender.join(5)
            self.assertFalse(contender.is_alive())
        self.assertEqual(cancelled[0]['status'], 'completed')
        self.assertEqual(self.jobs.get('request_0001')['status'], 'completed')
        self.assertEqual(self.message_count(), 2)
        self.assert_no_memories()

    def test_restart_interrupts_pending_jobs_and_replay_does_not_regenerate(self):
        encoded = json.dumps(self.payload, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        with self.store.connect() as cx:
            for key, state in (('restart_running', 'running'), ('restart_cancel', 'cancelling')):
                cx.execute('INSERT INTO chat_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                           (key, fingerprint, encoded, state, 'generating', now(), now(), None, None))
        restarted = ChatJobs(self.store, self.tutor)
        for key in ('restart_running', 'restart_cancel'):
            result = restarted.get(key)
            self.assertEqual(result['status'], 'interrupted')
            self.assertEqual(result['question'], self.payload['message'])
            self.assertIsNone(result['result'])
            self.assertIn('重启', result['error'])
            self.assertEqual(restarted.start(key, self.payload)['status'], 'interrupted')
        self.assertEqual(self.tutor.calls, [])
        self.assertEqual(self.message_count(), 0)
        self.assert_no_memories()


class ChatJobHTTPTests(test_core.HTTPTests):
    # Reuse the loopback fixture; inherited cases are disabled below to avoid
    # duplicating the existing suite when this module is discovered.

    def setUp(self):
        def tutor_factory(store):
            self.tutor = ControlledTutor(store)
            return self.tutor

        def jobs_factory(store, tutor):
            self.jobs = ObservedJobs(store, tutor)
            return self.jobs

        with patch.object(test_core, 'OfflineTutor', tutor_factory), patch.object(app, 'ChatJobs', jobs_factory):
            test_core.HTTPTests.setUp(self)
        self.addCleanup(self.clean_worker)
        self.payload = dict(message='HTTP合成测试问题', course='Java', idempotency_key='http_request_0001')
        self.initial_revision = self.store.revision()

    def clean_worker(self):
        self.tutor.release.set()
        if self.tutor.calls:
            self.assertTrue(self.jobs.finished.wait(5))

    def test_start_status_cancel_require_auth_and_unknown_ids_return_404(self):
        read = '/api/chat/job?id=http_request_0001'
        self.assertEqual(self.request('GET', read)[0], 401)
        self.assertEqual(self.request('POST', '/api/chat/start', self.payload)[0], 403)
        self.assertEqual(self.request('POST', '/api/chat/cancel', {'id':'http_request_0001'})[0], 403)
        for override in ({'Origin':None}, {'Origin':'https://other.invalid'},
                         {'X-Study-Request':None}, {'Sec-Fetch-Site':'cross-site'}):
            headers = self.write_headers()
            for name, value in override.items():
                if value is None:
                    headers.pop(name, None)
                else:
                    headers[name] = value
            for path, data in (('/api/chat/start', self.payload), ('/api/chat/cancel', {'id':'http_request_0001'})):
                with self.subTest(override=override, path=path):
                    self.assertEqual(self.request('POST', path, data, headers)[0], 403)
        self.assertEqual(self.tutor.calls, [])
        for method, path, data, headers in (
            ('GET', '/api/chat/job?id=unknown_request', None, {'Cookie':self.cookie}),
            ('POST', '/api/chat/cancel', {'id':'unknown_request'}, self.write_headers())):
            status, _, body = self.request(method, path, data, headers)
            self.assertEqual(status, 404, body)

        status, _, raw = self.request('POST', '/api/chat/start', self.payload, self.write_headers())
        self.assertEqual(status, 200, raw)
        self.assertTrue(self.tutor.entered.wait(5))
        self.assertEqual(json.loads(raw)['status'], 'running')
        status, _, raw = self.request('GET', read, headers={'Cookie':self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['question'], self.payload['message'])
        status, _, raw = self.request('POST', '/api/chat/cancel', {'id':'http_request_0001'}, self.write_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'cancelling')
        self.tutor.release.set()
        self.assertTrue(self.jobs.finished.wait(5))
        status, _, raw = self.request('GET', read, headers={'Cookie':self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'cancelled')
        with self.store.connect() as cx:
            self.assertEqual(cx.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.revision(), self.initial_revision)

    def test_http_duplicate_and_conflict_preserve_single_message_pair(self):
        for _ in range(2):
            status, _, raw = self.request('POST', '/api/chat/start', self.payload, self.write_headers())
            self.assertEqual(status, 200, raw)
        self.assertTrue(self.tutor.entered.wait(5))
        status, _, raw = self.request('POST', '/api/chat/start', {**self.payload, 'message':'不同问题'}, self.write_headers())
        self.assertEqual(status, 400)
        self.assertIn('不同内容', json.loads(raw)['error'])
        self.tutor.release.set()
        self.assertTrue(self.jobs.finished.wait(5))
        self.assertEqual(len(self.tutor.calls), 1)
        with self.store.connect() as cx:
            self.assertEqual(cx.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.revision(), self.initial_revision)


for _name in dir(test_core.HTTPTests):
    if _name.startswith('test_') and _name not in ChatJobHTTPTests.__dict__:
        setattr(ChatJobHTTPTests, _name, None)


if __name__ == '__main__':
    unittest.main()
