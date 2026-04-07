#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Exhibit Pre-Extractor
=====================
서버 PDF의 모든 exhibit 이미지를 미리 추출해서 파일로 저장합니다.

Usage:
    python3 extract_exhibits.py                  # WORKSPACE의 모든 PDF 처리
    python3 extract_exhibits.py path/to/file.pdf # 특정 PDF만 처리
    python3 extract_exhibits.py --clean          # exhibits/ 디렉토리 초기화

Output:
    exhibits/<pdf_name_without_ext>/NO.9_n1.jpg
    exhibits/<pdf_name_without_ext>/NO.9_n2.jpg
    exhibits/<pdf_name_without_ext>/NO.9_n2.absent  # n2가 없는 경우 마커 파일
"""

import os
import sys
import base64
import shutil

# quiz_server_cloud 모듈에서 필요한 함수/상수 임포트
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quiz_server_cloud import (
    WORKSPACE, EXHIBIT_DIR,
    find_pdfs, extract_questions_from_pdf,
    render_page_base64, _build_exhibit_pages_map,
    HAS_FITZ,
)


def extract_for_pdf(pdf_path: str, force: bool = False):
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir  = os.path.join(EXHIBIT_DIR, pdf_name)
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n📄 {pdf_name}")
    print(f"   Extracting questions...")
    questions = extract_questions_from_pdf(pdf_path)
    exhibit_qs = [q for q in questions if q.get('has_exhibit') and q.get('page_num', 0) > 0]
    print(f"   {len(questions)} questions total, {len(exhibit_qs)} with exhibits")

    if not exhibit_qs:
        print("   (no exhibits to extract)")
        return

    print(f"   Building exhibit pages map...")
    _build_exhibit_pages_map(pdf_path)

    ok = 0
    skipped = 0
    for q in exhibit_qs:
        q_num    = q['num']       # e.g. "NO.9"
        page_num = q['page_num']

        for n in (1, 2):
            img_path    = os.path.join(out_dir, f"{q_num}_n{n}.jpg")
            absent_path = os.path.join(out_dir, f"{q_num}_n{n}.absent")

            if not force and (os.path.exists(img_path) or os.path.exists(absent_path)):
                skipped += 1
                continue

            b64 = render_page_base64(pdf_path, page_num, q_num, exhibit_n=n)
            if b64:
                with open(img_path, 'wb') as f:
                    f.write(base64.b64decode(b64))
                # 혹시 남아있던 absent 마커 제거
                if os.path.exists(absent_path):
                    os.remove(absent_path)
                ok += 1
                print(f"   ✅ {q_num} n={n}")
            else:
                # 없음 마커 생성 (서버가 빠르게 absent 판단하도록)
                open(absent_path, 'w').close()
                if os.path.exists(img_path):
                    os.remove(img_path)
                if n == 1:
                    print(f"   ⚠️  {q_num} n={n}: render failed")

    print(f"   Done: {ok} extracted, {skipped} skipped")


def main():
    args = sys.argv[1:]

    if '--clean' in args:
        if os.path.exists(EXHIBIT_DIR):
            shutil.rmtree(EXHIBIT_DIR)
            print(f"🗑  Cleaned: {EXHIBIT_DIR}")
        return

    force = '--force' in args
    targets = [a for a in args if not a.startswith('--')]

    if not HAS_FITZ:
        print("❌ PyMuPDF(fitz) is not installed. Cannot extract exhibits.")
        sys.exit(1)

    os.makedirs(EXHIBIT_DIR, exist_ok=True)

    if targets:
        for t in targets:
            if not os.path.isfile(t):
                print(f"❌ File not found: {t}")
                continue
            extract_for_pdf(os.path.abspath(t), force=force)
    else:
        pdfs = find_pdfs()
        if not pdfs:
            print("❌ No PDFs found in WORKSPACE.")
            sys.exit(1)
        print(f"Found {len(pdfs)} PDF(s) in {WORKSPACE}")
        for p in pdfs:
            extract_for_pdf(p['path'], force=force)

    print("\n✅ All done.")


if __name__ == '__main__':
    main()
