#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_brave_dump.py  —  신규 NSE4 덤프(Brave-Dumps 형식) 전용 오프라인 변환기.

기존 PDF 파이프라인(extract_questions_from_pdf / exhibits 추출)을 일절 건드리지 않고,
신규 덤프를 우리 앱이 그대로 소비할 수 있는 산출물로 변환만 한다.

생성물 (모두 신규 — 기존 파일과 충돌 없음):
  - brave_questions.json                : 문제/선택지/정답/exhibit 메타
  - exhibits/<STEM>/<num>_n1.jpg ...     : exhibit 이미지 (기존 네이밍 컨벤션)
  - exhibits/<STEM>/<num>_n1.absent      : "그림 없음" 센티넬

분리 기준 (텍스트 3-앵커):
  ① 문제 블록 : "Question #N -"  →  다음 "Question #N+1 -" 직전
  ② 문제/선택지: 줄 시작 첫 "A." 앞 = 문제 본문, 뒤 = 선택지(여러 줄 이어붙임)
  ③ 선택지/정답: "Correct Answer:" 앞 = 선택지 끝, 뒤(Clients Votes…) = 폐기
exhibit 이미지:
  - 본문에 "Refer to the exhibit(s)" 있으면 has_exhibit
  - 문제 마커 ~ "Correct Answer:" 사이(= stem+선택지 영역)에 배치된 이미지만 추출
    → 그 뒤 커뮤니티 투표 영역의 이미지/다음 문제 이미지는 제외
"""
import os, re, json, sys
import fitz  # PyMuPDF

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SRC_PDF   = os.path.join(WORKSPACE, '_NSE 4 - FortiOS 7.6 Administrator (FCP - Fortigate 7.6) Exam Materials.pdf')
STEM      = 'NSE4_FGT_AD-7.6 Brave'          # exhibits/<STEM>/ 및 시험 식별용
OUT_JSON  = os.path.join(WORKSPACE, 'brave_questions.json')
EX_DIR    = os.path.join(WORKSPACE, 'exhibits', STEM)

MIN_W, MIN_H = 180, 90    # exhibit 후보 최소 크기(px in page units)


def is_exhibit_size(w, h):
    """exhibit 후보 크기 판정. 일반 그림 + 가로로 긴 로그 스트립 모두 허용."""
    if w >= MIN_W and h >= MIN_H:
        return True
    if w >= 300 and h >= 40 and (w * h) >= 15000:   # 넓고 낮은 로그/표 스트립
        return True
    return False

OPT_RE    = re.compile(r'^([A-F])\.\s+(.*)$')
ANS_RE    = re.compile(r'Correct Answer:\s*([A-F](?:\s*,\s*[A-F])*)')
MARK_RE   = re.compile(r'Question #(\d+)\s*[-–]')


# 매 페이지 반복되는 Brave-Dumps 머리글/워터마크 (페이지 경계에서 본문에 섞임)
_BOILERPLATE = {
    'brave-dumps.com',
    '● nse 4 - fortios 7.6',
    'nse 4 - fortios 7.6',
    'administrator (fcp - fortigate',
    '7.6) exam materials -',
    'kwangsu_kim',
}


def strip_boilerplate(text):
    """반복 머리글 줄 제거 + 줄 안에 섞인 머리글 토막 제거."""
    out = []
    for line in text.split('\n'):
        s = line.strip().lower()
        if s in _BOILERPLATE:
            continue
        out.append(line)
    text = '\n'.join(out)
    # 한 줄 안에 이어붙은 머리글 조각 제거 (선택지/본문 중간 침투분)
    text = re.sub(
        r'\s*Brave-Dumps\.com\s*●?\s*NSE 4 - FortiOS 7\.6\s*'
        r'Administrator \(FCP - Fortigate\s*7\.6\) Exam Materials -\s*Kwangsu_Kim\s*',
        ' ', text)
    text = re.sub(r'\s*●?\s*Brave-Dumps\.com\s*', ' ', text)
    return text


def clean(s):
    return re.sub(r'[ \t]+', ' ', s).strip()


def parse_options(opt_text):
    """선택지 영역 텍스트 → {letter: full_text}. 여러 줄 선택지 이어붙임."""
    opts = {}
    cur = None
    for raw in opt_text.split('\n'):
        line = raw.strip()
        if not line:
            continue
        m = OPT_RE.match(line)
        if m:
            cur = m.group(1)
            opts[cur] = m.group(2).strip()
        elif cur is not None:
            # 이전 선택지의 연속 줄
            opts[cur] = (opts[cur] + ' ' + line).strip()
    return opts


def find_marker_y(page, qnum):
    """페이지에서 'Question #<qnum>' 의 상단 y좌표. 없으면 None."""
    rects = page.search_for(f'Question #{qnum}')
    if rects:
        return min(r.y0 for r in rects)
    return None


def find_answer_y(page, min_y=None):
    """페이지의 'Correct Answer:' y좌표. min_y가 주어지면 그보다 아래 첫 등장만."""
    rects = sorted(page.search_for('Correct Answer:'), key=lambda r: r.y0)
    for r in rects:
        if min_y is None or r.y0 > min_y:
            return r.y0
    return None


def main():
    if not os.path.isfile(SRC_PDF):
        print(f"❌ 원본 PDF 없음: {SRC_PDF}")
        sys.exit(1)

    doc = fitz.open(SRC_PDF)
    npages = doc.page_count
    page_text = [doc[i].get_text() for i in range(npages)]
    alltext = "\n".join(page_text)

    # 문제 마커가 위치한 페이지 인덱스 수집
    marker_page = {}   # qnum -> page index of its "Question #N" marker
    for i, pt in enumerate(page_text):
        for m in MARK_RE.finditer(pt):
            qn = int(m.group(1))
            if qn not in marker_page:        # 첫 등장(마커) 페이지
                marker_page[qn] = i

    # 본문 파싱: 전체 텍스트를 마커로 분할
    parts = MARK_RE.split(alltext)
    questions = []
    for k in range(1, len(parts), 2):
        qn = int(parts[k])
        body = parts[k + 1]
        am = ANS_RE.search(body)
        ans = am.group(1).replace(' ', '') if am else ''
        stem_block = body[:am.start()] if am else body
        stem_block = strip_boilerplate(stem_block)

        om = re.search(r'(?m)^\s*A\.\s', stem_block)
        if om:
            stem = clean(stem_block[:om.start()])
            opts = parse_options(stem_block[om.start():])
        else:
            stem = clean(stem_block)
            opts = {}

        has_ex = bool(re.search(r'[Rr]efer to the exhibit', stem_block))
        questions.append({
            'num': qn,
            'question': stem,
            'options': opts,
            'answer': ans,
            'has_exhibit': has_ex,
        })

    # exhibit 이미지 추출 (문제 마커 ~ Correct Answer 사이 영역만)
    os.makedirs(EX_DIR, exist_ok=True)
    ex_report = {}
    # 정답(Correct Answer) 이 위치한 페이지/ y 를 마커 이후 첫 등장으로 추정
    sorted_q = sorted(marker_page.items(), key=lambda x: x[0])
    qnum_to_idx = {q['num']: q for q in questions}

    for qi, (qn, mpage) in enumerate(sorted_q):
        q = qnum_to_idx.get(qn)
        if not q:
            continue
        next_mpage = sorted_q[qi + 1][1] if qi + 1 < len(sorted_q) else npages - 1

        my = find_marker_y(doc[mpage], qn)

        # 이 문제의 'Correct Answer:' 페이지/y 찾기.
        # 마커 페이지에서는 마커 y보다 아래 등장만 인정(이전 문제 정답 오인 방지).
        ans_page, ans_y = None, None
        for p in range(mpage, min(next_mpage, npages - 1) + 1):
            ay = find_answer_y(doc[p], min_y=(my if p == mpage else None))
            if ay is not None:
                ans_page, ans_y = p, ay
                break
        if ans_page is None:
            ans_page = next_mpage

        # 마커 ~ 정답 사이에 배치된 이미지 수집
        imgs = []
        for p in range(mpage, ans_page + 1):
            for b in doc[p].get_image_info(xrefs=True):
                bb = b['bbox']
                w, h = bb[2] - bb[0], bb[3] - bb[1]
                if not is_exhibit_size(w, h):
                    continue
                # 경계 페이지에서는 y범위로 필터
                if p == mpage and my is not None and bb[1] < my:
                    continue
                if p == ans_page and ans_y is not None and bb[1] > ans_y:
                    continue
                xref = b.get('xref', 0)
                if xref:
                    imgs.append((p, round(w), round(h), xref, round(bb[1])))

        # 동일 xref 중복 제거 (배치 순서 유지)
        seen, uniq = set(), []
        for it in imgs:
            if it[3] in seen:
                continue
            seen.add(it[3])
            uniq.append(it)

        if q['has_exhibit'] and uniq:
            saved = []
            for n, (p, w, h, xref, y) in enumerate(uniq, 1):
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:        # CMYK → RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    out = os.path.join(EX_DIR, f"NO.{qn}_n{n}.jpg")
                    pix.save(out)
                    pix = None
                    saved.append(os.path.basename(out))
                except Exception as e:
                    print(f"  ⚠️  Q{qn} 이미지 저장 실패(xref={xref}): {e}")
            q['exhibit_files'] = saved
            ex_report[qn] = saved
        elif q['has_exhibit']:
            # 그림 참조하지만 임베드 이미지가 실제로 없음 → 기존 컨벤션대로 '그림 없음' 센티넬
            with open(os.path.join(EX_DIR, f"NO.{qn}_n1.absent"), 'w') as f:
                f.write('')
            q['exhibit_files'] = []
            q['missing_exhibit'] = True
            ex_report[qn] = 'ABSENT'
        else:
            q['exhibit_files'] = []

    # 산출물 저장
    out = {
        'source_pdf': os.path.basename(SRC_PDF),
        'stem': STEM,
        'count': len(questions),
        'questions': questions,
    }
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 리포트
    ex_q = [q for q in questions if q['has_exhibit']]
    ok = [q['num'] for q in ex_q if q.get('exhibit_files')]
    miss = [q['num'] for q in ex_q if not q.get('exhibit_files')]
    multi = [(q['num'], len(q['exhibit_files'])) for q in ex_q if len(q.get('exhibit_files', [])) > 1]
    print(f"✅ 문제 {len(questions)}개 → {OUT_JSON}")
    print(f"   exhibit 문제 {len(ex_q)}개 | 이미지 추출 {len(ok)} | 미발견 {len(miss)} {miss}")
    print(f"   이미지 2장 이상 문제: {multi}")
    print(f"   이미지 저장 위치: {EX_DIR}")


if __name__ == '__main__':
    main()
