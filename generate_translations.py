"""
generate_translations.py
========================
두 PDF에서 문제를 파싱하여 translation_cache.json 빈 템플릿을 생성합니다.
이미 값이 채워진 항목은 덮어쓰지 않습니다.

사용법:
    python3 generate_translations.py

이후 translation_cache.json 을 열어 각 "question" / "options" 값을 채워넣으세요.
"""

import json
import os
import sys

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
TRANS_FILE = os.path.join(WORKSPACE, 'translation_cache.json')

PDFS = [
    'FCSS_NST_SE-7.6 V13.35.pdf',
    'FCSS_EFW_AD-7.6 V12.95.pdf',
]

# ── PDF 파싱 (quiz_server_cloud.py 의 extract_questions_from_pdf 와 동일 로직) ──
def parse_pdf(pdf_path):
    """pdfplumber 로 문제 파싱, fitz 가 있으면 fitz 우선."""
    questions = []

    try:
        import fitz
        doc = fitz.open(pdf_path)
        pages_text = [doc[i].get_text() for i in range(len(doc))]
        doc.close()
    except Exception:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                pages_text = [p.extract_text() or '' for p in pdf.pages]
        except Exception as e:
            print(f"  ❌ PDF 읽기 실패: {e}")
            return []

    full_text = '\n'.join(pages_text)

    import re
    blocks = re.split(r'(?=^NO\.\d+)', full_text, flags=re.MULTILINE)
    for block in blocks:
        m = re.search(r'^(NO\.(\d+))', block, re.MULTILINE)
        if not m:
            continue
        num = m.group(1)

        # 문제 텍스트: NO.XX 다음 줄부터 첫 A. 이전까지
        q_match = re.search(r'NO\.\d+\s*\n(.*?)(?=^[A-E]\.)', block, re.DOTALL | re.MULTILINE)
        question_text = q_match.group(1).strip() if q_match else ''
        question_text = re.sub(r'\n+', ' ', question_text).strip()

        # 선택지
        opt_matches = re.findall(r'^([A-E])\.\s*(.*?)(?=^[A-E]\.|^Answer:|^Explanation:|$)',
                                 block, re.MULTILINE | re.DOTALL)
        options = {}
        for letter, text in opt_matches:
            cleaned = re.sub(r'\n+', ' ', text).strip()
            if cleaned:
                options[letter] = cleaned

        if question_text or options:
            questions.append({'num': num, 'question': question_text, 'options': options})

    return questions


def main():
    # 기존 캐시 로드
    if os.path.isfile(TRANS_FILE):
        with open(TRANS_FILE, encoding='utf-8') as f:
            cache = json.load(f)
        print(f"기존 translation_cache.json 로드: {len(cache)}개 항목")
    else:
        cache = {}
        print("translation_cache.json 없음 — 새로 생성합니다.")

    added = 0
    skipped = 0

    for pdf_name in PDFS:
        pdf_path = os.path.join(WORKSPACE, pdf_name)
        if not os.path.isfile(pdf_path):
            print(f"\n⚠️  PDF 없음, 건너뜀: {pdf_name}")
            continue

        print(f"\n📄 파싱 중: {pdf_name}")
        questions = parse_pdf(pdf_path)
        print(f"   {len(questions)}개 문제 발견")

        for q in questions:
            key = f"{pdf_name}::{q['num']}"
            existing = cache.get(key, {})

            # 이미 번역 값이 채워진 항목은 건드리지 않음
            if existing.get('question') or any(existing.get('options', {}).values()):
                skipped += 1
                continue

            # 빈 템플릿 생성 (원문은 참고용으로 주석처럼 _src 에 보관)
            cache[key] = {
                'question': '',
                'options': {letter: '' for letter in sorted(q['options'].keys())},
                '_src': {
                    'question': q['question'],
                    'options': q['options'],
                }
            }
            added += 1

    # 저장
    with open(TRANS_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 완료!")
    print(f"   추가: {added}개 / 기존 유지: {skipped}개 / 전체: {len(cache)}개")
    print(f"   → {TRANS_FILE}")
    print()
    print("다음 단계: translation_cache.json 을 열어")
    print("  'question' 과 'options' 값을 직독직해 번역으로 채워 넣으세요.")
    print("  (_src 는 원문 참고용이며 서버에서는 무시됩니다)")


if __name__ == '__main__':
    main()
