"""Study-flow checks use only temporary data and deterministic fake tutors."""
import json
import re
import unittest
from unittest.mock import patch

from test_core import Fixture
import bridge
import core
import study


class FakeTutor:
    model = "fake-study-model"

    def __init__(self):
        self.calls = []
        self.quiz_result = None
        self.feedback_result = None
        self.on_feedback = None

    def structured(self, system, prompt):
        data = json.loads(prompt)
        self.calls.append(data)
        if "answers" in data:
            if self.on_feedback:
                self.on_feedback(data)
            if isinstance(self.feedback_result, Exception):
                raise self.feedback_result
            if self.feedback_result is not None:
                return self.feedback_result
            return {"items": [{"id": q["id"], "comment": "AI参考：请再核对依据。",
                              "source_id":data['sources'][0]['id'],"evidence_quote":data['sources'][0]['content'][:30]}
                              for q in data["questions"]], "summary": "AI建议：再举一个例子。"}
        if isinstance(self.quiz_result, Exception):
            raise self.quiz_result
        if self.quiz_result is not None:
            return self.quiz_result
        count = int(re.search(r"生成(\d+)道", system).group(1))
        source_id = data["sources"][0]["id"] if data["sources"] else ""
        return {"questions": [{"question": f"检验{i + 1}：请解释并举例。", "source_id": source_id}
                              for i in range(count)]}


class StudyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.tutor = FakeTutor()
        self.flow = study.StudyFlow(self.store, self.tutor)

    def start(self, minutes=5, key="start", course="Java"):
        return self.flow.start(course, minutes, False, key)["run"]

    def answer(self, run):
        run = self.flow.check(run["id"])
        answers = [{"id": q["id"], "answer": f"  本人原答 {q['id']}\n保留换行  "}
                   for q in run["quiz"]["questions"]]
        return self.flow.answer(run["id"], answers, "answer:" + run["id"])

    def test_plan_duration_easy_mode_and_validation(self):
        for minutes, easy, actual in [(5, False, 5), (30, False, 30), (30, True, 5)]:
            with self.subTest(minutes=minutes, easy=easy):
                plan = self.flow.plan("Java", minutes, easy)["plan"]
                self.assertEqual(plan["minutes"], actual)
                self.assertEqual(len(plan["steps"]), 3)
                self.assertIn(f"{actual}分钟", plan["nudge"])
                if easy:
                    self.assertTrue(plan["title"].startswith("用自己的话说清："))
        for args in [("Java", True, False), ("Java", 10, False), ("Java", 5, "yes"), ("unknown", 5, False)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.flow.plan(*args)
        self.assertEqual(self.store.memories(), [])

    def test_unsupported_feedback_and_mastery_claims_never_become_accepted_feedback(self):
        run = self.start()
        checked = self.flow.check(run['id'])
        source = self.flow._context(run)['sources'][0]
        for invalid in [
            {'items':[{'id':'q1','comment':'已掌握全部内容','source_id':source['id'], 'evidence_quote':source['content'][:20]}], 'summary':'已掌握'},
            {'items':[{'id':'q1','comment':'反馈','source_id':source['id'], 'evidence_quote':'虚构的原文引文'}], 'summary':'参考'},
        ]:
            self.tutor.feedback_result = invalid
            if not self.flow.get(run['id'])['answers']:
                result = self.flow.answer(run['id'], [{'id':'q1','answer':'我的独立回答'}], 'reject-unfounded')
            else:
                result = self.flow.feedback(run['id'])
            self.assertEqual(result['feedback']['status'], 'unavailable')
            self.assertEqual(result['answers'][0]['answer'], '我的独立回答')
            self.assertEqual(self.store.memories(), [])

    def test_feedback_accepts_formatting_spaces_and_preserves_original_source(self):
        run = self.start()
        self.flow.check(run['id'])
        source = self.flow._context(run)['sources'][0]
        self.tutor.feedback_result = {
            'items': [{'id': 'q1', 'comment': '请对照原文继续举例。', 'source_id': source['id'],
                       'evidence_quote': ' '.join(source['content'][:20])}], 'summary': '继续核对一个例子。'}
        result = self.flow.answer(run['id'], [{'id': 'q1', 'answer': '我的回答'}], 'spaces')
        self.assertEqual(result['feedback']['status'], 'ready')
        self.assertEqual(result['feedback']['items'][0]['evidence_quote'], source['content'][:20])
        self.assertEqual(self.store.memories(), [])

    def test_canonical_plan_uses_explicit_next_step_not_background_and_keeps_titles_short(self):
        self.write('课程/Java/接续.md', '# Java接续\n## 从这里开始\n背景内容不应成为学习目标。\n'
                   '## 已知停点\n已讨论学习顺序。\n下一次核对：先确认当前章节，再练习一个方法。')
        self.store.index()
        plan = self.flow.plan('Java')['plan']
        self.assertEqual(plan['next_step'], '先确认当前章节，再练习一个方法。')
        self.assertNotIn('背景内容', plan['title'])
        long_step = '**先核对泛型**。' + '保留后面的详细背景。' * 25
        self.store.remember(long_step, 'Java', 'next_step')
        plan = self.flow.plan('Java')['plan']
        self.assertEqual(plan['context'], long_step)
        self.assertLessEqual(len(plan['title']), 80)
        self.assertLess(len(plan['nudge']), 110)
        self.assertNotIn('**', plan['title'])

    def test_guided_lesson_does_not_recycle_an_old_assistant_answer_as_a_source(self):
        old = self.store.session('Java')
        self.store.message(old, 'assistant', 'int OLD_UNVERIFIED_ASSISTANT_CLAIM')
        run = self.start()
        tutor = core.Tutor(self.store)
        with patch.object(tutor, 'ensure'), patch.object(tutor, 'status', return_value={'available':True}), \
                patch.object(tutor, 'rpc', return_value={'message':{'content':'先尝试解释int。'}}) as rpc:
            result = tutor.answer('int', 'Java', run['chat_session_id'], study_context='本次只学习int。')
        self.assertTrue(all(not s['id'].startswith('message-') for s in result['sources']))
        self.assertNotIn('OLD_UNVERIFIED_ASSISTANT_CLAIM', rpc.call_args.args[1]['messages'][0]['content'])
        self.assertEqual(self.store.memories(), [])

    def test_course_context_and_model_sources_are_isolated(self):
        self.store.remember("Java专属下一步", "Java", "next_step")
        self.store.remember("数学专属下一步", "离散数学", "next_step")
        for course, own, other in [("Java", "Java专属", "数学专属"), ("离散数学", "数学专属", "Java专属")]:
            run = self.start(course=course, key=course)
            self.assertIn(own, run["next_step"])
            self.assertNotIn(other, json.dumps(run, ensure_ascii=False))
            self.flow.check(run["id"])
            context = self.tutor.calls[-1]
            self.assertEqual(context["course"], course)
            self.assertNotIn(other, json.dumps(context, ensure_ascii=False))
            self.assertTrue(context["sources"])
            for source in context["sources"]:
                self.assertIn(self.store.read_source(source["id"])["course"], [course, "综合"])

    def test_previous_question_is_a_question_and_next_step_takes_priority(self):
        sid = self.store.session("Java")
        self.store.message(sid, "user", "泛型为什么不能用int？")
        plan = self.flow.plan("Java")["plan"]
        self.assertEqual(plan["context_label"], "上次的问题")
        self.assertEqual(plan["previous_question"], "泛型为什么不能用int？")
        self.store.remember("继续练习泛型边界", "Java", "next_step")
        plan = self.flow.plan("Java")["plan"]
        self.assertEqual(plan["context_label"], "你留下的下一步")
        self.assertEqual(plan["next_step"], "继续练习泛型边界")
        self.assertNotIn("掌握", plan["nudge"])

    def test_start_replay_refresh_restart_and_conflicting_key(self):
        first = self.start()
        self.assertTrue(self.flow.start("Java", 5, False, "start")["resumed"])
        restarted = study.StudyFlow(core.Store(self.root), self.tutor)
        self.assertEqual(restarted.plan("Java")["run"]["id"], first["id"])
        resumed = restarted.start("Java", 30, False, "refresh")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run"]["id"], first["id"])
        for course, minutes in [("离散数学", 5), ("Java", 30)]:
            with self.subTest(course=course, minutes=minutes), self.assertRaises(ValueError):
                restarted.start(course, minutes, False, "start")
        self.assertEqual(self.store.stats()["sessions"], 1)

    def test_keys_are_required_before_any_run_or_answer_is_saved(self):
        for key in [None, "", " ", "x" * 129, 123]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.flow.start("Java", 5, False, key)
        self.assertEqual(self.store.stats()["sessions"], 0)
        run = self.flow.check(self.start()["id"])
        for key in [None, "", "x" * 129]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.flow.answer(run["id"], [{"id": "q1", "answer": "原答"}], key)
        self.assertIsNone(self.flow.get(run["id"])["answers"])

    def test_manual_question_in_task_session_survives_restart_as_question(self):
        run = self.start()
        self.store.message(run["chat_session_id"], "user", run["prompt"])
        self.assertNotEqual(self.flow.plan("Java")["plan"]["previous_question"], run["prompt"])
        self.store.message(run["chat_session_id"], "user", "手动追问：为什么泛型不能使用int？")
        restarted = study.StudyFlow(core.Store(self.root), self.tutor)
        plan = restarted.plan("Java")["plan"]
        self.assertEqual(plan["previous_question"], "手动追问：为什么泛型不能使用int？")
        self.assertEqual(plan["context_label"], "上次的问题")

    def test_quiz_fallback_for_failure_malformed_and_invented_citation(self):
        for result in [OSError("offline"), [], "not JSON object", {}, {"questions": []},
                       {"questions": [{"question": "题目", "source_id": "invented-id"}]}]:
            with self.subTest(result=result):
                self.tutor.quiz_result = result
                run = self.start()
                checked = self.flow.check(run["id"])
                self.assertEqual(checked["quiz"]["mode"], "reflection")
                self.assertEqual(checked["quiz"]["questions"][0]["source_id"], "")
                self.assertEqual(self.flow.check(run["id"])["quiz"], checked["quiz"])
                self.flow.answer(run["id"], [{"id": "q1", "answer": "我的自述"}], "a:" + run["id"])
                self.flow.finish(run["id"])
        flow = study.StudyFlow(self.store, None)
        run = flow.start("Java", 30, False, "offline-30")["run"]
        self.assertEqual(len(flow.check(run["id"])["quiz"]["questions"]), 2)

    def test_raw_answers_saved_before_feedback_failure_and_retry(self):
        revision = self.store.revision()
        run = self.start(30)
        self.flow.check(run["id"])
        answers = [{"id": "q2", "answer": "  第二题\n原答  "}, {"id": "q1", "answer": " 第一题 "}]
        def verify_saved(data):
            persisted = study.StudyFlow(core.Store(self.root)).get(run["id"])
            self.assertEqual(persisted["answers"], list(reversed(answers)))
            self.assertEqual(persisted["status"], "answered")
        self.tutor.on_feedback = verify_saved
        self.tutor.feedback_result = ValueError("unavailable")
        answered = self.flow.answer(run["id"], answers, "original-answer-key")
        self.assertEqual(answered["feedback"]["status"], "unavailable")
        self.assertEqual(answered["submission_key"], "original-answer-key")
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.revision(), revision)
        self.tutor.feedback_result = None
        retried = self.flow.feedback(run["id"])
        self.assertEqual(retried["feedback"]["status"], "ready")
        self.assertEqual(retried["answers"], list(reversed(answers)))
        calls = len(self.tutor.calls)
        self.assertEqual(self.flow.feedback(run["id"]), retried)
        self.assertEqual(len(self.tutor.calls), calls)
        self.assertEqual(self.store.revision(), revision)

    def test_busy_model_preserves_answers_for_feedback_retry(self):
        run = self.flow.check(self.start()["id"])
        self.flow.generation.acquire()
        try:
            answered = self.flow.answer(run["id"], [{"id": "q1", "answer": "忙时原答"}], "busy-answer")
        finally:
            self.flow.generation.release()
        self.assertEqual(answered["feedback"]["status"], "unavailable")
        self.assertEqual(answered["answers"], [{"id": "q1", "answer": "忙时原答"}])
        self.assertEqual(self.flow.feedback(run["id"])["feedback"]["status"], "ready")
        self.assertEqual(self.store.memories(), [])

    def test_invalid_or_duplicate_answers_cannot_save_or_overwrite(self):
        run = self.flow.check(self.start(30)["id"])
        for answers in [[], [{"id": "q1", "answer": "a"}] * 2,
                        [{"id": "q1", "answer": "a"}, {"id": "bogus", "answer": "b"}],
                        [{"id": "q1", "answer": " "}, {"id": "q2", "answer": "b"}]]:
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                self.flow.answer(run["id"], answers, "invalid")
            self.assertIsNone(self.flow.get(run["id"])["answers"])
        original = [{"id": "q1", "answer": "a"}, {"id": "q2", "answer": "b"}]
        self.flow.answer(run["id"], original, "answer-key")
        calls = len(self.tutor.calls)
        replay = self.flow.answer(run["id"], list(reversed(original)), "retry-key")
        self.assertEqual(replay["submission_key"], "answer-key")
        self.assertEqual(len(self.tutor.calls), calls)
        with self.assertRaises(ValueError):
            self.flow.answer(run["id"], [{"id": "q1", "answer": "changed"}, original[1]], "answer-key")
        self.assertEqual(self.flow.get(run["id"])["answers"], original)

    def test_explicit_finish_writes_one_factual_memory_available_to_bridge(self):
        revision = self.store.revision()
        run = self.answer(self.start())
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.revision(), revision)
        finished = self.flow.finish(run["id"], "本人确认：边界情况还不清楚")
        self.assertEqual(self.flow.finish(run["id"], finished["stuck_point"]), finished)
        with self.assertRaises(ValueError):
            self.flow.finish(run["id"], "不同卡点")
        snapshot = bridge.call(core.Store(self.root), "study_snapshot", {"course": "Java"})
        self.assertEqual(snapshot["memory_count"], 1)
        self.assertEqual(snapshot["revision"], revision + 1)
        memory = snapshot["memories"][0]
        self.assertEqual(memory["kind"], "user_reported")
        self.assertEqual(memory["origin"], "study_check")
        self.assertIn(run["answers"][0]["answer"], memory["content"])
        self.assertIn(finished["stuck_point"], memory["content"])
        self.assertNotIn("AI参考", memory["content"])
        self.assertIn("AI参考", finished["feedback"]["items"][0]["comment"])
        self.assertIn("不代表实际用时", memory["content"])
        hits = bridge.call(self.store, "study_search", {"query": "边界情况", "course": "Java"})["items"]
        self.assertTrue(any(h["id"] == "memory-" + memory["id"] for h in hits))

    def test_finish_requires_original_answers_and_deleted_stuck_is_not_resurrected(self):
        run = self.start()
        with self.assertRaises(ValueError):
            self.flow.finish(run["id"], "卡点")
        self.answer(run)
        finished = self.flow.finish(run["id"], "独特卡点")
        self.store.remember("普通下一步", "Java", "next_step")
        plan = self.flow.plan("Java")["plan"]
        self.assertEqual(plan["context_label"], "你确认的卡点")
        self.assertEqual(plan["next_step"], "独特卡点")
        self.store.forget(finished["memory_id"])
        self.flow.finish(run["id"], "独特卡点")
        self.assertEqual(self.flow.plan("Java")["plan"]["next_step"], "普通下一步")
        self.assertFalse(any(m["id"] == finished["memory_id"] for m in self.store.memories()))

    def test_newest_completed_run_clears_old_stuck_even_with_equal_timestamps(self):
        with patch.object(study, "now", return_value="2026-10-08T10:00:00+08:00"):
            first = self.answer(self.start(key="first"))
            self.flow.finish(first["id"], "旧卡点")
            self.store.remember("真正下一步", "Java", "next_step")
            second = self.answer(self.start(key="second"))
            self.flow.finish(second["id"], "")
        plan = self.flow.plan("Java")["plan"]
        self.assertEqual(plan["context_label"], "你留下的下一步")
        self.assertEqual(plan["next_step"], "真正下一步")


if __name__ == "__main__":
    unittest.main()
