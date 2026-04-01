#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📚 Certification Exam Quiz Tool
================================
Usage:
    python3 quiz_server.py

Then open: http://localhost:5555
Stop:      Ctrl+C
"""

import os, re, json, random, sys, socket, threading, webbrowser, uuid, tempfile
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─────────────────────────────────────────
# CONFIG (Cloud 전용 — API 기능 없음)
# ─────────────────────────────────────────
WORKSPACE  = os.path.dirname(os.path.abspath(__file__))
PORT       = int(os.environ.get('PORT', 5555))
IS_CLOUD   = os.environ.get('RENDER') or os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('FLY_APP_NAME')
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), 'quiz_uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_MB = 80

# ─────────────────────────────────────────
# PDF Extraction
# ─────────────────────────────────────────
import subprocess, base64

def _pip_install(pkg):
    ret = subprocess.call([sys.executable, "-m", "pip", "install", pkg, "-q"],
                          stderr=subprocess.DEVNULL)
    if ret != 0:
        subprocess.call([sys.executable, "-m", "pip", "install", pkg, "-q",
                         "--break-system-packages"])

try:
    import pdfplumber
except ImportError:
    print("📦 Installing pdfplumber...")
    _pip_install("pdfplumber")
    import pdfplumber

# PyMuPDF for page-to-image rendering (exhibit display)
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    print("📦 Installing PyMuPDF (for exhibit images)...")
    _pip_install("pymupdf")
    try:
        import fitz
        HAS_FITZ = True
    except ImportError:
        HAS_FITZ = False
        print("⚠️  PyMuPDF 설치 실패 - Exhibit 이미지 표시 불가")

class _LRUCache:
    """Thread-safe LRU cache backed by OrderedDict."""
    def __init__(self, maxsize=200):
        self._cache = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def __contains__(self, key):
        with self._lock:
            return key in self._cache

    def __getitem__(self, key):
        with self._lock:
            self._cache.move_to_end(key)
            return self._cache[key]

    def __setitem__(self, key, val):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = val
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)


# page image cache: {(pdf_path, page_num): base64_str}
image_cache = _LRUCache(maxsize=200)

# Korean explanation cache: {q_num: korean_str}
# Pre-load from korean_cache.json if it exists (bundled explanations)
korean_cache = {}
_CACHE_FILE = os.path.join(WORKSPACE, 'korean_cache.json')
if os.path.isfile(_CACHE_FILE):
    try:
        with open(_CACHE_FILE, encoding='utf-8') as _f:
            korean_cache.update(json.load(_f))
        print(f"  📖 Korean cache loaded: {len(korean_cache)} entries from korean_cache.json")
    except Exception as _e:
        print(f"  ⚠️  Failed to load korean_cache.json: {_e}")

# Cloud 버전: API 기능 없음 — korean_cache.json 만 사용


def _cache_key(pdf_name, q_num):
    """Compound cache key to avoid collisions between PDFs with same question numbers."""
    return f"{pdf_name}::{q_num}"


def _extract_product_id(pdf_name):
    """PDF 파일명에서 고유 식별자 추출 (버전/공통단어 제외).
    예: 'FCSS_NST_SE-7.6 V13.35.pdf' → 'NST'
        'FCSS_EFW_AD-7.6 V12.95.pdf' → 'EFW'
    """
    # FCSS 다음에 오는 첫 번째 세그먼트가 제품 식별자
    m = re.search(r'FCSS[_\-]([A-Z]+)', pdf_name.upper())
    if m:
        return m.group(1)
    # 폴백: 숫자/버전/공통단어 제외한 3글자 이상 대문자 세그먼트
    segments = re.split(r'[\s_\-\.]+', pdf_name.upper())
    skip = {'FCSS', 'PDF', 'V'}
    for seg in segments:
        if len(seg) >= 3 and seg.isalpha() and seg not in skip:
            return seg
    return None


def _lookup_cache(pdf_name, q_num):
    """Look up cache: exact key → 제품 식별자(NST/EFW) 기반 매칭."""
    # 1) 정확한 키
    key = _cache_key(pdf_name, q_num) if pdf_name else q_num
    if key in korean_cache:
        return korean_cache[key]
    # 2) 파일명 없이 q_num만으로 검색
    if q_num in korean_cache:
        return korean_cache[q_num]
    # 3) 제품 식별자(NST/EFW 등)가 같은 캐시 항목만 검색
    if pdf_name:
        product_id = _extract_product_id(pdf_name)
        if product_id:
            for k, v in korean_cache.items():
                if k.endswith(f'::{q_num}') and product_id in k.upper():
                    return v
    return None


def generate_korean_explanation(q_num, question, explanation, options, answer=None, pdf_name=''):
    """Cloud 버전: 캐시 조회만 수행, API 호출 없음."""
    return _lookup_cache(pdf_name, q_num)


def find_pdfs():
    pdfs = []
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.lower().endswith('.pdf'):
                full = os.path.join(root, f)
                rel  = os.path.relpath(full, WORKSPACE)
                pdfs.append({'name': f, 'path': full, 'rel': rel})
    pdfs.sort(key=lambda x: x['name'])
    return pdfs


# ─────────────────────────────────────────
# OCR 오타 자동 수정 (FortiGate 시험 PDF 특화)
# ─────────────────────────────────────────
_OCR_FIXES = [
    # Fortinet 브랜드명: 'b' → 'ti' OCR 오류
    (re.compile(r'\bForbinet\b'), 'Fortinet'),   # Forbinet → Fortinet (별도 처리)
    (re.compile(r'\bForb(Gate|OS|AP|Switch|Analyzer|Manager|SIEM|Client|Sandbox|Cloud|Guard|Token|Proxy|Web|Mail|View|DDo\w*)'), r'Forti\1'),
    # 소문자 l ↔ 대문자 I 혼동 (가장 흔한 OCR 오류)
    (re.compile(r'\blPsec\b'),    'IPsec'),
    (re.compile(r'\blPv6\b'),     'IPv6'),
    (re.compile(r'\blPv4\b'),     'IPv4'),
    (re.compile(r'\blP\b'),       'IP'),
    (re.compile(r'\blKEv2\b'),    'IKEv2'),
    (re.compile(r'\blKEv1\b'),    'IKEv1'),
    (re.compile(r'\blKE\b'),      'IKE'),
    (re.compile(r'\blD\b'),       'ID'),
    (re.compile(r'\blDs\b'),      'IDs'),
    (re.compile(r'\blSP\b'),      'ISP'),
    (re.compile(r'\bSSl\b'),      'SSL'),
    (re.compile(r'\blnterface'),  'Interface'),
    (re.compile(r'\blnternet'),   'Internet'),
    (re.compile(r'\blnternal'),   'Internal'),
    (re.compile(r'\blnbound'),    'Inbound'),
    (re.compile(r'\blncoming'),   'Incoming'),
    (re.compile(r'\bldentif'),    'Identif'),   # Identify/Identification/Identity
    (re.compile(r'\bldP\b'),      'IdP'),
    # CLI 소문자 컨텍스트: 'lp' → 'ip'
    (re.compile(r'\blp proto\b'), 'ip proto'),
    (re.compile(r'\blp addr'),    'ip addr'),
    # 기타 일반 오류
    (re.compile(r'\bdiagnase\b', re.IGNORECASE), 'diagnose'),
    (re.compile(r'\bauthenflcation\b', re.IGNORECASE), 'authentication'),
    (re.compile(r'\bauthentlcation\b', re.IGNORECASE), 'authentication'),
    (re.compile(r'\bcertlflcate\b', re.IGNORECASE), 'certificate'),
    (re.compile(r'\bpollcy\b', re.IGNORECASE), 'policy'),
]

def fix_ocr_text(text):
    """PDF 텍스트 추출 후 자동 OCR 오타 수정 (FortiGate/네트워크 용어 특화)."""
    for pattern, replacement in _OCR_FIXES:
        text = pattern.sub(replacement, text)
    return text


def _extract_text_fitz(pdf_path):
    """PyMuPDF로 텍스트 추출 (pdfplumber 대비 더 정확한 경우가 많음)."""
    if not HAS_FITZ:
        return None, {}
    full_text = ''
    page_map  = {}
    try:
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                text = page.get_text('text')
                if not text:
                    continue
                lines = [l for l in text.split('\n')
                         if 'IT Certification Guaranteed' not in l]
                page_text = '\n'.join(lines)
                for m in re.finditer(r'NO\.(\d+)', page_text):
                    page_map[f'NO.{m.group(1)}'] = page_num
                full_text += page_text + '\n'
        return full_text, page_map
    except Exception as e:
        print(f"  ⚠️  PyMuPDF 추출 실패: {e}")
        return None, {}


def extract_questions_from_pdf(pdf_path):
    full_text = ''
    page_map  = {}

    # PyMuPDF로 먼저 시도 (더 정확한 경우가 많음)
    if HAS_FITZ:
        fitz_text, fitz_map = _extract_text_fitz(pdf_path)
        if fitz_text and fitz_text.strip():
            full_text = fitz_text
            page_map  = fitz_map

    # PyMuPDF 실패 또는 미설치 시 pdfplumber로 폴백
    if not full_text.strip():
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text(x_tolerance=2, y_tolerance=2)
                if not text:
                    continue
                lines = [l for l in text.split('\n')
                         if 'IT Certification Guaranteed' not in l]
                page_text = '\n'.join(lines)
                for m in re.finditer(r'NO\.(\d+)', page_text):
                    page_map[f'NO.{m.group(1)}'] = page_num
                full_text += page_text + '\n'

    # OCR 오타 자동 수정 적용
    full_text = fix_ocr_text(full_text)

    parts = re.split(r'(NO\.\d+)', full_text)
    questions = []
    i = 1
    while i < len(parts):
        if re.match(r'NO\.\d+', parts[i]):
            num     = parts[i]
            content = parts[i+1] if i+1 < len(parts) else ''
            q = parse_question(num, content.strip(), page_map.get(num, 0))
            if q:
                questions.append(q)
            i += 2
        else:
            i += 1

    return questions


def parse_question(num, content, page_num):
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    # Remove standalone page numbers
    lines = [l for l in lines if not re.match(r'^\d{1,3}$', l)]

    # Find Answer line
    answer_idx = None
    answer     = []
    for i, line in enumerate(lines):
        m = re.match(r'^Answer:\s*([A-E][A-E\s]*)', line, re.IGNORECASE)
        if m:
            answer_idx = i
            answer = sorted(list(set(re.findall(r'[A-E]', m.group(1).upper()))))
            break

    if answer_idx is None or not answer:
        return None

    # Parse question text / options / explanation
    question_lines    = []
    options           = {}
    explanation_lines = []
    current_opt       = None
    state             = 'question'

    for i, line in enumerate(lines):
        if i == answer_idx:
            state = 'explanation'
            continue
        if state == 'explanation':
            explanation_lines.append(line)
            continue

        # Match options: "A. text" OR "A." alone (when option text is in an exhibit image)
        opt_m = re.match(r'^([A-E])\.\s*(.*)', line)
        if opt_m and (state == 'options' or re.match(r'^[A-E]\.', line)):
            state       = 'options'
            current_opt = opt_m.group(1)
            options[current_opt] = opt_m.group(2).strip() or '[옵션 텍스트가 Exhibit 이미지에 포함됨]'
        elif state == 'options' and current_opt and not re.match(r'^[A-E]\.', line):
            if options[current_opt] == '[옵션 텍스트가 Exhibit 이미지에 포함됨]':
                options[current_opt] = line
            else:
                options[current_opt] += ' ' + line
        elif state == 'question':
            question_lines.append(line)

    if not question_lines or not options:
        return None

    q_text      = re.sub(r'\s+', ' ', ' '.join(question_lines)).strip()
    explanation = re.sub(r'\s+', ' ', ' '.join(explanation_lines)).strip()
    if len(explanation) > 2500:
        explanation = explanation[:2500] + '...'

    # Exhibit 감지: 문제 or 설명에 "exhibit" 포함, 또는 출력 참조 패턴("from the output" 등)
    has_exhibit = bool(
        re.search(r'\bexhibits?\b', q_text, re.IGNORECASE) or
        re.search(r'\bexhibits?\b', explanation, re.IGNORECASE) or
        re.search(r'(?:from|refer to|shown in|based on)\s+the\s+(?:output|following|diagram|topology|table)', q_text, re.IGNORECASE)
    )

    # How many to choose
    cm = re.search(r'choose\s+(\w+)', q_text, re.IGNORECASE)
    num_to_choose = len(answer)

    return {
        'num':          num,
        'question':     q_text,
        'options':      options,
        'answer':       answer,
        'explanation':  explanation,
        'has_exhibit':  has_exhibit,
        'page_num':     page_num,
        'num_to_choose': num_to_choose,
        'is_multiple':  num_to_choose > 1,
    }


# ─────────────────────────────────────────
# Page Image Rendering
# ─────────────────────────────────────────
def render_page_base64(pdf_path, page_num, question_num=None, dpi=150):
    """Extract the exhibit image for a specific question from a PDF page.

    Strategy:
      1. Look for an embedded raster image on the page — extract the
         largest one that isn't a tiny icon or a wide watermark banner.
      2. If no raster image is found (vector/text-based exhibit), render
         the page as a bitmap but crop precisely to the exhibit region:
           - bottom edge  = just above the "NO.XX" question-number text
           - top edge     = just below the "Answer:" line of the previous
                            question (or page header if not found)
      3. Last resort: full-page render.
    """
    key = (pdf_path, page_num, question_num)
    if key in image_cache:
        return image_cache[key]
    if not HAS_FITZ or page_num <= 0:
        return None
    doc = None
    try:
        doc  = fitz.open(pdf_path)
        page = doc[page_num - 1]

        # ── 1. Try to extract the best embedded image ──────────────────
        # Layout: question text → exhibit image (below) → answer options
        # When question_num is given:
        #   a) find q_y (Y of "NO.XX" text)
        #   b) find next_q_y (Y of the NEXT "NO." on the page, or end-of-page)
        #   c) prefer the image whose top (y0) falls between q_y and next_q_y
        #      (the exhibit sits right below the question text)
        #   d) if no such image exists, fall back to adjacent-page check
        # Without question_num: use the largest image on the page.
        img_list = page.get_images(full=True)

        # Pre-compute q_y and next_q_y for position-based selection
        q_y_for_img    = None
        next_q_y_for_img = None
        is_second_on_page = False   # True if another NO.XX appears BEFORE this Q on same page
        if question_num:
            q_hits_img = page.search_for(question_num)
            if q_hits_img:
                q_y_for_img = q_hits_img[0].y0
                pr_tmp = page.rect
                next_q_y_for_img = pr_tmp.y1  # default: end of page
                # Find the next "NO.\d+" on this page that comes AFTER q_y
                for m in page.search_for("NO."):
                    if m.y0 > q_y_for_img + 20:   # skip the question itself
                        if m.y0 < next_q_y_for_img:
                            next_q_y_for_img = m.y0
                # Detect if this is the second (or later) question on this page
                # by checking if any NO.XX appears BEFORE our question position
                for m in page.search_for("NO."):
                    if m.y0 < q_y_for_img - 20:
                        is_second_on_page = True
                        break

        best_xref    = None
        best_area    = 0
        # For position-based selection: largest image in [q_y, next_q_y]
        pos_xref      = None
        pos_best_area = 0

        for img in img_list:
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []

            if rects:
                r    = rects[0]
                area = r.width * r.height
                # Skip tiny images (bullets, icons …)
                if area < 8000:
                    continue
                # Skip very wide/flat banners (watermarks, page-wide headers)
                if r.width > 0 and (r.height / r.width) < 0.05:
                    continue
                # Position-based: image top must be between q_y and next_q_y
                if q_y_for_img is not None and next_q_y_for_img is not None:
                    if q_y_for_img - 10 <= r.y0 <= next_q_y_for_img:
                        if area > pos_best_area:
                            pos_best_area = area
                            pos_xref      = xref
            else:
                # No rect info — use raw byte size as a proxy
                try:
                    raw = doc.extract_image(xref)
                    area = len(raw['image'])
                    if area < 5000:
                        continue
                except Exception:
                    continue

            if area > best_area:
                best_area = area
                best_xref = xref

        # Use position-based selection when available; otherwise fall back
        # to the largest image on the page
        chosen_xref = pos_xref if pos_xref is not None else best_xref

        # If no image found in [q_y, next_q_y], signal to check adjacent pages
        no_image_in_range = (q_y_for_img is not None and pos_xref is None)

        # ── Dual-exhibit detection ────────────────────────────────────
        # Some questions have exhibit1 ABOVE the question number and
        # exhibit2 BELOW it (e.g. "Refer to the exhibits").
        # Collect any content images that sit between min_top and q_y-10.
        _pr_tmp2  = page.rect
        _min_top2 = _pr_tmp2.y0 + _pr_tmp2.height * 0.10   # skip header watermark

        # Find y-position of any "Answer:" text between _min_top2 and q_y.
        # Images BELOW the last Answer: line (but above q_y) belong to the
        # PREVIOUS question's options — not to this question's exhibit.
        _answer_barrier = _min_top2
        if q_y_for_img is not None:
            for _ah in page.search_for("Answer:"):
                if _min_top2 < _ah.y0 < q_y_for_img:
                    _answer_barrier = max(_answer_barrier, _ah.y1)

        above_xrefs = []
        if pos_xref is not None and q_y_for_img is not None:
            for img in img_list:
                xref2 = img[0]
                if xref2 == pos_xref:
                    continue
                try:
                    rects2 = page.get_image_rects(xref2)
                except Exception:
                    rects2 = []
                if rects2:
                    r2 = rects2[0]
                    a2 = r2.width * r2.height
                    if a2 >= 8000 and (r2.height / max(r2.width, 1)) >= 0.05:
                        # Must be above q_y but BELOW any Answer: line from
                        # the previous question (to avoid picking up prev Q options)
                        if _answer_barrier < r2.y0 < q_y_for_img - 10:
                            above_xrefs.append((xref2, r2.y0))

        if chosen_xref is not None and not no_image_in_range:
            # If there are images both above AND below the question text,
            # stitch them together vertically (dual-exhibit layout).
            if above_xrefs and pos_xref is not None:
                try:
                    from PIL import Image as PILImage
                    import io as _io
                    def _xref_to_pil(xref_val):
                        raw2 = doc.extract_image(xref_val)
                        d2 = raw2['image']
                        e2 = raw2.get('ext', 'jpeg').lower()
                        if e2 not in ('jpeg', 'jpg'):
                            px2 = fitz.Pixmap(doc, xref_val)
                            if px2.alpha:
                                px2 = fitz.Pixmap(fitz.csRGB, px2)
                            d2 = px2.tobytes('jpeg')
                        return PILImage.open(_io.BytesIO(d2)).convert('RGB')
                    # Build list: above images (sorted by y) then below image
                    ordered = sorted(above_xrefs, key=lambda x: x[1])
                    pil_imgs = [_xref_to_pil(x[0]) for x in ordered]
                    pil_imgs.append(_xref_to_pil(pos_xref))
                    max_w = max(im.width for im in pil_imgs)
                    gap = 8
                    total_h = sum(im.height for im in pil_imgs) + gap * (len(pil_imgs) - 1)
                    combined = PILImage.new('RGB', (max_w, total_h), (255, 255, 255))
                    y_off = 0
                    for im in pil_imgs:
                        if im.width != max_w:
                            scale = max_w / im.width
                            im = im.resize((max_w, int(im.height * scale)), PILImage.LANCZOS)
                        combined.paste(im, (0, y_off))
                        y_off += im.height + gap
                    buf = _io.BytesIO()
                    combined.save(buf, format='JPEG', quality=85)
                    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                    image_cache[key] = b64
                    return b64
                except Exception:
                    pass  # fall through to single-image path

            best_xref = chosen_xref  # reuse downstream code
            raw_img  = doc.extract_image(best_xref)
            img_data = raw_img['image']
            ext      = raw_img.get('ext', 'jpeg').lower()
            # Convert non-JPEG (png, jb2, …) to JPEG via pixmap
            if ext not in ('jpeg', 'jpg'):
                pix = fitz.Pixmap(doc, best_xref)
                if pix.alpha:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_data = pix.tobytes('jpeg')
            b64 = base64.b64encode(img_data).decode('utf-8')
            image_cache[key] = b64
            return b64

        # ── 2. No embedded raster image → smart vector crop ──────────
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pr  = page.rect
        MIN_TOP = pr.y0 + pr.height * 0.10   # skip header watermark

        def last_text_bottom(pg, from_y, to_y):
            """Return the bottom Y of the last text block that overlaps [from_y, to_y].
            Handles the case where a single giant block spans the whole previous
            question — we cap its bottom at to_y so the result stays in range.
            """
            last = from_y
            for blk in pg.get_text("blocks"):
                bx0, by0, bx1, by1, btext, _, btype = blk
                # Block must overlap the range: starts before to_y AND ends after from_y
                if btype == 0 and by0 < to_y and by1 > from_y:
                    last = max(last, min(by1, to_y))
            return last

        pix        = None
        crop_top   = MIN_TOP
        crop_bottom = pr.y1 - pr.height * 0.08

        if question_num:
            q_hits = page.search_for(question_num)
            if q_hits:
                q_y = q_hits[0].y0
                crop_bottom = q_y - 6

                # ── Find where previous question's content ENDS ──────
                # Priority:
                #   1. "Reference:" / "References:" — most reliable end-marker
                #   2. Any http/https URL line (citation)
                #   3. "Answer:" + everything that follows it
                marker_y  = MIN_TOP
                found_ref = False

                for term in ("References:", "Reference:"):
                    for hit in page.search_for(term):
                        if MIN_TOP < hit.y0 < q_y - 10 and hit.y0 > marker_y:
                            # Scan every text block that comes AFTER this
                            # "Reference:" header (citations may follow)
                            candidate = last_text_bottom(page, hit.y0, q_y - 10)
                            if candidate > marker_y:
                                marker_y  = candidate
                                found_ref = True

                if not found_ref:
                    # No Reference: found — use Answer: and skip all text after
                    for hit in page.search_for("Answer:"):
                        if MIN_TOP < hit.y0 < q_y - 10 and hit.y0 > marker_y:
                            candidate = last_text_bottom(page, hit.y0, q_y - 10)
                            if candidate > marker_y:
                                marker_y = candidate

                crop_top = marker_y + 8

        # ── 2b. If exhibit area is too thin, check adjacent pages ────
        # Exhibit may be on the previous page (common) or the next page
        # (when the question number sits at the very bottom of a page and
        # the exhibit + options continue on the next page).
        # Also force adjacent-page search when the question had NO image
        # in its own range [q_y, next_q_y] — meaning its exhibit is on a
        # different page entirely (e.g. question at page bottom).
        exhibit_height = 0 if no_image_in_range else (crop_bottom - crop_top)

        def _extract_best_image_from_page(pg, max_y=None):
            """Try to pull the largest suitable embedded image from pg.

            max_y: if set, only consider images whose top (y0) is BELOW max_y.
                   Used when checking the NEXT page to avoid picking images that
                   belong to a LATER question on that page.
                   Pass the Y position of the first question header on that page.
            """
            best_xref2, best_area2 = None, 0
            for img2 in pg.get_images(full=True):
                xref2 = img2[0]
                try:
                    rects2 = pg.get_image_rects(xref2)
                except Exception:
                    rects2 = []
                if rects2:
                    r2   = rects2[0]
                    a2   = r2.width * r2.height
                    rat2 = r2.height / max(r2.width, 1)
                    if a2 > 8000 and rat2 >= 0.08:
                        # When max_y is set, skip images that start at or after
                        # the first question on the page (they belong to that Q)
                        if max_y is not None and r2.y0 >= max_y:
                            continue
                        if a2 > best_area2:
                            best_area2 = a2
                            best_xref2 = xref2
            if best_xref2 is None:
                return None
            try:
                raw2  = doc.extract_image(best_xref2)
                data2 = raw2['image']
                ext2  = raw2.get('ext', 'jpeg').lower()
                if ext2 not in ('jpeg', 'jpg'):
                    px2 = fitz.Pixmap(doc, best_xref2)
                    if px2.alpha:
                        px2 = fitz.Pixmap(fitz.csRGB, px2)
                    data2 = px2.tobytes('jpeg')
                return data2
            except Exception:
                return None

        def _first_question_y_on_page(pg):
            """Return the Y position of the first 'NO.\\d+' text on pg, or None."""
            first_y = None
            for hit in pg.search_for("NO."):
                # Verify it is actually 'NO.<digits>' not just 'NO.' in body text
                # by checking a small area to the right contains digits
                y = hit.y0
                if first_y is None or y < first_y:
                    first_y = y
            return first_y

        def _page_has_questions(pg):
            """Return True if the page contains any NO.\\d+ question headers."""
            text = pg.get_text("text")
            return bool(re.search(r'NO\.\d+', text))

        if exhibit_height < 50:
            total_pages = doc.page_count
            candidates = []

            # Determine adjacent page characteristics
            prev_pg_idx = page_num - 2   # 0-indexed
            next_pg_idx = page_num       # 0-indexed (= page_num+1, 1-indexed)
            prev_has_qs = _page_has_questions(doc[prev_pg_idx]) if prev_pg_idx >= 0 else True
            next_has_qs = _page_has_questions(doc[next_pg_idx]) if next_pg_idx < doc.page_count else True

            if no_image_in_range:
                # Preferred order depends on whether this is the 2nd question on the page.
                # When a page has [NO.A, NO.B]:
                #   - NO.A's exhibit is on the PREV page (dedicated exhibit page)
                #   - NO.B's exhibit is on the NEXT page (not the prev which belongs to NO.A)
                # So if is_second_on_page: check NEXT first.
                # Otherwise: check PREV first if it has no questions (dedicated exhibit page).
                if is_second_on_page:
                    # This question's exhibit is on the NEXT page
                    if next_pg_idx < total_pages:
                        candidates.append(('next', next_pg_idx))
                    if prev_pg_idx >= 0:
                        candidates.append(('prev', prev_pg_idx))
                else:
                    # Original logic: PREV-first if prev is a dedicated exhibit page
                    if not prev_has_qs and prev_pg_idx >= 0:
                        candidates.append(('prev', prev_pg_idx))
                    if next_pg_idx < total_pages:
                        candidates.append(('next', next_pg_idx))
                    if prev_has_qs and prev_pg_idx >= 0:
                        candidates.append(('prev', prev_pg_idx))
            else:
                if page_num > 1:
                    candidates.append(('prev', prev_pg_idx))
                if page_num < total_pages:
                    candidates.append(('next', next_pg_idx))

            # ── Pass 1: try embedded images from adjacent pages ──
            # When checking NEXT page: pass the Y of its first question header
            # so we only pick images that appear BEFORE that question starts.
            # (Images appearing AFTER a question on the next page belong to that Q.)
            for direction, pg_idx in candidates:
                adj_pg = doc[pg_idx]
                if direction == 'next' and _page_has_questions(adj_pg):
                    # Only accept images that precede the first question on this page
                    max_y = _first_question_y_on_page(adj_pg)
                    img_data = _extract_best_image_from_page(adj_pg, max_y=max_y)
                else:
                    img_data = _extract_best_image_from_page(adj_pg)
                if img_data:
                    b64 = base64.b64encode(img_data).decode('utf-8')
                    image_cache[key] = b64
                    return b64

            # ── Pass 2: fallback vector render of adjacent page portion ──
            for direction, pg_idx in candidates:
                adj_page = doc[pg_idx]
                adj_pr   = adj_page.rect
                try:
                    if direction == 'prev':
                        clip = fitz.Rect(adj_pr.x0,
                                         adj_pr.y0 + adj_pr.height * 0.35,
                                         adj_pr.x1,
                                         adj_pr.y1 - adj_pr.height * 0.05)
                    else:
                        # Exhibit near top of next page
                        clip = fitz.Rect(adj_pr.x0,
                                         adj_pr.y0 + adj_pr.height * 0.05,
                                         adj_pr.x1,
                                         adj_pr.y0 + adj_pr.height * 0.40)
                    pix = adj_page.get_pixmap(matrix=mat, alpha=False, clip=clip)
                    img_bytes = pix.tobytes('jpeg')
                    b64 = base64.b64encode(img_bytes).decode('utf-8')
                    image_cache[key] = b64
                    return b64
                except Exception:
                    continue

        if pix is None:
            try:
                clip = fitz.Rect(pr.x0, crop_top, pr.x1, crop_bottom)
                pix  = page.get_pixmap(matrix=mat, alpha=False, clip=clip)
            except Exception as clip_err:
                print(f"  ⚠️  Clip render failed (p{page_num}): {clip_err}")

        # ── 3. Last resort: full-page render ─────────────────────────
        if pix is None:
            try:
                pix = page.get_pixmap(matrix=mat, alpha=False)
            except Exception as full_err:
                print(f"  ⚠️  Full page render failed (p{page_num}): {full_err}")
                return None

        img_bytes = pix.tobytes('jpeg')
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        image_cache[key] = b64
        return b64

    except Exception as e:
        print(f"  ⚠️  Exhibit render failed (p{page_num}): {e}")
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


options_area_cache = _LRUCache(maxsize=100)

def render_options_area_base64(pdf_path, page_num, question_num=None, dpi=150):
    """Render the answer-options area for questions whose options are images.

    When A/B/C/D option texts are embedded as images (not extractable as text),
    we render the portion of the adjacent/current page that shows those images.

    Strategy:
      1. The main exhibit is typically extracted from the NEXT page (page_num+1)
         when the question sits at the bottom of page_num.
      2. On that next page, after the main exhibit image, come the option images.
      3. We render from (bottom of largest image on next page) to
         (just before "Answer:" or 90% of page height) as a vector crop.
    """
    key = (pdf_path, page_num, question_num, 'opts')
    if key in options_area_cache:
        return options_area_cache[key]
    if not HAS_FITZ or page_num <= 0:
        return None
    doc = None
    try:
        doc  = fitz.open(pdf_path)
        mat  = fitz.Matrix(dpi / 72, dpi / 72)

        # Determine which page holds the options
        # Typically: options are on the NEXT page (page_num, 0-indexed)
        opts_pg_idx = page_num  # next page (0-indexed = page_num)
        if opts_pg_idx >= doc.page_count:
            # Fall back to current page
            opts_pg_idx = page_num - 1

        opts_page = doc[opts_pg_idx]
        pr = opts_page.rect

        # Find bottom of the MAIN (largest) embedded image on opts_page.
        # Option images are smaller; the main exhibit is the largest one.
        img_list = opts_page.get_images(full=True)
        main_img_bottom = pr.y0 + pr.height * 0.05   # default: near top
        largest_area    = 0

        for img in img_list:
            xref = img[0]
            try:
                rects = opts_page.get_image_rects(xref)
            except Exception:
                rects = []
            if rects:
                r    = rects[0]
                area = r.width * r.height
                if area < 8000:
                    continue
                if r.width > 0 and (r.height / r.width) < 0.05:
                    continue
                # Track only the LARGEST image's bottom (= main exhibit)
                if area > largest_area:
                    largest_area    = area
                    main_img_bottom = r.y1

        # Options area: from just below the main image to before "Answer:"
        crop_top    = main_img_bottom + 4
        crop_bottom = pr.y1 - pr.height * 0.08  # default: before footer

        # Narrow to above "Answer:" if present
        for hit in opts_page.search_for("Answer:"):
            if hit.y0 > crop_top and hit.y0 < crop_bottom:
                crop_bottom = hit.y0 - 4
                break

        # Also stop before the next "NO." question header
        for hit in opts_page.search_for("NO."):
            if hit.y0 > crop_top + 10 and hit.y0 < crop_bottom:
                crop_bottom = hit.y0 - 4
                break

        if crop_bottom - crop_top < 20:
            return None

        try:
            clip    = fitz.Rect(pr.x0, crop_top, pr.x1, crop_bottom)
            pix     = opts_page.get_pixmap(matrix=mat, alpha=False, clip=clip)
            img_bytes = pix.tobytes('jpeg')
        except Exception as e:
            print(f"  ⚠️  Options area render failed (p{opts_pg_idx+1}): {e}")
            return None

        b64 = base64.b64encode(img_bytes).decode('utf-8')
        options_area_cache[key] = b64
        return b64

    except Exception as e:
        print(f"  ⚠️  render_options_area_base64 failed: {e}")
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ─────────────────────────────────────────
# Multipart Parser (no cgi module needed)
# ─────────────────────────────────────────
def parse_multipart(content_type, body):
    """Extract (filename, bytes) from a multipart/form-data body."""
    m = re.search(r'boundary=([^\s;]+)', content_type)
    if not m:
        return None, None
    boundary = m.group(1).strip('"').encode()

    # Split on --boundary
    delimiter = b'--' + boundary
    parts = body.split(delimiter)

    for part in parts[1:]:
        if part.strip() in (b'', b'--', b'--\r\n'):
            continue
        # Separate headers from body (blank line)
        sep = b'\r\n\r\n' if b'\r\n\r\n' in part else b'\n\n'
        if sep not in part:
            continue
        raw_headers, part_body = part.split(sep, 1)
        # Strip trailing CRLF added by boundary
        part_body = re.sub(rb'\r?\n$', b'', part_body)

        headers_str = raw_headers.decode('utf-8', errors='replace')
        if 'filename=' not in headers_str:
            continue  # not a file field

        fn_m = re.search(r'filename="([^"]*)"', headers_str)
        if not fn_m:
            fn_m = re.search(r"filename=([^\s;]+)", headers_str)
        filename = fn_m.group(1) if fn_m else 'upload.bin'
        return filename, part_body

    return None, None


# ─────────────────────────────────────────
# HTTP Server
# ─────────────────────────────────────────
question_cache = {}

class QuizHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default log spam

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == '/':
            self.send_html(build_html())

        elif path == '/api/pdfs':
            self.send_json(find_pdfs())

        elif path == '/api/quiz':
            pdf_path = params.get('path', [''])[0]
            count    = int(params.get('count', ['30'])[0])

            if not pdf_path or not os.path.exists(pdf_path):
                self.send_json({'error': 'PDF not found'}, 404)
                return

            if pdf_path not in question_cache:
                print(f"  ⏳ Extracting: {os.path.basename(pdf_path)}")
                question_cache[pdf_path] = extract_questions_from_pdf(pdf_path)
                print(f"  ✅ {len(question_cache[pdf_path])} questions extracted")

            all_q = question_cache[pdf_path]
            if not all_q:
                self.send_json({'error': 'No questions found in PDF'}, 400)
                return

            pdf_name = os.path.basename(pdf_path)
            selected = random.sample(all_q, min(count, len(all_q)))

            # Cloud 버전: 캐시에서 즉시 조회 (API 생성 없음)
            for q in selected:
                q['explanation_ko'] = _lookup_cache(pdf_name, q['num'])
                q['pdf_name'] = pdf_name
            self.send_json({'questions': selected, 'total': len(all_q),
                            'has_fitz': HAS_FITZ})

        elif path == '/api/exhibit':
            pdf_path    = params.get('path', [''])[0]
            page_num    = int(params.get('page', ['0'])[0])
            question_num = params.get('q', [''])[0] or None  # e.g. "NO.84"
            opts_mode   = params.get('opts', ['0'])[0] == '1'

            if not pdf_path or not os.path.exists(pdf_path) or page_num <= 0:
                self.send_json({'error': 'invalid params'}, 400)
                return

            if opts_mode:
                b64 = render_options_area_base64(pdf_path, page_num, question_num)
            else:
                b64 = render_page_base64(pdf_path, page_num, question_num)
            if b64:
                self.send_json({'image': b64})
            else:
                self.send_json({'error': 'render failed or PyMuPDF not available'}, 500)

        else:
            self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == '/api/upload':
            content_type   = self.headers.get('Content-Type', '')
            content_length = int(self.headers.get('Content-Length', 0))

            if content_length > MAX_UPLOAD_MB * 1024 * 1024:
                self.rfile.read(content_length)
                self.send_json({'error': f'파일 크기 제한 {MAX_UPLOAD_MB}MB 초과'}, 400)
                return

            body = self.rfile.read(content_length)
            filename, filedata = parse_multipart(content_type, body)

            if not filename or filedata is None:
                self.send_json({'error': '파일을 읽을 수 없습니다'}, 400)
                return
            if not filename.lower().endswith('.pdf'):
                self.send_json({'error': 'PDF 파일만 업로드 가능합니다'}, 400)
                return

            uid       = uuid.uuid4().hex[:10]
            safe_name = re.sub(r'[^\w\-_.]', '_', os.path.basename(filename))
            dest      = os.path.join(UPLOAD_DIR, f'{uid}_{safe_name}')

            with open(dest, 'wb') as f:
                f.write(filedata)

            print(f'  📤 Uploaded: {safe_name} → {dest}')
            self.send_json({'path': dest, 'name': safe_name})

        else:
            self.send_json({'error': 'Not found'}, 404)


# ─────────────────────────────────────────
# HTML Builder
# ─────────────────────────────────────────
def build_html():
    return r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📚 Cert Exam Quiz</title>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
::-webkit-scrollbar{width:7px}
::-webkit-scrollbar-track{background:#1e293b}
::-webkit-scrollbar-thumb{background:#475569;border-radius:4px}
.container{max-width:820px;margin:0 auto;padding:24px 16px 60px}
.card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:28px;margin-bottom:16px}
.btn{padding:10px 22px;border-radius:10px;border:none;cursor:pointer;font-weight:600;font-size:14px;transition:all .2s}
.btn-primary{background:#3b82f6;color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-primary:disabled{background:#475569;cursor:not-allowed;opacity:.7}
.btn-secondary{background:#334155;color:#e2e8f0}
.btn-secondary:hover{background:#475569}
.btn-success{background:#22c55e;color:#fff}
.btn-success:hover{background:#16a34a}
.btn-danger{background:#ef4444;color:#fff}
.btn-danger:hover{background:#dc2626}
.prog-bar{height:6px;background:#334155;border-radius:3px;overflow:hidden;margin-bottom:20px}
.prog-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);transition:width .3s}
.opt{display:block;width:100%;text-align:left;padding:13px 16px;margin-bottom:9px;border-radius:10px;border:2px solid #334155;background:#0f172a;color:#e2e8f0;cursor:pointer;transition:all .15s;font-size:14px;line-height:1.6}
.opt:hover{border-color:#3b82f6;background:#1e3a5f}
.opt.sel{border-color:#3b82f6;background:#1e3a5f}
.opt.correct{border-color:#22c55e!important;background:#14532d!important}
.opt.wrong{border-color:#ef4444!important;background:#450a0a!important}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
.b-blue{background:#1e3a5f;color:#60a5fa}
.b-green{background:#14532d;color:#86efac}
.b-red{background:#450a0a;color:#fca5a5}
.b-yellow{background:#451a03;color:#fcd34d}
.b-gray{background:#334155;color:#94a3b8}
h1{font-size:26px;font-weight:700}
h2{font-size:20px;font-weight:700}
h3{font-size:16px;font-weight:600}
.muted{color:#94a3b8}
.sm{font-size:13px}
select{background:#0f172a;border:2px solid #334155;color:#e2e8f0;padding:10px 14px;border-radius:10px;font-size:14px;width:100%;cursor:pointer;outline:none}
select:focus{border-color:#3b82f6}
.divider{height:1px;background:#334155;margin:18px 0}
.expl{background:#0f172a;border:1px solid #334155;border-left:3px solid #3b82f6;border-radius:8px;padding:14px;margin-top:10px;font-size:13px;line-height:1.8;color:#cbd5e1}
.q-nav-btn{width:34px;height:34px;border-radius:7px;border:none;cursor:pointer;font-size:11px;font-weight:700;transition:all .15s}
.exhibit-warn{background:#451a03;border:1px solid #92400e;border-radius:8px;padding:9px 13px;margin-bottom:14px;font-size:13px;color:#fcd34d}
.key-banner{background:#1e1a00;border:1px solid #854d0e;border-radius:12px;padding:16px 20px;margin-bottom:16px}
.key-input{background:#0f172a;border:2px solid #334155;color:#e2e8f0;padding:9px 13px;border-radius:8px;font-size:13px;width:100%;font-family:monospace;outline:none}
.key-input:focus{border-color:#f59e0b}
.korean-expl h1,.korean-expl h2,.korean-expl h3{color:#4ade80;margin:12px 0 6px;font-size:14px}
.korean-expl p{margin-bottom:8px}
.korean-expl ul,.korean-expl ol{padding-left:20px;margin-bottom:8px}
.korean-expl li{margin-bottom:3px}
.korean-expl code{background:#0a1f10;border:1px solid #166534;border-radius:4px;padding:1px 5px;font-family:monospace;font-size:13px;color:#86efac}
.korean-expl pre{background:#0a1f10;border:1px solid #166534;border-radius:6px;padding:10px 12px;margin:8px 0;overflow-x:auto}
.korean-expl pre code{background:none;border:none;padding:0;font-size:13px;line-height:1.6}
.korean-expl table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
.korean-expl th{background:#14532d;color:#86efac;padding:6px 10px;border:1px solid #166534;text-align:left}
.korean-expl td{padding:5px 10px;border:1px solid #166534;color:#d1fae5}
.korean-expl tr:nth-child(even) td{background:#0a1a0e}
.korean-expl strong{color:#86efac;font-weight:600}
.korean-expl blockquote{border-left:3px solid #166534;padding-left:12px;color:#a7f3d0;margin:8px 0}
</style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const { useState, useEffect, useRef, useCallback, useMemo } = React;

/* ── ExhibitImage: lazy-loads PDF page image from server ── */
function ExhibitImage({ pdfPath, pageNum, qNum, optsMode }) {
  const [src,     setSrc]     = useState(null);
  const [loading, setLoading] = useState(true);
  const [err,     setErr]     = useState(false);

  useEffect(() => {
    if (!pdfPath || !pageNum) return;
    setLoading(true); setSrc(null); setErr(false);
    const url = '/api/exhibit?path=' + encodeURIComponent(pdfPath)
              + '&page=' + pageNum
              + (qNum ? '&q=' + encodeURIComponent(qNum) : '')
              + (optsMode ? '&opts=1' : '');
    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.image) setSrc('data:image/jpeg;base64,' + data.image);
        else setErr(true);
      })
      .catch(() => setErr(true))
      .finally(() => setLoading(false));
  }, [pdfPath, pageNum, qNum, optsMode]);

  const [zoomed, setZoomed] = useState(false);

  if (loading) return (
    <div style={{textAlign:'center',padding:'16px',color:'#94a3b8',fontSize:'13px',
      background:'#0f172a',borderRadius:'8px',marginBottom:'16px',border:'1px solid #334155'}}>
      ⏳ Exhibit 로딩 중...
    </div>
  );
  if (err && optsMode) return null;  // opts exhibit is optional — hide silently
  if (err) return (
    <div style={{textAlign:'center',padding:'10px',color:'#f59e0b',fontSize:'13px',
      background:'#451a03',borderRadius:'8px',marginBottom:'16px'}}>
      ⚠️ Exhibit 이미지를 불러올 수 없습니다.
    </div>
  );

  const label = optsMode ? '📋 선택지' : '📊 Exhibit';
  const borderColor = optsMode ? '#64748b' : '#475569';

  return (
    <>
      {/* 인라인 표시 */}
      <div style={{
        marginBottom:'16px', borderRadius:'8px', border:`1px solid ${borderColor}`,
        overflow:'hidden', position:'relative',
      }}>
        <div style={{
          background:'#0f172a', padding:'5px 10px',
          display:'flex', justifyContent:'space-between', alignItems:'center',
          borderBottom:'1px solid #334155',
        }}>
          <span style={{fontSize:'11px',color:optsMode?'#94a3b8':'#60a5fa',fontWeight:'600'}}>{label}</span>
          <button onClick={()=>setZoomed(true)}
            style={{fontSize:'11px',background:'none',border:'none',color:'#94a3b8',
              cursor:'pointer',padding:'2px 6px'}}>
            🔍 크게 보기
          </button>
        </div>
        <div style={{background:'#000'}}>
          <img src={src} alt={optsMode?'선택지':'Exhibit'} style={{width:'100%',display:'block'}} />
        </div>
      </div>

      {/* 전체화면 모달 */}
      {zoomed && (
        <div onClick={()=>setZoomed(false)} style={{
          position:'fixed', inset:0, background:'rgba(0,0,0,0.92)',
          display:'flex', alignItems:'center', justifyContent:'center',
          zIndex:9999, cursor:'zoom-out', padding:'20px',
        }}>
          <img src={src} alt="Exhibit"
            style={{maxWidth:'100%', maxHeight:'100%', borderRadius:'8px',
              boxShadow:'0 0 40px rgba(0,0,0,0.8)'}} />
          <div style={{position:'absolute',top:'16px',right:'20px',
            color:'#94a3b8',fontSize:'13px'}}>✕ 클릭하여 닫기</div>
        </div>
      )}
    </>
  );
}

/* ── KoreanExplain: 캐시에서 즉시 표시 (Cloud 버전 — API 없음) ── */
function KoreanExplain({ question }) {
  const korean = question.explanation_ko || '';
  if (!korean) return null;

  const html = typeof marked !== 'undefined'
    ? marked.parse(korean)
    : korean.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\n/g,'<br/>');

  return (
    <div style={{marginTop:'12px',background:'#0f2a1a',border:'1px solid #166534',
      borderRadius:'8px',padding:'14px'}}>
      <p style={{fontWeight:'600',fontSize:'13px',color:'#4ade80',marginBottom:'10px'}}>
        🇰🇷 한국어 해석
      </p>
      <div
        dangerouslySetInnerHTML={{__html: html}}
        style={{fontSize:'14px',lineHeight:'1.85',color:'#d1fae5'}}
        className="korean-expl"
      />
    </div>
  );
}

/* ── Helpers ── */
function fmt(n){ return n < 10 ? '0'+n : ''+n; }
function useTimer(){
  const [secs, setSecs] = useState(0);
  const ref = useRef(null);
  useEffect(()=>{ ref.current = setInterval(()=>setSecs(s=>s+1),1000); return ()=>clearInterval(ref.current); },[]);
  return `${fmt(Math.floor(secs/60))}:${fmt(secs%60)}`;
}

/* ── SelectScreen ── */
function SelectScreen({ onStart }){
  const [tab,      setTab]     = useState('upload'); // 'server' | 'upload'
  // server tab
  const [pdfs,    setPdfs]    = useState([]);
  const [sel,     setSel]     = useState('');
  // upload tab
  const [file,    setFile]    = useState(null);
  const [uploading,setUploading]=useState(false);
  const [uploaded, setUploaded]=useState(null); // {path, name}
  // common
  const [count,   setCount]   = useState(30);
  const [loading, setLoading] = useState(false);
  const [err,     setErr]     = useState('');
  const fileRef = useRef(null);

  useEffect(()=>{
    fetch('/api/pdfs').then(r=>r.json()).then(data=>{
      setPdfs(data);
      if(data.length>0) setSel(data[0].path);
    }).catch(()=>{});
  },[]);

  /* upload file to server */
  const doUpload = async(f)=>{
    if(!f) return;
    setUploading(true); setErr(''); setUploaded(null);
    try{
      const fd = new FormData();
      fd.append('file', f);
      const res  = await fetch('/api/upload', {method:'POST', body:fd});
      const data = await res.json();
      if(data.error) throw new Error(data.error);
      setUploaded(data);
    }catch(e){ setErr('업로드 오류: '+e.message); }
    finally{ setUploading(false); }
  };

  const handleFileChange = e=>{
    const f = e.target.files[0];
    if(f){ setFile(f); doUpload(f); }
  };

  /* start quiz (시험 모드) */
  const start = async(mode='exam')=>{
    const pdfPath = tab==='server' ? sel : (uploaded && uploaded.path);
    if(!pdfPath) return;
    setLoading(true); setErr('');
    try{
      // 연습 모드는 전체 문제, 시험 모드는 count개
      const url = mode==='practice'
        ? '/api/quiz?path='+encodeURIComponent(pdfPath)+'&count=9999'
        : '/api/quiz?path='+encodeURIComponent(pdfPath)+'&count='+count;
      const res  = await fetch(url);
      const data = await res.json();
      if(data.error) throw new Error(data.error);
      onStart(data.questions, data.total, pdfPath, mode);
    }catch(e){ setErr('오류: '+e.message); }
    finally{ setLoading(false); }
  };

  const canStart = tab==='server' ? !!sel : !!uploaded;

  const tabStyle = active => ({
    flex:1, padding:'10px', border:'none', cursor:'pointer', fontWeight:'600',
    fontSize:'14px', borderRadius:'8px', transition:'all .2s',
    background: active ? '#3b82f6' : 'transparent',
    color:      active ? '#fff'    : '#94a3b8',
  });

  return(
    <div className="container">
      <div style={{textAlign:'center',padding:'44px 0 28px'}}>
        <div style={{fontSize:'52px',marginBottom:'12px'}}>📚</div>
        <h1>Certification Exam Quiz</h1>
        <p className="muted" style={{marginTop:'8px'}}>덤프 PDF에서 랜덤 문제를 뽑아 모의고사를 풀어보세요</p>
      </div>

      <div className="card">
        <h3 style={{marginBottom:'16px'}}>⚙️ 시험 설정</h3>

        {/* Tab switcher */}
        <div style={{display:'flex',gap:'6px',background:'#0f172a',borderRadius:'10px',
          padding:'4px',marginBottom:'20px'}}>
          <button style={tabStyle(tab==='server')} onClick={()=>{setTab('server');setErr('');}}>
            📁 서버 PDF
          </button>
          <button style={tabStyle(tab==='upload')} onClick={()=>{setTab('upload');setErr('');}}>
            💻 내 PC에서 업로드
          </button>
        </div>

        {/* Server PDF tab */}
        {tab==='server' && (
          pdfs.length > 0 ? <>
            <label style={{display:'block',marginBottom:'7px',fontWeight:'600',fontSize:'13px',color:'#94a3b8'}}>
              📄 PDF 파일 선택
            </label>
            <select value={sel} onChange={e=>setSel(e.target.value)} style={{marginBottom:'18px'}}>
              {pdfs.map(p=><option key={p.path} value={p.path}>{p.rel}</option>)}
            </select>
          </> : (
            <div style={{textAlign:'center',padding:'20px 0',color:'#94a3b8',fontSize:'13px',marginBottom:'8px'}}>
              서버에 PDF 파일이 없습니다. "내 PC에서 업로드" 탭을 이용해 주세요.
            </div>
          )
        )}

        {/* Upload tab */}
        {tab==='upload' && (
          <div style={{marginBottom:'18px'}}>
            <input ref={fileRef} type="file" accept=".pdf"
              onChange={handleFileChange}
              style={{display:'none'}} />

            {!uploaded ? (
              <div
                onClick={()=>fileRef.current.click()}
                style={{
                  border:'2px dashed #475569',borderRadius:'12px',padding:'32px',
                  textAlign:'center',cursor:'pointer',transition:'border-color .2s',
                }}
                onMouseOver={e=>e.currentTarget.style.borderColor='#3b82f6'}
                onMouseOut={e=>e.currentTarget.style.borderColor='#475569'}
              >
                {uploading ? (
                  <div>
                    <div style={{fontSize:'32px',marginBottom:'8px'}}>⏳</div>
                    <p style={{color:'#94a3b8',fontSize:'14px'}}>업로드 중...</p>
                  </div>
                ) : (
                  <div>
                    <div style={{fontSize:'36px',marginBottom:'10px'}}>📤</div>
                    <p style={{fontWeight:'600',marginBottom:'4px'}}>클릭하여 PDF 선택</p>
                    <p style={{color:'#94a3b8',fontSize:'13px'}}>최대 80MB · .pdf 파일만 지원</p>
                  </div>
                )}
              </div>
            ) : (
              <div style={{background:'#0f172a',borderRadius:'10px',padding:'14px 16px',
                display:'flex',alignItems:'center',gap:'12px'}}>
                <span style={{fontSize:'28px'}}>📄</span>
                <div style={{flex:1,overflow:'hidden'}}>
                  <p style={{fontWeight:'600',fontSize:'14px',
                    overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                    {uploaded.name}
                  </p>
                  <p style={{color:'#22c55e',fontSize:'12px',marginTop:'2px'}}>✅ 업로드 완료</p>
                </div>
                <button className="btn btn-secondary" onClick={()=>{
                  setUploaded(null); setFile(null);
                  if(fileRef.current) fileRef.current.value='';
                }} style={{fontSize:'12px',padding:'6px 10px',flexShrink:0}}>
                  다시 선택
                </button>
              </div>
            )}
          </div>
        )}

        {/* Count slider — always visible */}
        <label style={{display:'block',marginBottom:'7px',fontWeight:'600',fontSize:'13px',color:'#94a3b8'}}>
          📝 문제 수: <span style={{color:'#3b82f6',fontWeight:'700'}}>{count}문제</span>
        </label>
        <input type="range" min="10" max="50" step="5" value={count}
          onChange={e=>setCount(+e.target.value)}
          style={{width:'100%',marginBottom:'22px',accentColor:'#3b82f6'}} />

        {err && <p style={{color:'#ef4444',marginBottom:'12px',fontSize:'14px'}}>{err}</p>}

        <div style={{display:'flex',gap:'10px',marginBottom:'10px'}}>
          <button className="btn btn-primary" onClick={()=>start('exam')}
            disabled={loading || !canStart || (tab==='upload' && uploading)}
            style={{flex:1,padding:'13px',fontSize:'15px'}}>
            {loading ? '⏳ 불러오는 중...' : '🚀 시험 모드'}
          </button>
          <button className="btn" onClick={()=>start('practice')}
            disabled={loading || !canStart || (tab==='upload' && uploading)}
            style={{flex:1,padding:'13px',fontSize:'15px',background:'#7c3aed',borderColor:'#7c3aed'}}>
            {loading ? '⏳ 불러오는 중...' : '🎯 연습 모드'}
          </button>
        </div>
      </div>

      <p className="muted sm" style={{textAlign:'center'}}>
        {tab==='server'
          ? `${pdfs.length}개 서버 PDF 발견 · 시험: ${count}문제 랜덤 출제 / 연습: 전체 문제`
          : '업로드한 PDF에서 문제를 출제합니다'}
      </p>
    </div>
  );
}

/* ── PracticeScreen ── */
function PracticeScreen({ questions, onExit, pdfPath }){
  // pool: 아직 맞추지 못한 문제 인덱스 배열 (틀리면 유지, 맞으면 제거)
  const [pool,      setPool]      = useState(()=>questions.map((_,i)=>i));
  const [done,      setDone]      = useState(new Set());
  const [curIdx,    setCurIdx]    = useState(()=>Math.floor(Math.random()*questions.length));
  const [selected,  setSelected]  = useState([]);
  const [submitted, setSubmitted] = useState(false);
  const [complete,  setComplete]  = useState(false);
  const [streak,    setStreak]    = useState(0);
  const [stats,     setStats]     = useState({correct:0, wrong:0});

  const q = questions[curIdx];

  const isCorrect = submitted &&
    JSON.stringify([...selected].sort()) === JSON.stringify([...q.answer].sort());

  // 선택지 셔플 (QuizScreen과 동일 로직)
  const {opts, origToDisplay} = useMemo(()=>{
    const labels = ['A','B','C','D','E'];
    const origArr = Object.keys(q.options).sort();
    let seed = (parseInt(q.num.replace(/\D/g,''),10)*1234567 + curIdx*9999) >>> 0;
    const rand = ()=>{ seed=(seed*1664525+1013904223)&0xffffffff; return (seed>>>0)/0xffffffff; };
    const shuffled = [...origArr];
    for(let i=shuffled.length-1;i>0;i--){
      const j=Math.floor(rand()*(i+1));
      [shuffled[i],shuffled[j]]=[shuffled[j],shuffled[i]];
    }
    const opts = shuffled.map((orig,i)=>({displayLetter:labels[i], origLetter:orig, text:q.options[orig]}));
    const origToDisplay={};
    opts.forEach(o=>{origToDisplay[o.origLetter]=o.displayLetter;});
    return {opts, origToDisplay};
  }, [q.num]);

  const toggle = origLetter => {
    if(submitted) return;
    if(q.is_multiple){
      setSelected(prev=>prev.includes(origLetter)?prev.filter(x=>x!==origLetter):[...prev,origLetter]);
    } else {
      setSelected(prev=>prev.includes(origLetter)?[]:[origLetter]);
    }
  };

  const submit = ()=>{
    if(selected.length===0) return;
    const correct = JSON.stringify([...selected].sort())===JSON.stringify([...q.answer].sort());
    setSubmitted(true);
    if(correct){
      setStreak(s=>s+1);
      setStats(s=>({...s, correct:s.correct+1}));
      setDone(prev=>new Set([...prev, q.num]));
    } else {
      setStreak(0);
      setStats(s=>({...s, wrong:s.wrong+1}));
    }
  };

  const next = ()=>{
    // 맞췄으면 pool에서 제거, 틀렸으면 유지
    const newPool = isCorrect ? pool.filter(i=>i!==curIdx) : pool;
    if(newPool.length===0){ setPool([]); setComplete(true); return; }
    setPool(newPool);
    const nextIdx = newPool[Math.floor(Math.random()*newPool.length)];
    setCurIdx(nextIdx);
    setSelected([]);
    setSubmitted(false);
  };

  const total     = questions.length;
  const doneCount = done.size;
  const remaining = pool.length - (isCorrect&&!complete ? 1 : 0); // 맞춘 후 남은 수 미리 표시

  // 완료 화면
  if(complete){
    return(
      <div className="container">
        <div className="card" style={{textAlign:'center',padding:'40px 24px'}}>
          <div style={{fontSize:'64px',marginBottom:'16px'}}>🎉</div>
          <h2 style={{marginBottom:'8px'}}>모든 문제 완료!</h2>
          <p className="muted" style={{marginBottom:'6px'}}>
            전체 <strong style={{color:'#e2e8f0'}}>{total}문제</strong>를 모두 맞혔습니다!
          </p>
          <p className="muted" style={{marginBottom:'20px'}}>
            총 시도 {stats.correct+stats.wrong}회 &nbsp;·&nbsp;
            정답 <span style={{color:'#22c55e'}}>{stats.correct}</span> &nbsp;/&nbsp;
            오답 <span style={{color:'#ef4444'}}>{stats.wrong}</span>
          </p>
          <button className="btn btn-primary" onClick={onExit}
            style={{padding:'12px 32px',fontSize:'15px'}}>
            홈으로
          </button>
        </div>
      </div>
    );
  }

  return(
    <div className="container">
      {/* 상단 바 */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:'14px'}}>
        <button onClick={onExit}
          style={{background:'none',border:'1px solid #4b5563',color:'#9ca3af',
            borderRadius:'6px',padding:'5px 12px',cursor:'pointer',fontSize:'13px'}}>
          ← 홈
        </button>
        <div style={{display:'flex',gap:'8px',alignItems:'center',flexWrap:'wrap'}}>
          {streak>=2 &&
            <span className="badge" style={{background:'#7c3aed22',color:'#a78bfa',border:'1px solid #7c3aed'}}>
              🔥 {streak}연속 정답
            </span>}
          <span className="badge" style={{background:'#14532d',color:'#86efac',border:'1px solid #22c55e'}}>
            ✅ {doneCount}/{total}
          </span>
          <span className="badge b-gray sm">남은 {pool.length}문제</span>
        </div>
      </div>

      {/* 진행 바 (초록) */}
      <div className="prog-bar" style={{marginBottom:'16px'}}>
        <div className="prog-fill"
          style={{width:`${(doneCount/total)*100}%`, background:'#22c55e', transition:'width .4s'}} />
      </div>

      {/* 문제 카드 */}
      <div className="card">
        <div style={{display:'flex',gap:'8px',alignItems:'center',marginBottom:'12px',flexWrap:'wrap'}}>
          <span className="badge b-blue">{q.num}</span>
          <span className="badge" style={{background:'#7c3aed22',color:'#a78bfa',border:'1px solid #7c3aed',fontSize:'11px'}}>
            🎯 연습 모드
          </span>
          {q.is_multiple && <span className="badge b-blue">복수 선택 ({q.num_to_choose}개)</span>}
          {q.has_exhibit  && <span className="badge b-yellow">📊 Exhibit</span>}
        </div>

        {q.has_exhibit && q.page_num>0 &&
          <ExhibitImage pdfPath={pdfPath} pageNum={q.page_num} qNum={q.num} />}
        {q.has_exhibit && q.page_num<=0 &&
          <div className="exhibit-warn">
            ⚠️ 이 문제는 <strong>Exhibit(그림/출력)</strong>을 참조하지만 페이지 정보를 찾을 수 없습니다.
          </div>}

        <p style={{fontSize:'15px',lineHeight:'1.85',marginBottom:'22px',whiteSpace:'pre-wrap'}}>
          {q.question}
        </p>

        {/* 선택지 */}
        {opts.map(({displayLetter, origLetter, text})=>{
          let style = {width:'100%',textAlign:'left',marginBottom:'8px'};
          let cls   = 'opt';
          if(submitted){
            const isAns = q.answer.includes(origLetter);
            const isSel = selected.includes(origLetter);
            if(isAns)          { style={...style,background:'#14532d',border:'2px solid #22c55e',color:'#86efac'}; }
            else if(isSel)     { style={...style,background:'#450a0a',border:'2px solid #ef4444',color:'#fca5a5'}; }
          } else {
            if(selected.includes(origLetter)) cls='opt sel';
          }
          return(
            <button key={displayLetter} className={cls} style={style}
              onClick={()=>toggle(origLetter)} disabled={submitted}>
              <span style={{fontWeight:'700',marginRight:'10px',color:'#64748b'}}>{displayLetter}.</span>
              {text}
            </button>
          );
        })}

        {/* 확인 버튼 */}
        {!submitted ? (
          <button className="btn btn-primary" onClick={submit}
            disabled={selected.length===0}
            style={{marginTop:'12px',width:'100%',
              opacity:selected.length===0?0.4:1,padding:'12px',fontSize:'15px'}}>
            확인
          </button>
        ) : (
          <div>
            {/* 정답/오답 배너 */}
            <div style={{
              padding:'12px 16px',borderRadius:'8px',marginTop:'12px',
              background: isCorrect?'#14532d':'#450a0a',
              border:`1px solid ${isCorrect?'#22c55e':'#ef4444'}`,
              color: isCorrect?'#86efac':'#fca5a5',
              fontWeight:'600',fontSize:'15px'}}>
              {isCorrect
                ? '✅ 정답! 이 문제는 제외됩니다.'
                : `❌ 오답! 정답: ${q.answer.map(a=>origToDisplay[a]||a).join(', ')}`}
              {!isCorrect &&
                <p style={{fontSize:'12px',marginTop:'4px',fontWeight:'400',opacity:0.85}}>
                  이 문제는 나중에 다시 나옵니다
                </p>}
            </div>

            {/* 해설 */}
            {q.explanation &&
              <div style={{marginTop:'12px',background:'#1e293b',border:'1px solid #334155',
                borderRadius:'8px',padding:'14px'}}>
                <p style={{fontWeight:'600',fontSize:'13px',color:'#94a3b8',marginBottom:'8px'}}>💡 해설</p>
                <p style={{fontSize:'13px',lineHeight:'1.8',color:'#cbd5e1'}}>{q.explanation}</p>
              </div>}

            {/* 한국어 해석 */}
            <KoreanExplain question={{...q, explanation_ko: q.explanation_ko}} />

            <button className="btn btn-primary" onClick={next}
              style={{marginTop:'14px',width:'100%',padding:'12px',fontSize:'15px'}}>
              {pool.filter(i=>i!==curIdx).length===0 && isCorrect ? '완료 🎉' : '다음 문제 →'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── QuizScreen ── */
function QuizScreen({ questions, onFinish, onExit, pdfPath }){
  const [idx,     setIdx]     = useState(0);
  const [answers, setAnswers] = useState({});
  const timer = useTimer();

  const q      = questions[idx];
  const total  = questions.length;
  const userAns = answers[q.num] || [];
  const ansCount = Object.keys(answers).length;

  // answers는 항상 origLetter 기준으로 저장 (정답 비교용)
  const toggle = displayLetter => {
    const opt = opts.find(o => o.displayLetter === displayLetter);
    if(!opt) return;
    const origLetter = opt.origLetter;
    const cur = answers[q.num] || [];
    let next;
    if(q.is_multiple){
      next = cur.includes(origLetter) ? cur.filter(x=>x!==origLetter) : [...cur, origLetter];
    } else {
      next = cur.includes(origLetter) ? [] : [origLetter];
    }
    setAnswers(prev=>({...prev, [q.num]: next.sort()}));
  };

  const submit = ()=>{
    const unanswered = total - ansCount;
    if(unanswered > 0){
      if(!window.confirm(`아직 ${unanswered}문제에 답하지 않았습니다. 제출하시겠습니까?`)) return;
    }
    onFinish(answers, timer);
  };

  // 문제 번호 기반 시드로 선택지 순서 셔플 + 라벨 재배정
  // opts: [{displayLetter:'A', origLetter:'C', text:'...'}, ...]
  // origToDisplay: {C:'A', A:'B', ...} — 정답 체크용 역매핑
  const {opts, origToDisplay} = useMemo(() => {
    const labels = ['A','B','C','D','E'];
    const origArr = Object.keys(q.options).sort();
    let seed = (parseInt(q.num.replace(/\D/g,''),10) * 1234567 + idx * 9999) >>> 0;
    const rand = () => { seed = (seed * 1664525 + 1013904223) & 0xffffffff; return (seed >>> 0) / 0xffffffff; };
    const shuffled = [...origArr];
    for (let i = shuffled.length - 1; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    const opts = shuffled.map((origLetter, i) => ({
      displayLetter: labels[i],
      origLetter,
      text: q.options[origLetter]
    }));
    const origToDisplay = {};
    opts.forEach(o => { origToDisplay[o.origLetter] = o.displayLetter; });
    return {opts, origToDisplay};
  }, [q.num]);

  return(
    <div className="container">
      {/* top bar */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:'14px'}}>
        <div style={{display:'flex',gap:'8px',alignItems:'center'}}>
          <button onClick={()=>{if(window.confirm('홈으로 돌아가면 진행 중인 시험이 초기화됩니다. 나가시겠습니까?')) onExit();}}
            style={{background:'none',border:'1px solid #e2e8f0',borderRadius:'8px',padding:'4px 10px',cursor:'pointer',fontSize:'13px',color:'#64748b'}}>
            ← 홈
          </button>
          <span className="badge b-blue">{q.num}</span>
          <span className="muted sm">{idx+1} / {total}</span>
        </div>
        <div style={{display:'flex',gap:'12px',alignItems:'center'}}>
          <span className="muted sm">⏱ {timer}</span>
          <span className="muted sm">✅ {ansCount}/{total}</span>
        </div>
      </div>

      <div className="prog-bar">
        <div className="prog-fill" style={{width:`${(idx/total)*100}%`}} />
      </div>

      {/* question */}
      <div className="card">
        {q.has_exhibit && q.page_num > 0 &&
          <ExhibitImage pdfPath={pdfPath} pageNum={q.page_num} qNum={q.num} />
        }
        {q.has_exhibit && q.page_num <= 0 &&
          <div className="exhibit-warn">
            ⚠️ 이 문제는 <strong>Exhibit(그림/출력)</strong>을 참조하지만 페이지 정보를 찾을 수 없습니다.
          </div>
        }
        <div style={{marginBottom:'10px',display:'flex',gap:'8px',flexWrap:'wrap'}}>
          {q.is_multiple &&
            <span className="badge b-blue">복수 선택 ({q.num_to_choose}개 선택)</span>}
        </div>
        <p style={{fontSize:'15px',lineHeight:'1.85',marginBottom:'22px',whiteSpace:'pre-wrap'}}>
          {q.question}
        </p>

        {/* When ALL options are image-placeholders, load a "options area" exhibit */}
        {q.page_num > 0 && opts.length > 0 &&
          opts.every(o => o.text === '[옵션 텍스트가 Exhibit 이미지에 포함됨]') && (
          <ExhibitImage pdfPath={pdfPath} pageNum={q.page_num} qNum={q.num} optsMode={true} />
        )}

        {opts.map(({displayLetter, origLetter, text})=>(
          <button key={displayLetter} className={'opt'+(userAns.includes(origLetter)?' sel':'')}
            onClick={()=>toggle(displayLetter)}>
            <span style={{fontWeight:'700',marginRight:'10px',
              color:userAns.includes(origLetter)?'#60a5fa':'#64748b'}}>
              {displayLetter}.
            </span>
            {text === '[옵션 텍스트가 Exhibit 이미지에 포함됨]'
              ? <span style={{color:'#94a3b8',fontStyle:'italic',fontSize:'13px'}}>
                  (위 이미지 참조)
                </span>
              : text
            }
          </button>
        ))}
      </div>

      {/* navigation */}
      <div style={{display:'flex',gap:'10px',justifyContent:'space-between',marginBottom:'16px'}}>
        <button className="btn btn-secondary" onClick={()=>setIdx(i=>i-1)} disabled={idx===0}>
          ← 이전
        </button>
        <div style={{display:'flex',gap:'8px'}}>
          {idx===total-1
            ? <button className="btn btn-success" onClick={submit}>✅ 제출하기</button>
            : <button className="btn btn-primary"  onClick={()=>setIdx(i=>i+1)}>다음 →</button>
          }
        </div>
      </div>

      {/* mini map */}
      <div className="card" style={{padding:'18px'}}>
        <p className="muted sm" style={{marginBottom:'10px'}}>문제 목록 (클릭하여 이동)</p>
        <div style={{display:'flex',flexWrap:'wrap',gap:'5px'}}>
          {questions.map((q2,i)=>{
            const done = !!answers[q2.num];
            const cur  = i===idx;
            return(
              <button key={i} className="q-nav-btn" onClick={()=>setIdx(i)}
                style={{
                  background: cur?'#3b82f6': done?'#14532d':'#334155',
                  color:      cur?'#fff':    done?'#86efac':'#94a3b8',
                }}>
                {i+1}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ── ResultsScreen ── */
function ResultsScreen({ questions, answers, elapsed, onRetry, pdfPath }){
  const [expanded,        setExpanded]        = useState(null);
  const [expandedCorrect, setExpandedCorrect] = useState(null);

  const results = questions.map(q=>{
    const userAns = answers[q.num] || [];
    const correct = JSON.stringify([...userAns].sort()) === JSON.stringify([...q.answer].sort());
    return {...q, userAns, correct};
  });

  const correctN = results.filter(r=>r.correct).length;
  const total    = questions.length;
  const score    = Math.round(correctN/total*100);
  const passed   = score >= 75;
  const wrongR   = results.filter(r=>!r.correct);
  const sc       = score>=80?'#22c55e':score>=75?'#f59e0b':'#ef4444';


  return(
    <div className="container">

      {/* score */}
      <div className="card" style={{textAlign:'center'}}>
        <div style={{width:'110px',height:'110px',borderRadius:'50%',
          background:sc+'22',border:`4px solid ${sc}`,color:sc,
          display:'flex',alignItems:'center',justifyContent:'center',
          margin:'0 auto 16px',fontSize:'30px',fontWeight:'700'}}>
          {score}%
        </div>
        <h2 style={{marginBottom:'6px'}}>{passed?'🎉 합격!':'😅 불합격'}</h2>
        <p className="muted">
          {total}문제 중 <strong style={{color:'#e2e8f0'}}>{correctN}문제</strong> 정답 /
          오답 <strong style={{color:'#fca5a5'}}>{wrongR.length}문제</strong>
        </p>
        <div style={{marginTop:'10px'}}>
          <span className="badge sm" style={{
            background:passed?'#14532d':'#450a0a',
            color:passed?'#86efac':'#fca5a5',
            fontSize:'13px',padding:'5px 14px'}}>
            {passed?'✅ PASS':'❌ FAIL'} (기준 75%)
          </span>
          <span className="badge b-gray sm" style={{marginLeft:'8px',fontSize:'13px',padding:'5px 12px'}}>
            ⏱ {elapsed}
          </span>
        </div>
      </div>

      {/* stats */}
      <div className="card">
        <div style={{display:'flex',gap:'0',justifyContent:'space-around',textAlign:'center'}}>
          {[
            {v:correctN,  l:'정답',  c:'#22c55e'},
            {v:wrongR.length, l:'오답', c:'#ef4444'},
            {v:results.filter(r=>r.userAns.length===0).length, l:'미답변', c:'#f59e0b'},
          ].map(s=>(
            <div key={s.l}>
              <div style={{fontSize:'26px',fontWeight:'700',color:s.c}}>{s.v}</div>
              <div className="muted sm">{s.l}</div>
            </div>
          ))}
        </div>
      </div>

      {/* wrong answers */}
      {wrongR.length > 0 && <>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',margin:'22px 0 10px'}}>
          <h3 style={{color:'#fca5a5'}}>❌ 틀린 문제 ({wrongR.length}개)</h3>
        </div>
        <p className="muted sm" style={{marginBottom:'14px'}}>
          클릭하면 문제 해설을 확인할 수 있어요. (한국어 해석은 PDF 로딩 후 백그라운드에서 자동 생성됩니다)
        </p>

        {wrongR.map((r,i)=>(
          <div key={i} className="card" style={{marginBottom:'10px'}}>
            <div style={{cursor:'pointer',display:'flex',justifyContent:'space-between',alignItems:'flex-start'}}
              onClick={()=>setExpanded(expanded===r.num?null:r.num)}>
              <div style={{flex:1}}>
                <div style={{display:'flex',gap:'6px',alignItems:'center',marginBottom:'8px',flexWrap:'wrap'}}>
                  <span className="badge b-red">{r.num}</span>
                  {r.has_exhibit && <span className="badge b-yellow">📊 Exhibit</span>}
                  {r.is_multiple && <span className="badge b-blue">복수선택</span>}
                </div>
                <p style={{fontSize:'13px',lineHeight:'1.6',color:'#cbd5e1'}}>
                  {r.question.length>160 ? r.question.slice(0,160)+'...' : r.question}
                </p>
                <div style={{marginTop:'10px',display:'flex',gap:'14px',fontSize:'12px',flexWrap:'wrap'}}>
                  <span>
                    내 답: {r.userAns.length>0
                      ? r.userAns.map(a=><span key={a} className="badge b-red" style={{marginRight:'3px'}}>{a}</span>)
                      : <span className="badge b-gray">미답변</span>}
                  </span>
                  <span>
                    정답: {r.answer.map(a=><span key={a} className="badge b-green" style={{marginRight:'3px'}}>{a}</span>)}
                  </span>
                </div>
              </div>
              <span className="muted" style={{marginLeft:'10px',fontSize:'18px'}}>
                {expanded===r.num?'▲':'▼'}
              </span>
            </div>

            {expanded===r.num && <>
              <div className="divider" />
              {/* exhibit image */}
              {r.has_exhibit && r.page_num > 0 &&
                <ExhibitImage pdfPath={pdfPath} pageNum={r.page_num} qNum={r.num} />
              }
              {/* options-area exhibit when ALL options are images */}
              {r.page_num > 0 && Object.keys(r.options).length > 0 &&
                Object.values(r.options).every(v=>v==='[옵션 텍스트가 Exhibit 이미지에 포함됨]') && (
                <ExhibitImage pdfPath={pdfPath} pageNum={r.page_num} qNum={r.num} optsMode={true} />
              )}
              {/* options highlight */}
              <div style={{marginBottom:'12px'}}>
                {Object.keys(r.options).sort().map(letter=>{
                  const isCorr = r.answer.includes(letter);
                  const isUser = r.userAns.includes(letter);
                  let cls = 'opt';
                  if(isCorr)            cls+=' correct';
                  else if(isUser)       cls+=' wrong';
                  return(
                    <div key={letter} className={cls} style={{cursor:'default'}}>
                      <span style={{fontWeight:'700',marginRight:'8px'}}>
                        {letter}. {isCorr?'✅ ':isUser?'❌ ':''}
                      </span>
                      {r.options[letter]==='[옵션 텍스트가 Exhibit 이미지에 포함됨]'
                        ? <span style={{color:'#94a3b8',fontStyle:'italic',fontSize:'13px'}}>(위 이미지 참조)</span>
                        : r.options[letter]
                      }
                    </div>
                  );
                })}
              </div>
              <KoreanExplain question={r} />
            </>}
          </div>
        ))}
      </>}

      {/* correct list */}
      {correctN > 0 && <>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',margin:'22px 0 10px'}}>
          <h3 style={{color:'#86efac'}}>✅ 맞힌 문제 ({correctN}개)</h3>
        </div>
        <p className="muted sm" style={{marginBottom:'14px'}}>
          클릭하면 해설을 다시 확인할 수 있어요.
        </p>
        {results.filter(r=>r.correct).map((r,i)=>(
          <div key={i} className="card" style={{marginBottom:'10px'}}>
            <div style={{cursor:'pointer',display:'flex',justifyContent:'space-between',alignItems:'flex-start'}}
              onClick={()=>setExpandedCorrect(expandedCorrect===r.num?null:r.num)}>
              <div style={{flex:1}}>
                <div style={{display:'flex',gap:'6px',alignItems:'center',marginBottom:'8px',flexWrap:'wrap'}}>
                  <span className="badge b-green">{r.num}</span>
                  {r.has_exhibit && <span className="badge b-yellow">📊 Exhibit</span>}
                  {r.is_multiple && <span className="badge b-blue">복수선택</span>}
                </div>
                <p style={{fontSize:'13px',lineHeight:'1.6',color:'#cbd5e1'}}>
                  {r.question.length>160 ? r.question.slice(0,160)+'...' : r.question}
                </p>
                <div style={{marginTop:'10px',fontSize:'12px'}}>
                  정답: {r.answer.map(a=><span key={a} className="badge b-green" style={{marginRight:'3px'}}>{a}</span>)}
                </div>
              </div>
              <span className="muted" style={{marginLeft:'10px',fontSize:'18px'}}>
                {expandedCorrect===r.num?'▲':'▼'}
              </span>
            </div>

            {expandedCorrect===r.num && <>
              <div className="divider" />
              {r.has_exhibit && r.page_num > 0 &&
                <ExhibitImage pdfPath={pdfPath} pageNum={r.page_num} qNum={r.num} />
              }
              {r.page_num > 0 && Object.keys(r.options).length > 0 &&
                Object.values(r.options).every(v=>v==='[옵션 텍스트가 Exhibit 이미지에 포함됨]') && (
                <ExhibitImage pdfPath={pdfPath} pageNum={r.page_num} qNum={r.num} optsMode={true} />
              )}
              <div style={{marginBottom:'12px'}}>
                {Object.keys(r.options).sort().map(letter=>{
                  const isCorr = r.answer.includes(letter);
                  let cls = 'opt';
                  if(isCorr) cls+=' correct';
                  return(
                    <div key={letter} className={cls} style={{cursor:'default'}}>
                      <span style={{fontWeight:'700',marginRight:'8px'}}>
                        {letter}. {isCorr?'✅ ':''}
                      </span>
                      {r.options[letter]==='[옵션 텍스트가 Exhibit 이미지에 포함됨]'
                        ? <span style={{color:'#94a3b8',fontStyle:'italic',fontSize:'13px'}}>(위 이미지 참조)</span>
                        : r.options[letter]
                      }
                    </div>
                  );
                })}
              </div>
              <KoreanExplain question={r} />
            </>}
          </div>
        ))}
      </>}

      <button className="btn btn-primary" onClick={onRetry}
        style={{width:'100%',padding:'13px',fontSize:'15px',marginBottom:'8px'}}>
        🔄 다시 시험보기
      </button>
    </div>
  );
}

/* ── App ── */
function App(){
  const [screen,    setScreen]    = useState('select');
  const [questions, setQuestions] = useState([]);
  const [answers,   setAnswers]   = useState({});
  const [elapsed,   setElapsed]   = useState('00:00');
  const [pdfPath,   setPdfPath]   = useState('');
  const [mode,      setMode]      = useState('exam'); // 'exam' | 'practice'

  const handleStart  = (qs, total, path, m='exam') => {
    setQuestions(qs); setAnswers({}); setPdfPath(path); setMode(m);
    setScreen(m==='practice' ? 'practice' : 'quiz');
  };
  const handleFinish = (ans, t) => { setAnswers(ans); setElapsed(t); setScreen('results'); };
  const handleRetry  = ()       => setScreen('select');

  if(screen==='select')   return <SelectScreen  onStart={handleStart} />;
  if(screen==='practice') return <PracticeScreen questions={questions} onExit={handleRetry} pdfPath={pdfPath} />;
  if(screen==='quiz')     return <QuizScreen     questions={questions} onFinish={handleFinish} onExit={handleRetry} pdfPath={pdfPath} />;
  if(screen==='results')  return <ResultsScreen  questions={questions} answers={answers} elapsed={elapsed} onRetry={handleRetry} pdfPath={pdfPath} />;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
</body>
</html>"""


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def get_free_port(preferred=5555):
    try:
        s = socket.socket()
        s.bind(('', preferred))
        s.close()
        return preferred
    except OSError:
        s = socket.socket()
        s.bind(('', 0))
        p = s.getsockname()[1]
        s.close()
        return p


if __name__ == '__main__':
    port   = PORT if IS_CLOUD else get_free_port(PORT)
    server = HTTPServer(('0.0.0.0', port), QuizHandler)
    url    = f'http://localhost:{port}'

    print('\n' + '='*45)
    print('  📚 Certification Exam Quiz')
    print('='*45)
    if IS_CLOUD:
        print(f'  🌐 Running on cloud (port {port})')
    else:
        print(f'  🌐 URL  : {url}')
    print(f'  📁 Scan : {WORKSPACE}')
    print(f'  ⏹  Stop : Ctrl+C')
    print('='*45 + '\n')

    if not IS_CLOUD:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n✅ Server stopped.')
