#!/usr/bin/env python3
"""
korean_cache.json 해설↔정답 정합성 검증 도구
==============================================

목적
----
한국어 해설(korean_cache.json)이 표시하는 "정답 알파벳"(✅ X가 정답 / 정답: X, Y)이
현재 PDF에서 추출되는 덤프 정답(answer)과 일치하는지 자동 점검한다.

배경 (왜 어긋나는가)
--------------------
해설은 문제 번호로만 키잉되며 한 번 작성되면 갱신되지 않는다. 그런데
  - translation_cache 의 선택지 라벨을 재매핑(remap)하거나
  - 덤프 PDF 버전이 바뀌어 같은 번호의 선택지 순서/내용이 달라지면
해설의 ✅/❌ 알파벳이 실제 정답과 어긋나, 사용자가 틀린 라벨을 정답으로 학습하게 된다.

검증 원리
---------
해설 본문에서 "✅ X가 정답", "✅ X.", "정답: X, Y" 패턴으로 정답 알파벳을 추출해
PDF 추출 answer 집합과 비교한다. 불일치 시 보고한다. (question_overrides.json 의
answer_conflict 로 의도적으로 라벨을 바꾼 항목은 제외한다.)

사용법
------
    python3 validate_explanations.py
"""
import json, sys, re, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quiz_server_cloud import extract_questions_from_pdf, find_pdfs
from collections import defaultdict

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
KOREAN_FILE = os.path.join(WORKSPACE, 'korean_cache.json')
OVERRIDES_FILE = os.path.join(WORKSPACE, 'question_overrides.json')

def extract_correct_letters(exp):
    """해설에서 정답으로 표시된 알파벳 집합 추출."""
    letters = set()
    # "정답: A, B" / "정답：A,B"
    for m in re.findall(r'정답\s*[:：]\s*([A-F](?:\s*,\s*[A-F])*)', exp):
        letters |= set(re.findall(r'[A-F]', m))
    # "✅ X가 정답" / "✅ X." / "✅ **X**" / "✅ **A, D가 정답**" (콤마 나열 포함)
    # 단, 알파벳이 단독 토큰일 때만 (뒤가 글자면 'FGSP'의 F 같은 오인 방지)
    for m in re.finditer(r'✅\s*\**\s*([A-F](?:\s*,\s*[A-F])*)(?![A-Za-z])', exp):
        letters |= set(re.findall(r'[A-F]', m.group(1)))
    # "A. ✅", "A ✅" — 라벨이 ✅ 앞에 오는 형식
    for m in re.findall(r'(?:^|[\s*])([A-F])[.)]?\s*✅', exp):
        letters.add(m)
    return letters

def main():
    with open(KOREAN_FILE, encoding='utf-8') as f:
        kc = json.load(f)
    overrides = {}
    if os.path.exists(OVERRIDES_FILE):
        with open(OVERRIDES_FILE, encoding='utf-8') as f:
            overrides = json.load(f)

    def has_conflict(fname, qnum):
        base = fname[:-4] if fname.endswith('.pdf') else fname
        return 'answer_conflict' in (overrides.get(base, {}).get(qnum, {}) or {})

    pdf_by_name = {p['name']: p['path'] for p in find_pdfs()}
    by_pdf = defaultdict(dict)
    for k, v in kc.items():
        if '::' not in k:
            continue
        fname, qnum = k.rsplit('::', 1)
        by_pdf[fname][qnum] = v

    checked = 0
    mismatches = []   # (fname, qnum, exp_letters, dump_letters, conflict)
    no_letters = []   # 해설에서 정답 알파벳을 못 찾음
    for fname, entries in sorted(by_pdf.items()):
        path = pdf_by_name.get(fname)
        if not path or not os.path.exists(path):
            continue
        q_map = {q['num']: q for q in extract_questions_from_pdf(path)}
        for qnum, exp in entries.items():
            cur = q_map.get(qnum)
            if not cur:
                continue
            dump = set(cur.get('answer') or [])
            if not dump:
                continue
            checked += 1
            exp_letters = extract_correct_letters(exp)
            if not exp_letters:
                no_letters.append((fname, qnum))
                continue
            if exp_letters != dump:
                mismatches.append((fname, qnum, exp_letters, dump, has_conflict(fname, qnum)))

    print('=' * 70)
    print(f'해설↔정답 정합성 검사 — 검사 {checked}건')
    print('=' * 70)
    real = [m for m in mismatches if not m[4]]
    conflicts = [m for m in mismatches if m[4]]
    print(f'  불일치(실제 오류 의심): {len(real)}건   '
          f'override 처리됨: {len(conflicts)}건   '
          f'정답 미검출: {len(no_letters)}건\n')

    if real:
        print('### 불일치 — 해설 정답 알파벳이 덤프와 다름 (확인 필요)')
        cur = None
        for fname, qnum, el, dl, _ in sorted(real):
            if fname != cur:
                print(f'\n  {fname}'); cur = fname
            print(f'    {qnum}: 해설={sorted(el)}  덤프={sorted(dl)}')
    else:
        print('### 불일치 없음 ✅')

    sys.exit(1 if real else 0)

if __name__ == '__main__':
    main()
