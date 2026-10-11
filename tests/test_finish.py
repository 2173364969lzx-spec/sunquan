"""Regression checks for finishing a study run without a quiz answer."""
import unittest

from test_core import Fixture
import study
from test_study import FakeTutor


class FinishTests(Fixture):
    def setUp(self):
        super().setUp()
        self.flow = study.StudyFlow(self.store, FakeTutor())

    def start(self, key="finish-start", course="Java"):
        return self.flow.start(course, 5, False, key)["run"]

    def test_skip_quiz_closes_active_run_without_creating_quiz(self):
        run = self.start()
        finished = self.flow.finish(run["id"], skip_quiz=True)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["finish_mode"], "note")
        self.assertTrue(finished["quiz_skipped"])
        self.assertIsNone(finished["quiz"])
        self.assertIsNone(finished["answers"])

    def test_blank_note_closes_run_without_memory_or_revision_change(self):
        run = self.start()
        revision = self.store.revision()
        finished = self.flow.finish(run["id"], skip_quiz=True, stop_note="  \n ")
        self.assertEqual(finished["stop_note"], "")
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.memories(), [])
        next_run = self.flow.start("Java", 5, False, "next-start")["run"]
        self.assertNotEqual(next_run["id"], run["id"])
        self.assertEqual(next_run["status"], "active")

    def test_nonblank_note_is_trimmed_saved_once_and_replay_is_idempotent(self):
        run = self.start()
        finished = self.flow.finish(run["id"], skip_quiz=True, stop_note="  今天只完成了第一步。  ")
        self.assertEqual(finished["stop_note"], "今天只完成了第一步。")
        revision = self.store.revision()
        replay = self.flow.finish(run["id"], skip_quiz=True, stop_note="今天只完成了第一步。")
        self.assertEqual(replay, finished)
        self.assertEqual(self.store.revision(), revision)
        memories = self.store.memories("Java")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["kind"], "user_reported")
        self.assertEqual(memories[0]["origin"], "study_stop")
        self.assertIn("今天只完成了第一步。", memories[0]["content"])

    def test_completed_finish_rejects_changed_note_mode_or_stuck_point(self):
        run = self.start()
        self.flow.finish(run["id"], skip_quiz=True, stop_note="记录甲")
        for kwargs in [
            {"skip_quiz": True, "stop_note": "记录乙"},
            {"skip_quiz": True, "stop_note": "记录甲", "stuck_point": "新的卡点"},
            {"skip_quiz": False, "stop_note": "记录甲"},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.flow.finish(run["id"], **kwargs)
        self.assertEqual(len(self.store.memories("Java")), 1)

    def test_saved_answers_survive_skip_and_memory_contains_only_factual_answer(self):
        run = self.flow.check(self.start()["id"])
        answers = [{"id": q["id"], "answer": "  本人原答\n保留格式  "}
                   for q in run["quiz"]["questions"]]
        answered = self.flow.answer(run["id"], answers, "saved-answer-key")
        finished = self.flow.finish(run["id"], skip_quiz=True, stop_note="时间到了")
        self.assertEqual(finished["answers"], answered["answers"])
        memory = self.store.memories("Java")[0]
        self.assertEqual(memory["origin"], "study_stop")
        self.assertIn("本人原答\n保留格式", memory["content"])
        self.assertNotIn("AI参考：", memory["content"])

    def test_plan_prefers_explicit_stuck_then_stop_note_and_forget_removes_it(self):
        self.store.remember("既有下一步", "Java", "next_step")
        older = self.start("older")
        older_done = self.flow.finish(older["id"], skip_quiz=True, stop_note="较早的停点")
        newer = self.start("newer")
        newer_done = self.flow.finish(newer["id"], skip_quiz=True, stop_note="最新停点")
        plan = self.flow.plan("Java")["plan"]
        self.assertEqual(plan["next_step"], "最新停点")
        self.assertEqual(plan["context_label"], "你上次停在这里")
        self.store.forget(newer_done["memory_id"])
        self.assertEqual(self.flow.plan("Java")["plan"]["next_step"], "既有下一步")
        explicit = self.start("explicit")
        self.flow.finish(explicit["id"], "本人明确卡在边界", skip_quiz=True, stop_note="另一个说明")
        self.assertEqual(self.flow.plan("Java")["plan"]["next_step"], "本人明确卡在边界")
        self.assertIn(older_done["memory_id"], {m["id"] for m in self.store.memories("Java")})

    def test_stop_notes_are_course_scoped_and_length_is_validated(self):
        java = self.start(course="Java")
        self.flow.finish(java["id"], skip_quiz=True, stop_note="Java停点")
        math = self.start("math-start", "离散数学")
        self.flow.finish(math["id"], skip_quiz=True, stop_note="数学停点")
        self.assertEqual(self.flow.plan("Java")["plan"]["next_step"], "Java停点")
        self.assertEqual(self.flow.plan("离散数学")["plan"]["next_step"], "数学停点")
        bad = self.start("bad-length", "Java")
        with self.assertRaises(ValueError):
            self.flow.finish(bad["id"], skip_quiz=True, stop_note="x" * 1001)
        self.assertEqual(self.flow.get(bad["id"])["status"], "active")

    def test_legacy_finish_still_requires_answers(self):
        run = self.start()
        with self.assertRaises(ValueError):
            self.flow.finish(run["id"])


if __name__ == "__main__":
    unittest.main()
