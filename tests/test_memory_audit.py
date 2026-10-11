"""Memory audit and real continuation checks on isolated synthetic records."""
import unittest

from test_core import Fixture
from study import StudyFlow
from test_study import FakeTutor


class MemoryAuditTests(Fixture):
    def correct(self, item, content, key=None):
        return self.store.remember(content, item['course'], kind='correction',
                                   supersedes=item['id'], idempotency_key=key,
                                   expected_revision=self.store.revision())

    def test_lineage_status_metadata_and_revoked_leaf_never_restores_old(self):
        first = self.store.remember('原下一步', 'Java', 'next_step', origin='user', evidence='本人核对')
        second = self.correct(first, '第一次更正')
        third = self.correct(second, '最终更正')
        for selected in (first, second, third):
            detail = self.store.memory_detail(selected['id'])
            self.assertEqual(detail['versions_order'], 'oldest_first')
            self.assertEqual([m['id'] for m in detail['versions']], [first['id'], second['id'], third['id']])
            self.assertEqual([m['status'] for m in detail['versions']], ['replaced', 'replaced', 'active'])
            self.assertEqual(detail['item']['id'], selected['id'])
        original = self.store.memory_detail(first['id'])['item']
        for field in ('content', 'origin', 'evidence', 'created_at', 'kind', 'supersedes'):
            self.assertEqual(original[field], first[field])
        self.store.forget(third['id'])
        self.assertEqual(self.store.memory_detail(third['id'])['item']['status'], 'revoked')
        self.assertEqual(self.store.memories('Java'), [])
        self.assertEqual(self.store.memory_page('Java', state='history')['total'], 3)
        self.assertNotIn('原下一步', self.store.snapshot('Java')['handoff'])
        self.assertEqual(self.store.search('最终更正', 'Java'), [])
        with self.assertRaises(ValueError):
            self.store.read_source('memory-' + first['id'])
        with self.assertRaises(ValueError):
            self.correct(third, '已撤销条目不允许复活')

    def test_pages_search_course_isolation_and_validation(self):
        for i in range(7):
            self.store.remember(f'Alpha 记忆 {i}', 'Java')
        shared = self.store.remember('ALPHA 公共记忆', '综合')
        self.store.remember('Alpha 另一课程', '离散数学')
        first = self.store.memory_page('Java', 'alpha', offset=0, limit=3)
        second = self.store.memory_page('Java', 'alpha', offset=3, limit=3)
        last = self.store.memory_page('Java', 'alpha', offset=6, limit=3)
        self.assertEqual(first['total'], 8)
        self.assertEqual(first['revision'], self.store.revision())
        ids = [m['id'] for page in (first, second, last) for m in page['items']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(shared['id'], ids)
        self.assertEqual(self.store.memory_page('Java', offset=999)['items'], [])
        self.assertEqual(self.store.memory_page(state='all')['total'], 9)
        for kwargs in ({'course':'不存在'}, {'state':'deleted'}, {'query':None},
                       {'offset':-1}, {'offset':True}, {'offset':'0'}, {'offset':0.5},
                       {'limit':0}, {'limit':101}, {'limit':False}, {'limit':'30'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.memory_page(**kwargs)
        for mid in ('missing', '', None, 42):
            with self.subTest(mid=mid), self.assertRaises(ValueError):
                self.store.memory_detail(mid)

    def test_delete_conflict_and_repeat_are_idempotent(self):
        item = self.store.remember('撤销目标', 'Java')
        stale = self.store.revision()
        self.store.remember('其他新记录', 'Java')
        with self.assertRaisesRegex(ValueError, '已经变化'):
            self.store.forget(item['id'], stale)
        self.assertEqual(self.store.memory_detail(item['id'])['item']['status'], 'active')
        result = self.store.forget(item['id'], self.store.revision())
        self.assertEqual(result['revision'], self.store.revision())
        self.assertEqual(self.store.forget(item['id'], stale), result)
        with self.assertRaises(ValueError):
            self.store.forget('missing', self.store.revision())
        for rev in (True, '2', -1, 1.5):
            with self.subTest(rev=rev), self.assertRaises(ValueError):
                self.store.forget(item['id'], rev)

    def test_idempotency_rejects_different_effective_semantics(self):
        item = self.store.remember('相同文字', 'Java', idempotency_key='same')
        revision = self.store.revision()
        for kwargs in ({'kind':'next_step'}, {'origin':'other'}, {'evidence':'其他依据'}, {'supersedes':item['id']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.remember('相同文字', 'Java', idempotency_key='same', **kwargs)
        self.assertEqual(self.store.revision(), revision)
        replacement = self.correct(item, '更正内容', 'fix')
        self.assertEqual(self.correct(item, '更正内容', 'fix')['id'], replacement['id'])
        with self.assertRaises(ValueError):
            self.store.remember('更正内容', 'Java', kind='preference', supersedes=item['id'], idempotency_key='fix')

    def test_corrected_next_step_snapshot_delta_search_and_actual_plan(self):
        item = self.store.remember('旧下一步 OLD_ANCHOR', 'Java', 'next_step')
        baseline = self.store.snapshot('Java')
        replacement = self.correct(item, '新下一步 NEW_ANCHOR')
        self.assertEqual(replacement['kind'], 'next_step')
        delta = self.store.snapshot('Java', baseline['revision'])
        self.assertEqual(delta['deleted_ids'], [item['id']])
        self.assertEqual([m['id'] for m in delta['memories']], [replacement['id']])
        self.assertEqual(self.store.snapshot('Java')['next_step'], replacement['content'])
        self.assertEqual(StudyFlow(self.store).plan('Java')['plan']['next_step'], replacement['content'])
        self.assertEqual(self.store.search('OLD_ANCHOR', 'Java'), [])
        self.assertIn('memory-' + replacement['id'], [r['id'] for r in self.store.search('NEW_ANCHOR', 'Java')])
        revision = self.store.revision()
        self.store.forget(replacement['id'], revision)
        self.assertEqual(self.store.snapshot('Java', revision)['deleted_ids'], [replacement['id']])
        self.assertNotIn('NEW_ANCHOR', StudyFlow(self.store).plan('Java')['plan']['context'])

    def test_corrected_finish_uses_current_memory_and_preserves_run_payload(self):
        flow = StudyFlow(self.store)
        self.store.remember('可用下一步', 'Java', 'next_step')
        run = flow.start('Java', idempotency_key='finish-test')['run']
        finished = flow.finish(run['id'], '旧卡点不可复活', skip_quiz=True, stop_note='旧收尾')
        original = self.store.memory_detail(finished['memory_id'])['item']
        corrected = self.correct(original, '本人留下的停点：新收尾\n本人确认的卡点：新卡点')
        self.assertEqual(flow.plan('Java')['plan']['next_step'], '新卡点')
        self.assertEqual(flow.get(run['id']), finished)
        corrected_again = self.correct(corrected, '只继续新章节，不使用原任务卡点')
        self.assertEqual(flow.plan('Java')['plan']['next_step'], corrected_again['content'])
        self.store.forget(corrected_again['id'])
        plan = flow.plan('Java')['plan']
        self.assertEqual(plan['next_step'], '可用下一步')
        self.assertNotIn('旧卡点', plan['prompt'])
        self.assertEqual(flow.get(run['id']), finished)

    def test_correction_and_revocation_keep_original_answers_and_feedback(self):
        flow = StudyFlow(self.store, FakeTutor())
        run = flow.start('Java', idempotency_key='answer-history')['run']
        quiz = flow.check(run['id'])
        answers = [{'id': q['id'], 'answer': '  原始回答\n保留换行  '} for q in quiz['quiz']['questions']]
        flow.answer(run['id'], answers, 'answer-history-submit')
        finished = flow.finish(run['id'], '原始卡点')
        original = self.store.memory_detail(finished['memory_id'])['item']
        replacement = self.correct(original, '本人确认的卡点：更新卡点')
        self.assertEqual(flow.plan('Java')['plan']['next_step'], '更新卡点')
        self.assertEqual(flow.get(run['id']), finished)
        self.assertEqual(flow.get(run['id'])['answers'], answers)
        self.assertEqual(flow.get(run['id'])['feedback']['status'], 'ready')
        self.store.forget(replacement['id'])
        self.assertNotIn('原始卡点', flow.plan('Java')['plan']['prompt'])
        self.assertEqual(flow.get(run['id']), finished)


if __name__ == '__main__':
    unittest.main()
