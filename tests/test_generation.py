"""Exercise actual loopback streaming and cancellation without model weights."""
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from test_core import Fixture
from core import Tutor
from generation import Cancelled, RequestControl


class GenerationTests(Fixture):
    def fake_model(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(cleanup)
        tutor = Tutor(self.store)
        tutor.base = 'http://127.0.0.1:' + str(server.server_port)
        return tutor

    def test_cancel_closes_inflight_socket_and_stops_reader(self):
        accepted, disconnected = threading.Event(), threading.Event()
        class WaitingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.end_headers()
                accepted.set()
                self.connection.settimeout(3)
                try:
                    if self.connection.recv(1) == b'': disconnected.set()
                except OSError:
                    pass
        tutor = self.fake_model(WaitingHandler)
        control, errors = RequestControl(), []
        def generate():
            try: tutor.stream_chat({'model':'test-model'}, control)
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=generate)
        worker.start()
        self.assertTrue(accepted.wait(2))
        control.cancel()
        worker.join(2)
        self.assertFalse(worker.is_alive(), 'Cancellation must wake the blocked model response read')
        self.assertTrue(disconnected.wait(2), 'The model connection must actually close')
        self.assertIsInstance(errors[0], Cancelled)

    def test_stream_requires_done_and_rejects_truncation(self):
        rows = [{'message': {'content':'{"supported":false}'}, 'done':True}]
        class StreamingHandler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.end_headers()
                for row in rows:
                    self.wfile.write((json.dumps(row)+'\n').encode())
                self.wfile.flush()
        tutor = self.fake_model(StreamingHandler)
        result = tutor.stream_chat({}, RequestControl())
        self.assertEqual(result['message']['content'], '{"supported":false}')
        rows[0]['done'] = False
        with self.assertRaisesRegex(ValueError, '中断'):
            tutor.stream_chat({}, RequestControl())
        rows[0].update(done=True, done_reason='length')
        with self.assertRaisesRegex(ValueError, '长度'):
            tutor.stream_chat({}, RequestControl())

    def test_no_sources_skips_model_and_has_no_progress(self):
        tutor = Tutor(self.store)
        with patch.object(tutor, 'ensure') as ensure, patch.object(tutor, 'stream_chat') as stream, \
             patch.object(self.store, 'search', return_value=[]), \
             patch.object(self.store, 'snapshot', return_value={'sources':[], 'handoff':''}):
            result = tutor.answer('这是一个完全未接入的课程问题', '英语六级', control=RequestControl(), persist=False)
        ensure.assert_not_called()
        stream.assert_not_called()
        self.assertEqual(result['grounding']['status'], 'insufficient')
        self.assertEqual(self.store.history(result['session_id']), [])
        self.assertEqual(self.store.memories(), [])

    def test_smaller_prompt_still_checks_citations_before_publication(self):
        tutor = Tutor(self.store)
        sid = self.store.session('Java')
        for i in range(8):
            self.store.message(sid, 'user' if i % 2 == 0 else 'assistant', '历史内容' * 800)
        stages = []
        def reply(payload, control):
            control.stage('generating')
            self.assertLessEqual(len(payload['messages']), 4)
            self.assertLessEqual(payload['options']['num_predict'], 700)
            self.assertLess(len(payload['messages'][0]['content']), 5000)
            return {'message':{'content':json.dumps({'supported':True,'explanation':'伪造内容不得显示',
                    'next_step':'试试看','citations':[{'source_id':'made-up','quote':'伪造'}]})}}
        with patch.object(tutor,'ensure'), patch.object(tutor,'status',return_value={'available':True}), \
             patch.object(tutor,'stream_chat',side_effect=reply):
            result = tutor.answer('泛型是什么？','Java',sid,control=RequestControl(stages.append),persist=False)
        self.assertEqual(stages, ['retrieving','loading','generating','verifying'])
        self.assertEqual(result['grounding']['status'],'invalid')
        self.assertNotIn('伪造内容',result['answer'])
        self.assertEqual(len(self.store.history(sid)),8)


if __name__ == '__main__': unittest.main()
