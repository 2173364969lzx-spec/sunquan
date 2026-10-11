"""Check citations deterministically; matching text is not proof of reasoning."""
import json
import re


INSTRUCTION = (
    '只输出JSON，不要输出Markdown代码围栏。先判断给定原文能否支持当前问题。'
    '有依据时输出 {"supported":true,"explanation":"不超过600字的简短解释",'
    '"next_step":"只给一个思考提示或小问题，不超过200字",'
    '"citations":[{"source_id":"输入中的资料id","quote":"逐字摘取一段相关原文，不超过400字"}]}。'
    '最多引用3处；不得引用历史助手回答、学习计划或记忆来证明课程知识。'
    '资料不足、仅有接续计划或无法确定时输出 {"supported":false}，不要靠猜测补齐。'
    '解释只能围绕引文；不要引入原文外的实现细节，不要一次讲完多个知识点。'
    '不得声称回答通过正确性验证、本人已掌握、已提交或已保存记忆。'
)


def source_quote(quote, source):
    """Match formatting whitespace, returning the exact original source span."""
    if quote in source:
        return quote
    positions = [i for i, char in enumerate(source) if not char.isspace()]
    plain = ''.join(source[i] for i in positions)
    target = ''.join(quote.split())
    start = plain.find(target)
    if not target or start < 0:
        raise ValueError('引用不在原文中。')
    return source[positions[start]:positions[start + len(target) - 1] + 1]


def _short(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('回答格式不完整。')
    return value.strip()


def render_answer(raw, sources):
    """Discard unsupported output, not just decorate it with a disclaimer."""
    fallback = '当前资料不足以核对这个问题。请补充对应的课件段落或原题，或把问题缩小到一个具体概念。'
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('supported'), bool):
            raise ValueError('回答格式不完整。')
        if not data['supported']:
            return fallback, {'status': 'insufficient', 'evidence_count': 0}
        explanation = _short(data.get('explanation'), 1200)
        next_step = _short(data.get('next_step'), 400)
        if re.search(r'已(?:经)?掌握|完全掌握|已(?:经)?学会|已(?:经)?提交|已(?:经)?保存|通过.{0,8}(?:验证|核验)', explanation + next_step):
            raise ValueError('模型夸大了结果。')
        citations = data.get('citations')
        if not isinstance(citations, list) or not 1 <= len(citations) <= 3:
            raise ValueError('缺少引用。')
        by_id = {s['id']: (i + 1, s) for i, s in enumerate(sources)}
        quotes = []
        seen = set()
        for item in citations:
            if not isinstance(item, dict) or not isinstance(item.get('source_id'), str) or item['source_id'] not in by_id:
                raise ValueError('引用标识无效。')
            number, source = by_id[item['source_id']]
            quote = source_quote(_short(item.get('quote'), 400), source['excerpt'])
            if (number, quote) not in seen:
                quotes.append(f'[资料{number}] {quote}')
                seen.add((number, quote))
        # Model-written source numbers are untrusted; labels come from validated IDs.
        explanation = re.sub(r'\[资料[^\]]*\]', '', explanation)
        next_step = re.sub(r'\[资料[^\]]*\]', '', next_step)
        answer = ('原文依据\n' + '\n\n'.join(quotes) + '\n\n参考解释\n' + explanation +
                  '\n\n先试这一步\n' + next_step + '\n\n引用已对照原文；解释仍需核对，不代表已掌握。')
        return answer, {'status': 'cited', 'evidence_count': len(quotes)}
    except (ValueError, TypeError, KeyError):
        return ('这次模型回答的引用或格式未能通过核对，已隐藏该解释。\n' + fallback,
                {'status': 'invalid', 'evidence_count': 0})
