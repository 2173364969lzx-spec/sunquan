"""Deterministic citation checks use only fixture sources and mocked local RPC."""
import json
import unittest
from unittest.mock import patch

from test_core import Fixture
import core
import grounding


class GroundingTests(Fixture):
    def setUp(self):
        super().setUp()
        hits = self.store.search("泛型", "Java")
        self.source = next(item for item in hits if item["path"] == "课程/Java/类型.txt")

    def reply(self, **updates):
        return json.dumps({
            "supported": True,
            "explanation": "按这段资料理解泛型。",
            "next_step": "举一个类型例子。",
            "citations": [{"source_id": self.source["id"], "quote": self.source["excerpt"][:24]}],
            **updates,
        }, ensure_ascii=False)

    def test_valid_citation_uses_verified_source_number_and_displays_original_quote(self):
        answer, status = grounding.render_answer(self.reply(), [self.source])
        self.assertEqual(status, {"status": "cited", "evidence_count": 1})
        self.assertIn("[资料1] " + self.source["excerpt"][:24], answer)
        self.assertIn("解释仍需核对，不代表已掌握", answer)

    def test_whitespace_variation_matches_but_displays_exact_source_span(self):
        source = {**self.source, "excerpt": "alpha  beta\ngamma"}
        reformatted = "alpha beta gamma"
        raw = json.dumps({"supported": True, "explanation": "按原文逐字说明。",
                          "next_step": "尝试复述。", "citations": [{
                              "source_id": source["id"], "quote": reformatted,
                          }]}, ensure_ascii=False)
        answer, status = grounding.render_answer(raw, [source])
        self.assertEqual(status["status"], "cited")
        self.assertIn("[资料1] alpha  beta\ngamma", answer)

    def test_unknown_source_fabricated_quote_bad_format_and_overclaim_are_hidden(self):
        cases = [
            self.reply(citations=[{"source_id": "invented-source", "quote": "泛型"}]),
            self.reply(citations=[{"source_id": self.source["id"], "quote": "原文没有这句话"}]),
            "not JSON",
            json.dumps({"supported": True, "explanation": "没有引用", "next_step": "继续"}),
            self.reply(explanation="你已经掌握，且此回答通过核验。"),
            self.reply(next_step="已保存进度。"),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                answer, status = grounding.render_answer(raw, [self.source])
                self.assertEqual(status, {"status": "invalid", "evidence_count": 0})
                self.assertNotIn("按这段资料理解泛型", answer)
                self.assertIn("已隐藏该解释", answer)

    def test_unsupported_claim_returns_fixed_insufficient_answer(self):
        answer, status = grounding.render_answer('{"supported":false}', [self.source])
        self.assertEqual(status, {"status": "insufficient", "evidence_count": 0})
        self.assertEqual(answer, "当前资料不足以核对这个问题。请补充对应的课件段落或原题，或把问题缩小到一个具体概念。")

    def test_successful_quote_does_not_claim_reasoning_was_verified(self):
        answer, status = grounding.render_answer(self.reply(), [self.source])
        self.assertEqual(status["status"], "cited")
        self.assertNotIn("结论正确", answer)
        self.assertNotIn("通过验证", answer)
        self.assertIn("解释仍需核对", answer)

    def test_evidence_source_rejects_changed_or_deleted_original(self):
        source_id = self.source["id"]
        original_file = self.source_root_path()
        original_bytes = original_file.read_bytes()
        try:
            original_file.write_bytes(original_bytes + b" changed")
            with self.assertRaisesRegex(ValueError, "原件已变化"):
                self.store.evidence_source(source_id)
        finally:
            original_file.write_bytes(original_bytes)
        original_file.unlink()
        with self.assertRaisesRegex(ValueError, "原件已移走"):
            self.store.evidence_source(source_id)

    def source_root_path(self):
        return self.source_dir / self.source["path"]

    @property
    def source_dir(self):
        return self.store.source

    def test_evidence_source_rejects_chat_and_memory_as_course_evidence(self):
        memory = self.store.remember("MEMORY_ONLY_SECRET：已经学过泛型", "Java")
        session = self.store.session("Java")
        self.store.message(session, "assistant", "ASSISTANT_ONLY_SECRET：泛型解释")
        with self.store.connect() as cx:
            message_id = cx.execute("SELECT id FROM messages WHERE session_id=?", (session,)).fetchone()[0]
        for key in ["memory-" + memory["id"], "message-" + str(message_id)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.evidence_source(key)

    def test_courseware_original_change_invalidates_its_indexed_page(self):
        page = self.store.evidence_source('page-p-1')
        self.assertEqual(page['page'], 3)
        (self.store.source/page['path']).write_bytes(b'changed-pdf-fixture')
        with self.assertRaises(ValueError):
            self.store.evidence_source('page-p-1')

    def test_original_changed_while_model_runs_is_not_presented_as_current_evidence(self):
        tutor = core.Tutor(self.store)
        def changed(path, payload=None, timeout=5):
            self.source_root_path().write_text('资料已被替换。', encoding='utf-8')
            return {'message':{'content':self.reply()}}
        with patch.object(tutor, 'ensure'), patch.object(tutor, 'status', return_value={'available':True}), \
             patch.object(tutor, 'rpc', side_effect=changed):
            result = tutor.answer('泛型', 'Java')
        self.assertEqual(result['grounding']['status'], 'insufficient')
        self.assertNotIn('按这段资料理解泛型', result['answer'])

    def test_matching_memories_and_chats_do_not_crowd_out_course_materials(self):
        for i in range(8):
            self.store.remember(f'泛型学习停点 {i}', 'Java')
        sid = self.store.session('Java')
        self.store.message(sid, 'user', '泛型问题')
        self.store.message(sid, 'assistant', '泛型旧回答')
        tutor = core.Tutor(self.store)
        # No canonical handoff source: the question must retrieve actual material.
        with patch.object(self.store, 'snapshot', return_value={'sources':[], 'handoff':''}), \
             patch.object(tutor, 'ensure'), patch.object(tutor, 'status', return_value={'available':True}), \
             patch.object(tutor, 'rpc', return_value={'message':{'content':self.reply()}}):
            result = tutor.answer('泛型', 'Java', sid)
        self.assertEqual(result['grounding']['status'], 'cited')
        self.assertIn(self.source['id'], {item['id'] for item in result['sources']})

    def test_tutor_calls_once_persists_checked_answer_and_excludes_other_course_sources(self):
        # The model response cites a real source ID selected for this course.
        tutor = core.Tutor(self.store)
        payloads = []

        def rpc(path, payload=None, timeout=5):
            self.assertEqual(path, "/api/chat")
            payloads.append(payload)
            system = payload["messages"][0]["content"]
            self.assertIn("source_id=" + self.source["id"], system)
            self.assertNotIn("MATH_ONLY_SECRET", system)
            return {"message": {"content": self.reply()}}

        self.store.remember("Java泛型 MEMORY_ONLY_SECRET：不要作为课程原文", "Java")
        self.store.remember("MATH_ONLY_SECRET：离散数学独有内容", "离散数学")
        sid = self.store.session("Java")
        self.store.message(sid, "assistant", "Java泛型 ASSISTANT_ONLY_SECRET：旧回答不是原文")
        revision = self.store.revision()
        with patch.object(tutor, "ensure"), patch.object(tutor, "status", return_value={"available": True}), \
             patch.object(tutor, "rpc", side_effect=rpc), \
             patch.object(core, "web_search", side_effect=AssertionError("No network")), \
             patch.object(core, "web_read", side_effect=AssertionError("No network")):
            result = tutor.answer("Java泛型是什么？", "Java", sid)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(result["grounding"]["status"], "cited")
        self.assertEqual(result["grounding"]["evidence_count"], 1)
        self.assertTrue(all(not item["id"].startswith(("memory-", "message-"))
                            for item in result["sources"]))
        self.assertNotIn("memory-", json.dumps(result["sources"], ensure_ascii=False))
        self.assertNotIn("MEMORY_ONLY_SECRET", json.dumps(result["sources"], ensure_ascii=False))
        self.assertNotIn("ASSISTANT_ONLY_SECRET", json.dumps(result["sources"], ensure_ascii=False))
        self.assertEqual(self.store.revision(), revision)
        history = self.store.history(result["session_id"])
        self.assertEqual(history[-1]["content"], result["answer"])
        self.assertEqual(history[-1]["sources"], result["sources"])


if __name__ == "__main__":
    unittest.main()
