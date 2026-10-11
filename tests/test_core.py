"""Behavior checks use temporary files/database and a loopback-only test server.

Run from the application directory: python -m unittest discover -s tests -v
No real course files, app data, model, or external network are used.
"""
import contextlib
import hashlib
import http.client
import io
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import bridge
import core


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="study-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "app"
        self.source = self.base / "courses"
        self.root.mkdir()
        self.source.mkdir()
        (self.root / "settings.json").write_text(json.dumps({
            "source_root": str(self.source), "model": "test-model", "port": 18765,
        }), encoding="utf-8")
        self.write("课程/离散数学/集合.md", "集合运算：交集表示同时属于两个集合的元素。")
        self.write("课程/Java/类型.txt", "Java 类型与泛型，封装与继承。")
        self.write("共同.md", "学习入口。先记录当前停点，再安排下一步。")
        # Exercise imported page evidence, including original hash validation.
        original = self.write("课程/离散数学/关系.pdf", b"fixture-original-pdf", binary=True)
        payload = {"documents": [{"id": "doc-1", "path": "课程/离散数学/关系.pdf",
                    "name": "关系课件", "course": "离散数学",
                    "sha256": hashlib.sha256(original.read_bytes()).hexdigest()}],
                   "pages": [{"id": "p-1", "docId": "doc-1", "page": 3,
                              "text": "等价关系满足自反性、对称性和传递性。"}]}
        self.write("工具/课件搜索/generated/data.js",
                   "window.COURSE_INDEX = " + json.dumps(payload, ensure_ascii=False) + ";")
        self.store = core.Store(self.root)
        self.store.index()

    def write(self, name, content, binary=False):
        target = self.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target


class SourceTests(Fixture):
    def test_chinese_search_has_original_source_and_page(self):
        result = self.store.search("等价关系", "离散数学")
        self.assertTrue(result)
        page = next(item for item in result if item["page"] == 3)
        self.assertEqual(page["path"], "课程/离散数学/关系.pdf")
        self.assertIn("自反性", page["excerpt"])
        self.assertEqual(self.store.read_source(page["id"])["content"],
                         "等价关系满足自反性、对称性和传递性。")

    def test_reimport_is_no_change_and_preserves_revision(self):
        revision = self.store.revision()
        report = self.store.index()
        self.assertEqual(report["changed"], 0)
        self.assertEqual(report["missing"], 0)
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.stats()["documents"], 4)

    def test_deleted_source_is_not_searchable_or_readable(self):
        item = self.store.search("交集", "离散数学")[0]
        (self.source / "课程/离散数学/集合.md").unlink()
        report = self.store.index()
        self.assertEqual(report["missing"], 1)
        self.assertEqual(self.store.search("交集", "离散数学"), [])
        with self.assertRaises(ValueError):
            self.store.read_source(item["id"])

    def test_changed_original_invalidates_stale_page_text(self):
        (self.source / "课程/离散数学/关系.pdf").write_bytes(b"changed")
        report = self.store.index()
        self.assertTrue(any("原件已变化" in item["error"] for item in report["errors"]))
        self.assertEqual(self.store.search("等价关系", "离散数学"), [])

    def test_course_search_excludes_other_course(self):
        self.assertEqual(self.store.search("泛型", "离散数学"), [])
        self.assertIn("Java", {x["course"] for x in self.store.search("泛型", "Java")})

    def test_unavailable_source_root_preserves_existing_evidence(self):
        self.store.source = self.base / "missing"
        with self.assertRaises(ValueError):
            self.store.index()
        self.assertTrue(self.store.search("交集", "离散数学"))


class MemoryTests(Fixture):
    def test_connection_context_closes_database_handle(self):
        with self.store.connect() as cx:
            self.assertEqual(cx.execute("SELECT 1").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            cx.execute("SELECT 1")

    def test_idempotency_replay_is_one_write_conflicting_content_rejected(self):
        first = self.store.remember("看完集合这一节", "离散数学", idempotency_key="delivery-1")
        revision = self.store.revision()
        again = self.store.remember("看完集合这一节", "离散数学", idempotency_key="delivery-1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.store.revision(), revision)
        with self.assertRaises(ValueError):
            self.store.remember("已经完成所有作业", "离散数学", idempotency_key="delivery-1")
        self.assertEqual(len(self.store.memories()), 1)

    def test_revision_conflict_preserves_newer_progress(self):
        revision = self.store.revision()
        self.store.remember("新的真实进度", "Java", expected_revision=revision)
        with self.assertRaises(ValueError):
            self.store.remember("过期交接", "Java", expected_revision=revision)
        self.assertEqual([x["content"] for x in self.store.memories("Java")], ["新的真实进度"])

    def test_delete_removes_next_step_from_snapshot_and_handoff(self):
        memory = self.store.remember("做集合第二题", "离散数学", kind="next_step")
        self.assertEqual(self.store.snapshot("离散数学")["next_step"], "做集合第二题")
        self.store.forget(memory["id"])
        snap = self.store.snapshot("离散数学")
        self.assertEqual(snap["next_step"], core.DEFAULT_NEXT)
        self.assertNotIn("做集合第二题", json.dumps(snap, ensure_ascii=False))
        revision = self.store.revision()
        self.store.forget(memory["id"])
        self.assertEqual(self.store.revision(), revision)

    def test_correction_replaces_old_fact_without_losing_audit(self):
        original = self.store.remember("第一章作业已提交", "Java")
        corrected = self.store.remember("更正：只看了第一章，作业尚未提交", "Java",
                                        kind="correction", supersedes=original["id"],
                                        expected_revision=self.store.revision())
        snap = self.store.snapshot("Java")
        self.assertEqual([m["id"] for m in snap["memories"]], [corrected["id"]])
        self.assertNotIn("第一章作业已提交", snap["handoff"])
        with self.store.connect() as cx:
            self.assertEqual(cx.execute("SELECT deleted FROM memories WHERE id=?", (original["id"],)).fetchone()[0], 1)

    def test_memory_and_session_course_isolation(self):
        self.store.remember("离散数学停点", "离散数学")
        self.store.remember("Java停点", "Java")
        self.assertEqual([m["content"] for m in self.store.memories("Java")], ["Java停点"])
        java = self.store.session("Java")
        math = self.store.session("离散数学")
        self.store.message(java, "user", "Java独有问题")
        self.store.message(math, "user", "数学独有问题")
        self.assertEqual(self.store.history(java)[0]["content"], "Java独有问题")
        with self.assertRaises(ValueError):
            self.store.session("离散数学", java)
        self.assertEqual(self.store.session("Java", java), java)

    def test_bridge_commit_read_forget_round_trip(self):
        args = dict(content="尚未掌握关系", course="离散数学", kind="user_reported",
                    evidence="用户明确说明", idempotency_key="bridge-1",
                    expected_revision=self.store.revision())
        saved = bridge.call(self.store, "study_commit", args)
        self.assertEqual(saved["origin"], "codex")
        self.assertEqual(saved["evidence"], args["evidence"])
        self.assertIn(saved["id"], [m["id"] for m in bridge.call(self.store, "study_snapshot", {})["memories"]])
        bridge.call(self.store, "study_forget", {"id": saved["id"]})
        self.assertEqual(bridge.call(self.store, "study_snapshot", {})["memories"], [])

    def test_bridge_mcp_conflict_is_error_without_second_write(self):
        args = dict(content="一次记录", course="Java", kind="user_reported", evidence="用户",
                    idempotency_key="rpc-1", expected_revision=self.store.revision())
        bridge.call(self.store, "study_commit", args)
        args["content"] = "冲突记录"
        request = {"jsonrpc":"2.0", "id":17, "method":"tools/call",
                   "params":{"name":"study_commit", "arguments":args}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            bridge.mcp(self.store)
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], 17)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(len(self.store.memories()), 1)


class ContinuationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.continuations = {
            "Java": ("课程/Java/接续.md", "JAVA_CONTINUE_ANCHOR：只读到泛型定义，先解释一个例子。"),
            "离散数学": ("课程/离散数学/接续.md", "MATH_CONTINUE_ANCHOR：只看过集合定义，先写一个交集。"),
            "CS50P": ("LEARNING_HANDOFF.md", "PYTHON_CONTINUE_ANCHOR：只读过异常处理例题。"),
            "综合": ("学习入口.md", "GENERAL_CONTINUE_ANCHOR：先选择一门课，再做一小步。"),
        }
        for path, content in self.continuations.values():
            self.write(path, content)
        # More keyword-rich noise must not outrank the designated continuation.
        for i in range(5):
            self.write(f"课程/Java/当前接续下一步{i}.md", "接续 当前 停点 下一步 " * 8)
        self.store.index()

    def test_each_course_designated_continuation_is_first_source(self):
        for course, (path, content) in self.continuations.items():
            with self.subTest(course=course):
                snap = self.store.snapshot(course)
                self.assertTrue(snap["sources"])
                self.assertEqual(snap["sources"][0]["path"], path)
                self.assertIn(content.split("：")[0], snap["sources"][0]["excerpt"])
                other_paths = {value[0] for name, value in self.continuations.items()
                               if name not in {course, "综合"}}
                self.assertFalse(other_paths & {s["path"] for s in snap["sources"]})

    def test_continue_only_question_injects_course_handoff_into_model(self):
        tutor = core.Tutor(self.store)
        with patch.object(tutor, "ensure"), patch.object(tutor, "status", return_value={"available":True}), \
             patch.object(tutor, "rpc", return_value={"message":{"content":"先做一个小例子。"}}) as rpc, \
             patch.object(core, "web_search", side_effect=AssertionError("Must not contact network")), \
             patch.object(core, "web_read", side_effect=AssertionError("Must not contact network")):
            for course, (_, content) in self.continuations.items():
                with self.subTest(course=course):
                    tutor.answer("继续", course)
                    payload = rpc.call_args.args[1]
                    model_context = "\n".join(message["content"] for message in payload["messages"])
                    self.assertIn(content, model_context)
                    for other, (_, other_content) in self.continuations.items():
                        if other not in {course, "综合"}:
                            self.assertNotIn(other_content, model_context)
        self.assertEqual(self.store.memories(), [])


class RetrievalAndDeltaTests(Fixture):
    def test_single_han_character_matches_inside_chinese_word(self):
        self.write("课程/数据结构/堆栈.md", "堆栈采用后进先出方式，用于保存调用状态。")
        self.store.index()
        hits = self.store.search("栈", "数据结构")
        self.assertTrue(hits)
        self.assertIn("课程/数据结构/堆栈.md", {item["path"] for item in hits})
        self.assertEqual(self.store.search("栈", "Java"), [])

    def test_older_than_80_memories_is_searchable_readable_and_course_scoped(self):
        old = self.store.remember("archivalanchor 用户曾卡在接口方差。", "Java")
        other = self.store.remember("archivalanchor 另一门课的独立记录。", "离散数学")
        for i in range(95):
            self.store.remember(f"较新的短记录 number{i}", "Java")
        snap = self.store.snapshot("Java")
        self.assertEqual(len(snap["memories"]), 80)
        self.assertTrue(snap["memories_truncated"])
        self.assertNotIn(old["id"], {item["id"] for item in snap["memories"]})
        hits = self.store.search("archivalanchor", "Java")
        self.assertIn("memory-" + old["id"], {item["id"] for item in hits})
        self.assertNotIn("memory-" + other["id"], {item["id"] for item in hits})
        source = self.store.read_source("memory-" + old["id"])
        self.assertEqual(source["content"], old["content"])
        self.assertEqual(source["course"], "Java")
        self.store.forget(old["id"])
        self.assertEqual(self.store.search("archivalanchor", "Java"), [])
        with self.assertRaises(ValueError):
            self.store.read_source("memory-" + old["id"])

    def test_exact_80_memories_is_not_marked_truncated(self):
        for i in range(80):
            self.store.remember(f"记忆项目{i}", "Java")
        snap = self.store.snapshot("Java")
        self.assertEqual(len(snap["memories"]), 80)
        self.assertFalse(snap["memories_truncated"])

    def test_incremental_forget_returns_tombstone_after_delete_revision_only(self):
        old = self.store.remember("需要停止使用的记录", "Java")
        other = self.store.remember("另一课程的记录", "离散数学")
        checkpoint = self.store.revision()
        self.store.forget(old["id"])
        deleted_revision = self.store.revision()
        self.store.forget(other["id"])
        delta = self.store.snapshot("Java", since_revision=checkpoint)
        self.assertEqual(set(delta["deleted_ids"]), {old["id"]})
        self.assertEqual(delta["memories"], [])
        self.assertNotIn(old["id"], self.store.snapshot("Java", since_revision=deleted_revision)["deleted_ids"])
        with self.store.connect() as cx:
            tombstone = cx.execute("SELECT revision,deleted FROM memories WHERE id=?", (old["id"],)).fetchone()
        self.assertEqual(tombstone["revision"], deleted_revision)
        self.assertEqual(tombstone["deleted"], 1)

    def test_incremental_correction_can_replay_to_same_state_as_full_snapshot(self):
        first = self.store.remember("旧结论：作业已提交", "Java")
        unchanged = self.store.remember("保留的真实记录", "Java")
        baseline = self.store.snapshot("Java")
        checkpoint = baseline["revision"]
        correction = self.store.remember("更正：作业尚未提交", "Java", kind="correction",
                                         supersedes=first["id"], expected_revision=checkpoint)
        delta = bridge.call(self.store, "study_snapshot", {"course":"Java", "since_revision":checkpoint})
        self.assertEqual(set(delta["deleted_ids"]), {first["id"]})
        self.assertEqual([m["id"] for m in delta["memories"]], [correction["id"]])
        cached = {m["id"]:m for m in baseline["memories"]}
        for deleted in delta["deleted_ids"]:
            cached.pop(deleted, None)
        cached.update({m["id"]:m for m in delta["memories"]})
        self.assertEqual(set(cached), {m["id"] for m in self.store.snapshot("Java")["memories"]})
        self.assertEqual(set(cached), {correction["id"], unchanged["id"]})
        with self.store.connect() as cx:
            tombstone_revision = cx.execute("SELECT revision FROM memories WHERE id=?", (first["id"],)).fetchone()[0]
        self.assertEqual(tombstone_revision, correction["revision"])
        self.assertEqual(self.store.snapshot("Java", since_revision=delta["revision"])["deleted_ids"], [])
        self.assertFalse(any(s["id"] == "memory-" + first["id"] for s in self.store.search("旧结论", "Java")))

    def test_more_than_80_delta_records_can_be_consumed_without_skipping_deletions(self):
        checkpoint = self.store.revision()
        records = [self.store.remember(f"分页增量记录{i}", "Java") for i in range(85)]
        for item in records[:15]:
            self.store.forget(item["id"])
        cached, deleted = {}, set()
        cursor = checkpoint
        first = self.store.snapshot("Java", since_revision=cursor)
        self.assertTrue(first["memories_truncated"])
        for page_number in range(5):
            delta = first if page_number == 0 else self.store.snapshot("Java", since_revision=cursor)
            self.assertLessEqual(len(delta["memories"]) + len(delta["deleted_ids"]), 80)
            for mid in delta["deleted_ids"]:
                cached.pop(mid, None)
                deleted.add(mid)
            cached.update({m["id"]:m for m in delta["memories"]})
            next_cursor = delta["next_revision"]
            self.assertGreater(next_cursor, cursor)
            cursor = next_cursor
            if not delta["memories_truncated"]:
                break
        else:
            self.fail("Incremental cursor did not finish within bounded pages")
        expected = {m["id"] for m in self.store.memories("Java", limit=200)}
        self.assertEqual(set(cached), expected)
        self.assertEqual(deleted, {item["id"] for item in records[:15]})
        self.assertEqual(len(expected), 70)
        self.assertEqual(cursor, self.store.revision())

    def test_delta_page_boundary_keeps_atomic_correction_in_same_page(self):
        original = self.store.remember("需要更正的基线", "Java")
        checkpoint = self.store.revision()
        for i in range(79):
            self.store.remember(f"页边界前记录{i}", "Java")
        replacement = self.store.remember("基线更正后的内容", "Java", kind="correction",
                                          supersedes=original["id"], expected_revision=self.store.revision())
        delta = self.store.snapshot("Java", since_revision=checkpoint)
        self.assertIn(original["id"], delta["deleted_ids"])
        self.assertIn(replacement["id"], {m["id"] for m in delta["memories"]})
        self.assertEqual(len(delta["memories"]) + len(delta["deleted_ids"]), 81)
        self.assertEqual(delta["next_revision"], replacement["revision"])
        self.assertFalse(delta["memories_truncated"])


class Clock(datetime):
    current = datetime(2026, 10, 3, 9, 0, tzinfo=core.TZ)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class ConversationPersistenceTests(Fixture):
    def test_answer_citations_survive_restart_with_original_order(self):
        sid = self.store.session("Java")
        citations = [
            {"id":"file-example", "title":"本地原文", "path":"课程/Java/类型.txt", "page":2},
            {"title":"Python official", "url":"https://docs.python.org/3/tutorial/datastructures.html", "excerpt":"append returns None"},
        ]
        self.store.message(sid, "user", "一个问题")
        self.store.message(sid, "assistant", "本地资料[资料1]，官方资料[资料2]。", citations)
        restarted = core.Store(self.root)
        history = restarted.history(sid)
        self.assertEqual(history[0]["sources"], [])
        self.assertEqual(history[1]["sources"], citations)
        self.assertEqual(history[1]["content"], "本地资料[资料1]，官方资料[资料2]。")

    def test_legacy_messages_schema_migrates_without_losing_history(self):
        legacy_root = self.base / "legacy-app"
        legacy_root.mkdir()
        (legacy_root / "settings.json").write_text((self.root / "settings.json").read_text(encoding="utf-8"), encoding="utf-8")
        data = legacy_root / "data"
        data.mkdir()
        with contextlib.closing(sqlite3.connect(data / "learning.sqlite3")) as cx:
            with cx:
                cx.executescript("""
                    CREATE TABLE sessions(id TEXT PRIMARY KEY,course TEXT,created_at TEXT);
                    CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,created_at TEXT);
                    INSERT INTO sessions VALUES('legacy-session','Java','2026-10-03T09:00:00+08:00');
                    INSERT INTO messages VALUES(1,'legacy-session','user','旧库问题','2026-10-03T09:00:00+08:00');
                    INSERT INTO messages VALUES(2,'legacy-session','assistant','旧库回答','2026-10-03T09:00:01+08:00');
                """)
        migrated = core.Store(legacy_root)
        history = migrated.history("legacy-session")
        self.assertEqual([m["content"] for m in history], ["旧库问题", "旧库回答"])
        self.assertEqual([m["sources"] for m in history], [[], []])
        new_sources = [{"title":"新引用", "url":"https://docs.python.org/3/"}]
        migrated.message("legacy-session", "assistant", "迁移后的回答", new_sources)
        restarted = core.Store(legacy_root)
        self.assertEqual(restarted.history("legacy-session")[-1]["sources"], new_sources)
        with restarted.connect() as cx:
            columns = [row[1] for row in cx.execute("PRAGMA table_info(messages)")]
        self.assertEqual(columns.count("sources"), 1)
        self.assertEqual(restarted.memories(), [])

    def test_conversation_search_and_read_survive_store_restart_with_course_isolation(self):
        java = self.store.session("Java")
        math = self.store.session("离散数学")
        self.store.message(java, "user", "conversationanchor Java课程的一个问题")
        self.store.message(java, "assistant", "conversationanchor 这是助手的参考解释")
        self.store.message(math, "user", "conversationanchor 数学课程的另一个问题")
        restarted = core.Store(self.root)
        self.assertEqual(restarted.session("Java", java), java)
        hits = restarted.search("conversationanchor", "Java", limit=30)
        conversation = [item for item in hits if item["id"].startswith("message-")]
        self.assertEqual(len(conversation), 2)
        self.assertEqual({item["course"] for item in conversation}, {"Java"})
        self.assertEqual({item["role"] for item in conversation}, {"user", "assistant"})
        for item in conversation:
            original = restarted.read_source(item["id"])
            self.assertEqual(original["course"], "Java")
            self.assertEqual(original["role"], item["role"])
            self.assertEqual(original["created_at"], item["created_at"])
            self.assertEqual(original["content"], item["excerpt"])
            if original["role"] == "assistant":
                self.assertIn("未独立核验", original["title"])
        math_hits = restarted.search("conversationanchor", "离散数学", limit=30)
        self.assertEqual([item["course"] for item in math_hits], ["离散数学"])
        self.assertEqual(restarted.memories(), [])

    def test_recent_conversation_preserves_roles_without_implying_learning_progress(self):
        sid = self.store.session("Java")
        revision = self.store.revision()
        for i in range(3):
            self.store.message(sid, "user", f"user-turn-{i} " + "问" * 750)
            self.store.message(sid, "assistant", f"assistant-turn-{i} " + "答" * 750)
        other = self.store.session("离散数学")
        self.store.message(other, "assistant", "另一课程的最新回答，不能混入Java")
        snap = self.store.snapshot("Java")
        recent = snap["recent_conversation"]
        self.assertEqual(len(recent), 4)
        self.assertEqual([item["role"] for item in recent], ["user", "assistant", "user", "assistant"])
        self.assertTrue(recent[0]["content"].startswith("user-turn-1"))
        self.assertTrue(recent[-1]["content"].startswith("assistant-turn-2"))
        self.assertTrue(all(len(item["content"]) <= 700 for item in recent))
        self.assertEqual(snap["memories"], [])
        self.assertEqual(snap["memory_count"], 0)
        self.assertEqual(snap["next_step"], core.DEFAULT_NEXT)
        self.assertEqual(snap["revision"], revision)
        self.assertNotIn("另一课程", json.dumps(recent, ensure_ascii=False))


class TutorTests(Fixture):
    def test_web_evidence_beyond_navigation_enters_context_and_preserves_citation_numbers(self):
        tutor = core.Tutor(self.store)
        network = [
            {"title":"Python list methods", "url":"https://docs.python.org/3/tutorial/datastructures.html",
             "excerpt":"navigation " * 140 + "PYTHON_APPEND_EVIDENCE: list.append modifies the list in place and returns None.", "links":[]},
            {"title":"Second reference", "url":"https://docs.python.org/3/library/stdtypes.html",
             "excerpt":"SECOND_NETWORK_EVIDENCE: mutable collection methods return None.", "links":[]},
        ]
        def model_reply(path, payload=None, timeout=5):
            context = payload["messages"][0]["content"]
            numbers = [re.search(r"\[资料(\d+)\] " + re.escape(source["title"]), context).group(1) for source in network]
            citations = [{'source_id':'web-'+hashlib.sha256(s['url'].encode()).hexdigest()[:18],
                          'quote':s['excerpt'].split(': ')[-1]} for s in network]
            return {"message":{"content":json.dumps({'supported':True,'explanation':'append 返回 None。',
                'next_step':'请试着解释原地修改。','citations':citations},ensure_ascii=False)}}

        with patch.object(tutor, "ensure"), patch.object(tutor, "status", return_value={"available":True}), \
             patch.object(core, "web_search", return_value=network), \
             patch.object(core, "web_read", side_effect=AssertionError("No real network allowed")), \
             patch.object(tutor, "rpc", side_effect=model_reply) as rpc:
            result = tutor.answer("append返回什么？", "Java", web=True)
        system = rpc.call_args.args[1]["messages"][0]["content"]
        self.assertIn("PYTHON_APPEND_EVIDENCE", system)
        self.assertIn("SECOND_NETWORK_EVIDENCE", system)
        for url in [item["url"] for item in network]:
            numbered = next((i, source) for i, source in enumerate(result["sources"], 1) if source.get("url") == url)
            self.assertIn(f"[资料{numbered[0]}] {numbered[1]['title']}", system)
            self.assertIn(f"[资料{numbered[0]}]", result["answer"])
        persisted = core.Store(self.root).history(result["session_id"])
        self.assertEqual(persisted[-1]["sources"], result["sources"])
        self.assertEqual(persisted[-1]["content"], result["answer"])
        self.assertEqual([source.get("url") for source in persisted[-1]["sources"]],
                         [source.get("url") for source in result["sources"]])
        self.assertEqual(self.store.memories(), [])

    def test_answer_does_not_write_progress_or_leak_other_course_history(self):
        tutor = core.Tutor(self.store)
        payloads = []

        def rpc(path, payload=None, timeout=5):
            self.assertEqual(path, "/api/chat")
            payloads.append(payload)
            return {"message":{"content":"测试解释，不能算作掌握。"}}

        revision = self.store.revision()
        with patch.object(tutor, "ensure"), patch.object(tutor, "status", return_value={"available":True}), \
             patch.object(tutor, "rpc", side_effect=rpc), \
             patch.object(core, "web_search", side_effect=AssertionError("Must not contact network")), \
             patch.object(core, "web_read", side_effect=AssertionError("Must not contact network")):
            java = tutor.answer("Java独有问题：泛型怎么用？", "Java")
            math = tutor.answer("交集是什么意思？", "离散数学")
            self.assertNotIn("Java独有问题", json.dumps(payloads[-1], ensure_ascii=False))
            self.assertTrue(math["sources"])
            with self.assertRaises(ValueError):
                tutor.answer("继续", "离散数学", java["session_id"])
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.memories(), [])
        self.assertEqual([x["role"] for x in self.store.history(java["session_id"])], ["user","assistant"])
        self.assertEqual(self.store.history(math["session_id"])[0]["content"], "交集是什么意思？")

    def test_model_failure_does_not_save_incomplete_dialog_or_progress(self):
        tutor = core.Tutor(self.store)
        sid = self.store.session("Java")
        with patch.object(tutor, "ensure"), patch.object(tutor, "status", return_value={"available":True}), \
             patch.object(tutor, "rpc", side_effect=ValueError("测试模型失败")):
            with self.assertRaises(ValueError):
                tutor.answer("泛型是什么？", "Java", sid)
        self.assertEqual(self.store.history(sid), [])
        self.assertEqual(self.store.memories(), [])
        self.assertFalse(tutor.lock.locked())


class ReminderTests(Fixture):
    def setUp(self):
        super().setUp()
        Clock.current = datetime(2026, 10, 3, 9, 0, tzinfo=core.TZ)
        self.mock_clock = patch.object(core, "datetime", Clock)
        self.mock_clock.start()
        self.addCleanup(self.mock_clock.stop)

    def test_daily_shown_is_not_shown_again_but_next_day_is(self):
        self.assertTrue(self.store.reminder()["should_show"])
        self.assertFalse(self.store.reminder("shown")["should_show"])
        self.assertFalse(self.store.reminder()["should_show"])
        Clock.current += timedelta(days=1)
        self.assertTrue(self.store.reminder()["should_show"])

    def test_snooze_is_quiet_for_15_minutes_then_due(self):
        result = self.store.reminder("snooze")
        self.assertFalse(result["should_show"])
        self.assertEqual(datetime.fromisoformat(result["snoozed_until"]), Clock.current + timedelta(minutes=15))
        Clock.current += timedelta(minutes=14, seconds=59)
        self.assertFalse(self.store.reminder()["should_show"])
        Clock.current += timedelta(seconds=1)
        self.assertTrue(self.store.reminder()["should_show"])

    def test_skip_and_start_suppress_same_day_reminder(self):
        self.assertFalse(self.store.reminder("skip")["should_show"])
        Clock.current += timedelta(hours=10)
        self.assertFalse(self.store.reminder()["should_show"])
        Clock.current += timedelta(days=1)
        self.assertTrue(self.store.reminder()["should_show"])
        self.assertFalse(self.store.reminder("start")["should_show"])


class OfflineTutor:
    def __init__(self, store):
        self.process = None
        self.model = "test-offline"

    def status(self):
        return {"name": self.model, "available": False, "status": "测试离线"}

    def answer(self, *args, **kwargs):
        raise ValueError("测试模型未就绪")


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "web").mkdir()
        (self.root / "web/index.html").write_text("<!doctype html><title>测试</title>", encoding="utf-8")
        self.secret = self.base / "outside-secret.txt"
        self.secret.write_text("OUTSIDE_SECRET_NEVER_EXPOSE", encoding="utf-8")
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.origin = "http://127.0.0.1:" + str(self.port)
        self.tutor_patch = patch.object(app, "Tutor", OfflineTutor)
        self.tutor_patch.start()
        self.addCleanup(self.tutor_patch.stop)
        self.server_error = []

        def run():
            try:
                app.serve(self.root, self.port)
            except Exception as error:
                self.server_error.append(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(.02)
        else:
            self.fail("Temporary HTTP server failed to start: " + str(self.server_error))
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        merged = dict(headers or {})
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=body, headers=merged)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            conn.close()

    def write_headers(self):
        return {"Cookie": self.cookie, "Origin": self.origin, "X-Study-Request": "1"}

    def stop_server(self):
        if self.thread.is_alive() and hasattr(self, "cookie"):
            self.request("POST", "/api/shutdown", {}, self.write_headers())
            self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), "Temporary server must stop before fixture removal")
        self.assertEqual(self.server_error, [])

    def test_api_needs_authentication_and_cookie_is_httponly(self):
        self.assertEqual(self.request("GET", "/api/memory")[0], 401)
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": self.cookie})[0], 200)

    def test_study_http_offline_roundtrip_and_idempotent_confirmation(self):
        self.assertEqual(self.request('GET', '/api/study/plan')[0], 401)
        payload = {'course':'Java','minutes':5,'easy':False,'idempotency_key':'http-start'}
        self.assertEqual(self.request('POST', '/api/study/start', payload)[0], 403)
        status, _, raw = self.request('POST', '/api/study/start', payload, self.write_headers())
        self.assertEqual(status, 200)
        run = json.loads(raw)['run']
        self.assertEqual(self.store.memories(), [])
        status, _, raw = self.request('POST', '/api/study/check', {'id':run['id']}, self.write_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['run']['quiz']['mode'], 'reflection')
        status, _, raw = self.request('POST', '/api/study/answer',
            {'id':run['id'],'answers':[{'id':'q1','answer':'  原答\n保留空格  '}],'idempotency_key':'http-answer'}, self.write_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['run']['feedback']['status'], 'unavailable')
        self.assertEqual(self.store.memories(), [])
        for _ in range(2):
            status, _, raw = self.request('POST', '/api/study/finish', {'id':run['id'],'stuck_point':'还不清楚泛型'}, self.write_headers())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['run']['status'], 'completed')
        self.assertEqual(len(self.store.memories()), 1)
        self.assertIn('  原答\n保留空格  ', self.store.memories()[0]['content'])
        status, _, raw = self.request('GET', '/api/study/plan?course=Java&minutes=30', headers={'Cookie':self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['plan']['next_step'], '还不清楚泛型')

    def test_stop_without_quiz_http_roundtrip(self):
        _, _, raw = self.request('POST', '/api/study/start',
            {'course':'Java','minutes':5,'idempotency_key':'http-stop-start'}, self.write_headers())
        run = json.loads(raw)['run']
        body = {'id':run['id'],'skip_quiz':True,'stop_note':'停在泛型的第一个例子'}
        for _ in range(2):
            status, _, raw = self.request('POST', '/api/study/finish', body, self.write_headers())
            self.assertEqual(status, 200)
            finished = json.loads(raw)['run']
            self.assertIsNone(finished['answers'])
            self.assertEqual(finished['status'], 'completed')
        self.assertEqual(len(self.store.memories()), 1)
        _, _, raw = self.request('GET', '/api/study/plan?course=Java', headers={'Cookie':self.cookie})
        self.assertEqual(json.loads(raw)['plan']['next_step'], body['stop_note'])

    def test_study_chat_rejects_mismatched_course_and_session(self):
        status, _, raw = self.request('POST', '/api/study/start',
            {'course':'Java','minutes':30,'idempotency_key':'course-start'}, self.write_headers())
        self.assertEqual(status, 200)
        run = json.loads(raw)['run']
        for course, session in [('离散数学',run['chat_session_id']),('Java','different-session')]:
            status, _, raw = self.request('POST', '/api/chat',
                {'message':'继续','course':course,'session_id':session,'study_run_id':run['id']}, self.write_headers())
            self.assertEqual(status, 400)
            self.assertIn('不匹配', json.loads(raw)['error'])
        self.assertEqual(self.store.history(run['chat_session_id']), [])

    def test_wrong_host_is_rejected_even_with_cookie(self):
        for method, path, data in [("GET", "/api/state", None), ("POST", "/api/memory", {"content":"拒绝", "course":"Java"})]:
            headers = self.write_headers()
            headers["Host"] = "attacker.invalid:" + str(self.port)
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, data, headers)[0], 403)

    def test_cross_site_missing_origin_or_marker_cannot_write(self):
        for override in [{"Origin":"https://attacker.invalid"}, {"Origin":None},
                         {"X-Study-Request":None}, {"Sec-Fetch-Site":"cross-site"},
                         {"Cookie":None}]:
            headers = self.write_headers()
            for name, value in override.items():
                if value is None:
                    headers.pop(name, None)
                else:
                    headers[name] = value
            self.assertEqual(self.request("POST", "/api/memory", {"content":"不应保存", "course":"Java"}, headers)[0], 403)
        self.assertEqual(self.store.memories(), [])

    def test_valid_cookie_origin_marker_write_and_read(self):
        status, _, raw = self.request("POST", "/api/memory", {
            "content":"HTTP真实记录", "course":"Java", "kind":"user_reported", "idempotency_key":"http-1",
        }, self.write_headers())
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["content"], "HTTP真实记录")
        status, _, raw = self.request("GET", "/api/memory?course=Java", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["items"][0]["content"], "HTTP真实记录")

    def test_empty_frontend_course_normalizes_to_general(self):
        status, _, raw = self.request("POST", "/api/memory", {
            "content":"全部课程下记录", "course":"", "kind":"user_reported",
        }, self.write_headers())
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["course"], "综合")

    def test_path_cannot_read_arbitrary_file(self):
        paths = ["/../outside-secret.txt", "/%2e%2e/outside-secret.txt",
                 "/api/source?id=../../outside-secret.txt", "/api/source?path=" + str(self.secret),
                 "/api/preview?id=../../outside-secret.txt"]
        for path in paths:
            status, _, raw = self.request("GET", path, headers={"Cookie":self.cookie})
            self.assertIn(status, (400, 404), (path, status, raw))
            self.assertNotIn("OUTSIDE_SECRET_NEVER_EXPOSE", raw)

    def test_unavailable_model_returns_error_without_progress_or_history(self):
        status, _, raw = self.request("POST", "/api/chat", {"message":"测试问题", "course":"Java"}, self.write_headers())
        self.assertEqual(status, 400)
        self.assertIn("未就绪", json.loads(raw)["error"])
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.stats()["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
