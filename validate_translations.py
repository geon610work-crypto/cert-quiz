#!/usr/bin/env python3
"""
translation_cache.json 정합성 검증 도구
========================================

목적
----
한국어 번역(options_ko)이 현재 PDF에서 추출되는 영어 원문(options)과
"알파벳 단위로 올바르게 정렬"되어 있는지 자동 점검한다.

배경 (왜 어긋나는가)
--------------------
generate_translations.py 는 번역을 문제 번호(pdf_name::NO.x)로만 키잉하고,
한 번 채워진 번역은 다시 건드리지 않는다(skip). 그래서 다음 상황에서
한국어 선택지 라벨(A/B/C/D)이 영어와 어긋날 수 있다:
  - 덤프 PDF 버전이 바뀌어 같은 번호 문제의 선택지 순서가 달라짐
  - 추출 로직이 바뀜
이때 한국어는 옛 순서로 고정된 채라, 사용자는 정답 라벨과 다른 내용을 읽게 된다.

검증 원리
---------
언어에 무관한 "앵커 토큰"(괄호 속 영어, 명령어/파라미터, 약어, 숫자, 기술 영단어)을
한국어·영어 양쪽에서 추출한다. 한국어 선택지 X가 자기 알파벳(X)의 영어보다
다른 알파벳(Y)의 영어와 더 많이 겹치면 "오정렬 의심"으로 보고한다.

신뢰도
------
HIGH  : 여러 선택지가 일관된 다른 위치를 가리키거나(순열), self=0 & best>=2
LOW   : self/best 차이가 1뿐 — 공통 기술 토큰(inbound/outbound, UDP/TCP 등)이나
        OCR로 깨진 영어 원문 때문일 가능성 (대개 거짓양성)

사용법
------
    python3 validate_translations.py            # 전체 검사
    python3 validate_translations.py --all       # 거짓양성(LOW)까지 모두 출력
"""
import json, sys, re, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quiz_server_cloud import extract_questions_from_pdf, find_pdfs
from collections import defaultdict

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
TRANS_FILE = os.path.join(WORKSPACE, 'translation_cache.json')

# 수동 검증 완료된 거짓양성 (영어 원문이 OCR로 깨져 앵커 매칭이 실패하나,
# 한국어는 알파벳별로 정확히 정렬됨이 확인된 항목)
KNOWN_FALSE_POSITIVES = {
    # IKEv2 메시지 순서 문제: 영어가 'IKESAJNIT'(=IKE_SA_INIT) 등으로 OCR 손상.
    # 한국어 A/B/C/D는 각 시퀀스 순서대로 영어와 정확히 일치. 정답 C도 일치.
    'FCSS_NST_SE-7.6 V13.35.pdf::NO.87',
    'FCSS_NST_SE-7.6 V13.65.pdf::NO.98',
}

_STOP = {'this','that','from','with','must','only','when','will','they','then','than',
         'enable','option','places','administrator','into','user','users','group','groups',
         'server','website','websites','profile','allow','correct','ensure','their','have',
         'does','what','which','where','because','about','take','make','made','time','both',
         'same','setting','settings','configure','using','used','device','devices'}

def anchors(text):
    """언어 독립적 앵커 토큰 집합."""
    if not text:
        return set()
    toks = set()
    for m in re.findall(r'\(([A-Za-z][^)]*)\)', text):           # 괄호 속 영어
        for w in re.findall(r'[A-Za-z][A-Za-z0-9\-_.]{2,}', m):
            toks.add(w.lower())
    for w in re.findall(r'[a-zA-Z]+(?:[-_][a-zA-Z0-9]+)+', text):  # 명령어/파라미터
        toks.add(w.lower())
    for w in re.findall(r'\b[A-Z]{2,}\b', text):                   # 약어
        toks.add(w.lower())
    for w in re.findall(r'\b\d+(?:\.\d+)?\b', text):               # 숫자
        toks.add(w)
    for w in re.findall(r'\b[A-Za-z]{4,}\b', text):               # 기술 영단어
        lw = w.lower()
        if lw not in _STOP:
            toks.add(lw)
    return toks

def audit():
    with open(TRANS_FILE, encoding='utf-8') as f:
        trans = json.load(f)

    pdf_by_name = {p['name']: p['path'] for p in find_pdfs()}
    by_pdf = defaultdict(dict)
    for k, v in trans.items():
        if '::' not in k:
            continue
        fname, qnum = k.rsplit('::', 1)
        by_pdf[fname][qnum] = v

    results = []   # (level, fname, qnum, crosses)
    checked = 0
    for fname, entries in sorted(by_pdf.items()):
        path = pdf_by_name.get(fname)
        if not path or not os.path.exists(path):
            continue
        q_map = {q['num']: q for q in extract_questions_from_pdf(path)}
        for qnum, tv in entries.items():
            cur = q_map.get(qnum)
            if not cur:
                continue
            en = cur.get('options') or {}
            ko = tv.get('options') or {}
            letters = [l for l in 'ABCDEF' if l in en and l in ko]
            if len(letters) < 2:
                continue
            checked += 1
            en_anc = {l: anchors(en[l]) for l in letters}
            ko_anc = {l: anchors(ko[l]) for l in letters}
            crosses = []
            for l in letters:
                ka = ko_anc[l]
                if not ka:
                    continue
                scores = {l2: len(ka & en_anc[l2]) for l2 in letters}
                best = max(scores, key=scores.get)
                if scores[best] == 0:
                    continue
                if best != l and scores[best] > scores[l]:
                    crosses.append((l, best, scores[l], scores[best]))
            if not crosses:
                continue
            if f'{fname}::{qnum}' in KNOWN_FALSE_POSITIVES:
                continue
            # 신뢰도 판정
            strong = (len(crosses) >= 2 or
                      any(s == 0 and bs >= 2 for _, _, s, bs in crosses) or
                      any(bs - s >= 2 for _, _, s, bs in crosses))
            results.append(('HIGH' if strong else 'LOW', fname, qnum, crosses))
    return checked, results

def main():
    show_all = '--all' in sys.argv
    checked, results = audit()
    high = [r for r in results if r[0] == 'HIGH']
    low  = [r for r in results if r[0] == 'LOW']

    print('=' * 70)
    print(f'translation_cache 정합성 검사 — 검사 {checked}건')
    print('=' * 70)
    print(f'  HIGH(실제 오류 의심): {len(high)}건   LOW(대개 거짓양성): {len(low)}건\n')

    def dump(rows, title):
        print(f'\n### {title}')
        cur = None
        for level, fname, qnum, crosses in rows:
            if fname != cur:
                print(f'\n  {fname}')
                cur = fname
            cd = ', '.join(f'{l}→{b}(self {s} vs {bs})' for l, b, s, bs in crosses)
            print(f'    {qnum}: {cd}')

    if high:
        dump(high, 'HIGH — 우선 확인 필요')
    else:
        print('\n### HIGH 오정렬 없음 ✅')
    if show_all and low:
        dump(low, 'LOW — 참고 (공통 토큰/OCR 잡음일 가능성 높음)')

    # 종료 코드: HIGH 가 있으면 1
    sys.exit(1 if high else 0)

if __name__ == '__main__':
    main()
