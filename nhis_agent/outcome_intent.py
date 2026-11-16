"""Shared, bounded outcome phrase recognition for routing and scope checks.

Mentions describe registered event requests, never observations or estimates.
Source spans preserve shared-object phrases such as "delayed or unmet care".
This vocabulary is deliberately finite; it is not a general clinical parser.
"""
from dataclasses import dataclass
import re


OUTCOME_ORDER = ('fair_poor', 'insured', 'uninsured', 'delayed_care_cost', 'unmet_care_cost')


@dataclass(frozen=True)
class OutcomeMention:
    outcomes: tuple
    text: str
    start: int
    end: int


def outcome_mentions(question):
    text = question if isinstance(question, str) else str(question)
    q = text.lower()
    found = []
    def add(pattern, codes):
        for match in re.finditer(pattern, q):
            found.append(OutcomeMention(tuple(codes), text[match.start():match.end()],
                                        match.start(), match.end()))

    add(r'自评健康|self.?rated(?: health)?|fair.{0,5}poor(?: health)?|phstat_a', ('fair_poor',))
    nonmedical = re.search(r'车险|寿险|财产保险|旅游保险|\b(?:car|auto|life|home|property|travel) insurance\b', q)
    insurance = re.search(r'保险|医保|参保|\binsurance\b|\binsured\b|\buninsured\b|notcov_a', q)
    if insurance and not nonmedical:
        negative = list(re.finditer(r'未参保|未投保|没有.{0,4}(?:医保|保险)|无医保|无保险|'
            r'\buninsured\b|\bnot insured\b|\bwithout (?:health )?insurance\b|\bnot covered\b', q))
        positive = [match for match in re.finditer(r'有医保|有保险|已参保|\binsured\b', q)
                    if not any(item.start() <= match.start() and match.end() <= item.end() for item in negative)]
        if re.search(r'有无|是否|\bwhether\b', q) or not negative and not positive:
            add(r'(?:health )?insurance(?: coverage)?|\binsured\b|\buninsured\b|医疗保险|健康保险|医保|参保|notcov_a',
                ('insured', 'uninsured'))
        else:
            for codes, matches in ((('uninsured',), negative), (('insured',), positive)):
                found.extend(OutcomeMention(codes, text[match.start():match.end()],
                                            match.start(), match.end()) for match in matches)

    # The object may be shared with another verb/event; only the actual delay
    # word is recorded, with a bounded lookahead requiring a medical object.
    add(r'meddl12m_a|(?:延迟|推迟)(?=[^，。？！\n]{0,18}(?:就医|医疗|看病))|'
        r'\bdelay(?:ed|ing)?\b(?=[^.;?!\n]{0,40}\b(?:care|medical|treatment)\b)',
        ('delayed_care_cost',))
    add(r'medng12m_a|未就医|未获得.{0,8}(?:医疗|服务)|未能.{0,5}(?:就医|看病)|无法.{0,5}(?:就医|看病)|'
        r'未满足.{0,5}(?:医疗|需求)|\bunmet\b(?=[^.;?!\n]{0,25}\b(?:care|medical|need)\b)|'
        r'\b(?:forgo|forego|forgone|foregone|without|did not get|not get|fail(?:ed)? to (?:obtain|get)|'
        r'unable to (?:obtain|get))\b(?=[^.;?!\n]{0,35}\b(?:medical|needed care)\b)',
        ('unmet_care_cost',))
    broad = r'医疗服务指标|医疗服务.{0,10}分布|医疗可及性|医疗服务可及性|health.?care (?:access|service) (?:indicators?|measures?)'
    add(broad, ('insured', 'uninsured', 'delayed_care_cost', 'unmet_care_cost'))
    add(r'及时就医|及时.{0,5}(?:医疗|看病)|timely.{0,12}(?:care|medical)|access to (?:medical |health )?care|'
        r'就医费用障碍|医疗费用障碍|cost.?related barriers|cost barriers',
        ('delayed_care_cost', 'unmet_care_cost'))
    return tuple(sorted(set(found), key=lambda item: (item.start, item.end, item.outcomes)))


def selected_outcomes(question):
    requested = {code for mention in outcome_mentions(question) for code in mention.outcomes}
    return tuple(code for code in OUTCOME_ORDER if code in requested)
