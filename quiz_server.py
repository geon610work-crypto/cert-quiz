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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
WORKSPACE  = os.path.dirname(os.path.abspath(__file__))
PORT       = int(os.environ.get('PORT', 5555))
IS_CLOUD   = os.environ.get('RENDER') or os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('FLY_APP_NAME')
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), 'quiz_uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_MB = 80
ENV_FILE   = os.path.join(WORKSPACE, '.quiz_env')

def _load_env_file():
    """Load ANTHROPIC_API_KEY from .quiz_env file if present."""
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith('ANTHROPIC_API_KEY='):
                    key = line.split('=', 1)[1].strip().strip('"\'')
                    if key:
                        os.environ['ANTHROPIC_API_KEY'] = key
                        print(f"  🔑 API key loaded from {ENV_FILE}")
                        return

def _save_api_key(key):
    """Persist ANTHROPIC_API_KEY to .quiz_env file."""
    with open(ENV_FILE, 'w') as f:
        f.write(f'ANTHROPIC_API_KEY={key}\n')
    os.environ['ANTHROPIC_API_KEY'] = key
    print(f"  🔑 API key saved to {ENV_FILE}")

_load_env_file()

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

# page image cache: {(pdf_path, page_num): base64_str}
image_cache = {}

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

# Try to load Anthropic SDK (optional — CLI fallback is used when SDK fails)
try:
    import anthropic as _anthropic_sdk
    HAS_ANTHROPIC = True
except ImportError:
    try:
        _pip_install("anthropic")
        _pip_install("httpx[socks]")
        import anthropic as _anthropic_sdk
        HAS_ANTHROPIC = True
    except Exception:
        HAS_ANTHROPIC = False


def _build_prompt(q_num, question, explanation, options, answer):
    opts_text  = '\n'.join(
        f'{k}. {v}' for k, v in sorted(options.items())
        if v != '[옵션 텍스트가 Exhibit 이미지에 포함됨]'
    )
    answer_str = ', '.join(answer) if answer else ''
    p  = "당신은 FortiGate/네트워크 자격증 시험 전문가입니다. "
    p += "아래 시험 문제의 정답 이유를 한국어로 설명해주세요.\n\n"
    p += f"[문제 {q_num}]\n{question}\n"
    if opts_text:   p += f"\n[선택지]\n{opts_text}\n"
    if answer_str:  p += f"\n[정답] {answer_str}\n"
    if explanation and explanation.strip():
        p += f"\n[영문 해설 참고]\n{explanation}\n"
    p += (
        "\n주의사항:\n"
        "- exhibit 이미지는 없어도 됩니다. 정답 보기와 FortiGate 기술 지식만으로 설명하세요.\n"
        "- 핵심 개념과 정답 이유를 한국어 3~5문장으로 설명하세요.\n"
        "- FortiGate, OSPF, BGP, IPsec, FSSO 등 기술 용어는 영어 그대로 사용하세요.\n"
        "- 설명 텍스트만 출력하고 '해설:', '설명:' 같은 접두어는 붙이지 마세요."
    )
    return p


def _find_claude_bin():
    """Find claude CLI binary — works on macOS, Linux, and Windows."""
    IS_WIN = sys.platform == 'win32'
    home   = os.path.expanduser('~')

    # 1. Try PATH lookup (where / where.exe)
    lookup_cmd = ['where', 'claude'] if IS_WIN else ['which', 'claude']
    try:
        r = subprocess.run(lookup_cmd, capture_output=True, text=True, timeout=5)
        first = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ''
        if r.returncode == 0 and first and os.path.isfile(first):
            return first
    except Exception:
        pass

    # 2. macOS/Linux: try login shell (sources ~/.zprofile, ~/.bash_profile)
    if not IS_WIN:
        for shell in ['zsh', 'bash']:
            try:
                r2 = subprocess.run(
                    [shell, '-l', '-c', 'which claude'],
                    capture_output=True, text=True, timeout=8
                )
                path = r2.stdout.strip().splitlines()[0] if r2.stdout.strip() else ''
                if r2.returncode == 0 and path and os.path.isfile(path):
                    return path
            except Exception:
                pass

    # 3. Hardcoded candidates per platform
    if IS_WIN:
        appdata  = os.environ.get('APPDATA', '')
        localapp = os.environ.get('LOCALAPPDATA', '')
        candidates = [
            # npm global on Windows: %APPDATA%\npm\claude.cmd
            os.path.join(appdata,  'npm', 'claude.cmd'),
            os.path.join(appdata,  'npm', 'claude'),
            os.path.join(localapp, 'Programs', 'claude', 'claude.exe'),
            os.path.join(home, 'AppData', 'Roaming', 'npm', 'claude.cmd'),
            r'C:\Program Files\claude\claude.exe',
        ]
    else:
        candidates = [
            '/usr/local/bin/claude',
            '/opt/homebrew/bin/claude',
            os.path.join(home, '.local', 'bin', 'claude'),
            os.path.join(home, 'bin', 'claude'),
            '/usr/bin/claude',
            os.path.join(home, '.npm-global', 'bin', 'claude'),
            os.path.join(home, '.npm', 'bin', 'claude'),
            '/usr/local/lib/node_modules/.bin/claude',
        ]

    # Ask npm where its global bin lives (cross-platform)
    try:
        npm_cmd = ['npm.cmd', 'bin', '-g'] if IS_WIN else ['npm', 'bin', '-g']
        npm_r = subprocess.run(npm_cmd, capture_output=True, text=True, timeout=5)
        if npm_r.returncode == 0 and npm_r.stdout.strip():
            npm_bin = npm_r.stdout.strip().splitlines()[0]
            for name in ('claude.cmd', 'claude'):
                candidates.insert(0, os.path.join(npm_bin, name))
    except Exception:
        pass

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _cache_key(pdf_name, q_num):
    """Compound cache key to avoid collisions between PDFs with same question numbers."""
    return f"{pdf_name}::{q_num}"


def _lookup_cache(pdf_name, q_num):
    """Look up cache: exact key → 같은 PDF 계열(NST/EFW 등) 키워드 매칭."""
    # 1) 정확한 키
    key = _cache_key(pdf_name, q_num) if pdf_name else q_num
    if key in korean_cache:
        return korean_cache[key]
    # 2) 파일명 없이 q_num만으로 검색
    if q_num in korean_cache:
        return korean_cache[q_num]
    # 3) PDF 이름에서 핵심 식별자 추출 후 같은 계열 캐시 검색
    #    예: "FCSS_NST_SE-7.6 V13.35.pdf" → ["NST", "SE"]
    #        "FCSS_EFW_AD-7.6 V12.95.pdf" → ["EFW", "AD"]
    if pdf_name:
        keywords = [w for w in re.split(r'[\s_\-\.]+', pdf_name.upper()) if len(w) >= 2 and not w.isdigit()]
        for k, v in korean_cache.items():
            if k.endswith(f'::{q_num}'):
                k_upper = k.upper()
                if any(kw in k_upper for kw in keywords):
                    return v
    return None


def generate_korean_explanation(q_num, question, explanation, options, answer=None, pdf_name=''):
    """Generate Korean explanation via claude CLI or Anthropic SDK API key."""
    cached = _lookup_cache(pdf_name, q_num)
    if cached:
        return cached

    prompt = _build_prompt(q_num, question, explanation, options, answer or [])

    # ── Method 1: claude CLI (works when run from user's own terminal) ──
    claude_bin = _find_claude_bin()
    print(f"  [explain] claude bin: {claude_bin!r}")
    if claude_bin:
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ('CLAUDECODE', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS')}
        try:
            r = subprocess.run(
                [claude_bin, '-p', prompt, '--model', 'claude-haiku-4-5-20251001'],
                capture_output=True, text=True, timeout=60, env=env_clean
            )
            print(f"  [explain] CLI rc={r.returncode} out={r.stdout[:60]!r} err={r.stderr[:80]!r}")
            text = r.stdout.strip()
            if text and r.returncode == 0:
                korean_cache[key] = text
                return text
        except Exception as e:
            print(f"  ⚠️  claude CLI failed ({q_num}): {e}")

    # ── Method 2: Anthropic SDK with explicit API key ───────────────────
    if HAS_ANTHROPIC:
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if api_key:
            try:
                client = _anthropic_sdk.Anthropic(api_key=api_key)
                msg    = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=700,
                    messages=[{"role": "user", "content": prompt}]
                )
                result = msg.content[0].text.strip()
                korean_cache[key] = result
                return result
            except Exception as e:
                print(f"  ⚠️  SDK API call failed ({q_num}): {e}")

    # ── Method 3: requests library ───────────────────────────────────────
    try:
        import requests as _requests
        import warnings as _warnings
        _warnings.filterwarnings('ignore', message='Unverified HTTPS request')

        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            raise ValueError("no api key")

        proxies = {}
        for var in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            val = os.environ.get(var, '')
            if val:
                proxies = {'http': val, 'https': val}
                break

        resp = _requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 700,
                'messages': [{'role': 'user', 'content': prompt}]
            },
            proxies=proxies or None,
            timeout=30,
            verify=False,
        )
        if resp.status_code == 200:
            result = resp.json()['content'][0]['text'].strip()
            korean_cache[key] = result
            return result
        else:
            print(f"  ⚠️  requests API failed ({q_num}): {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  ⚠️  requests method failed ({q_num}): {e}")

    print(f"  ⚠️  All methods exhausted for {q_num}. claude={claude_bin!r} key={'set' if os.environ.get('ANTHROPIC_API_KEY') else 'unset'}")
    return None


def _bg_generate_all(questions, pdf_name=''):
    """Background thread: pre-generate Korean explanations for selected questions.
    Rate limit: Anthropic free tier allows 5 req/min → wait 13s between calls.
    """
    import time
    print(f"  🔤 Background Korean generation started for {len(questions)} questions...")
    for i, q in enumerate(questions):
        q_num = q['num']
        key   = _cache_key(pdf_name, q_num) if pdf_name else q_num
        if key not in korean_cache:
            try:
                result = generate_korean_explanation(
                    q_num, q['question'], q.get('explanation', ''),
                    q.get('options', {}), q.get('answer', []),
                    pdf_name=pdf_name,
                )
                if result:
                    print(f"  ✅ Korean OK: {q_num} ({i+1}/{len(questions)})")
                else:
                    print(f"  ⚠️  Korean failed: {q_num}")
            except Exception as e:
                print(f"  ⚠️  Korean error ({q_num}): {e}")
            if i < len(questions) - 1:
                time.sleep(13)
    print("  🔤 Background Korean generation complete.")


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


def extract_questions_from_pdf(pdf_path):
    full_text = ''
    page_map  = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if not text:
                continue
            lines = [l for l in text.split('\n')
                     if 'IT Certification Guaranteed' not in l]
            page_text = '\n'.join(lines)
            for m in re.finditer(r'NO\.(\d+)', page_text):
                page_map[f'NO.{m.group(1)}'] = page_num
            full_text += page_text + '\n'

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
    has_exhibit = bool(re.search(r'\bexhibits?\b', q_text, re.IGNORECASE))

    explanation = re.sub(r'\s+', ' ', ' '.join(explanation_lines)).strip()
    if len(explanation) > 2500:
        explanation = explanation[:2500] + '...'

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
                if r.width > 0 and (r.height / r.width) < 0.08:
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

        if chosen_xref is not None and not no_image_in_range:
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
            doc.close()
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
                # Preferred order:
                # 1. If PREV page is a dedicated exhibit page (no question headers),
                #    check it FIRST — it's almost certainly the correct exhibit.
                # 2. Otherwise check NEXT first (the exhibit continues on the next page).
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
                    doc.close()
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
                    doc.close()
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
                doc.close()
                return None

        img_bytes = pix.tobytes('jpeg')
        doc.close()
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        image_cache[key] = b64
        return b64

    except Exception as e:
        print(f"  ⚠️  Exhibit render failed (p{page_num}): {e}")
        try:
            doc.close()
        except Exception:
            pass
        return None


options_area_cache = {}

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
                if r.width > 0 and (r.height / r.width) < 0.08:
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
            doc.close()
            return None

        try:
            clip    = fitz.Rect(pr.x0, crop_top, pr.x1, crop_bottom)
            pix     = opts_page.get_pixmap(matrix=mat, alpha=False, clip=clip)
            img_bytes = pix.tobytes('jpeg')
        except Exception as e:
            print(f"  ⚠️  Options area render failed (p{opts_pg_idx+1}): {e}")
            doc.close()
            return None

        doc.close()
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        options_area_cache[key] = b64
        return b64

    except Exception as e:
        print(f"  ⚠️  render_options_area_base64 failed: {e}")
        try:
            doc.close()
        except Exception:
            pass
        return None


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

            # Start background Korean generation for ONLY the selected questions.
            # Already-cached ones are skipped instantly inside _bg_generate_all.
            t = threading.Thread(target=_bg_generate_all, args=(selected, pdf_name), daemon=True)
            t.start()

            # Attach already-cached Korean explanations (may be None if not yet ready)
            for q in selected:
                q['explanation_ko'] = _lookup_cache(pdf_name, q['num'])
                q['pdf_name'] = pdf_name  # pass to frontend for polling
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

        elif path == '/api/explain_status':
            # Poll whether background Korean generation is done for a question
            q_num    = params.get('num',  [''])[0]
            pdf_name = params.get('pdf',  [''])[0]
            cached   = _lookup_cache(pdf_name, q_num)
            if cached:
                self.send_json({'ready': True, 'korean': cached})
            else:
                self.send_json({'ready': False})

        elif path == '/api/keycheck':
            key = os.environ.get('ANTHROPIC_API_KEY', '')
            self.send_json({'has_key': bool(key)})

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

        elif path == '/api/explain':
            # Korean explanation via Claude API
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self.send_json({'error': 'no body'}, 400)
                return
            try:
                payload  = json.loads(self.rfile.read(length).decode('utf-8'))
                q_num    = payload.get('num', '')
                question = payload.get('question', '')
                expl     = payload.get('explanation', '')
                options  = payload.get('options', {})
                answer   = payload.get('answer', [])
                korean   = generate_korean_explanation(q_num, question, expl, options, answer)
                if korean:
                    self.send_json({'korean': korean})
                else:
                    self.send_json({'error': 'unavailable'}, 503)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/setkey':
            # Save ANTHROPIC_API_KEY entered from the UI
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self.send_json({'error': 'no body'}, 400)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                key = payload.get('key', '').strip()
                if not key:
                    self.send_json({'error': '키가 비어 있습니다'}, 400)
                    return
                _save_api_key(key)
                # Clear Korean cache so next quiz re-generates with new key
                korean_cache.clear()
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

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

/* ── ApiKeyBanner: shown when ANTHROPIC_API_KEY is not configured ── */
function ApiKeyBanner() {
  const [hasKey,  setHasKey]  = useState(true);   // optimistic
  const [key,     setKey]     = useState('');
  const [saving,  setSaving]  = useState(false);
  const [saved,   setSaved]   = useState(false);
  const [err,     setErr]     = useState('');

  useEffect(() => {
    fetch('/api/keycheck')
      .then(r => r.json())
      .then(d => setHasKey(d.has_key))
      .catch(() => {});
  }, []);

  if (hasKey || saved) return null;

  const save = async () => {
    if (!key.trim()) return;
    setSaving(true); setErr('');
    try {
      const res  = await fetch('/api/setkey', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: key.trim()}),
      });
      const data = await res.json();
      if (data.ok) { setSaved(true); setHasKey(true); }
      else setErr(data.error || '저장 실패');
    } catch(e) { setErr('저장 중 오류: ' + e.message); }
    finally { setSaving(false); }
  };

  return (
    <div className="key-banner">
      <p style={{fontWeight:'700',fontSize:'14px',color:'#fbbf24',marginBottom:'6px'}}>
        🔑 한국어 해석 기능 설정
      </p>
      <p style={{fontSize:'13px',color:'#d97706',marginBottom:'12px',lineHeight:'1.6'}}>
        한국어 해석을 생성하려면 Anthropic API 키가 필요합니다.{' '}
        <a href="https://console.anthropic.com/settings/keys" target="_blank"
          style={{color:'#fbbf24'}}>console.anthropic.com</a>에서 발급 후 입력하세요.
      </p>
      <div style={{display:'flex',gap:'8px'}}>
        <input
          className="key-input"
          type="password"
          placeholder="sk-ant-api03-..."
          value={key}
          onChange={e => setKey(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && save()}
        />
        <button className="btn btn-primary" onClick={save}
          disabled={saving || !key.trim()}
          style={{flexShrink:0, padding:'9px 18px', fontSize:'13px'}}>
          {saving ? '저장 중...' : '저장'}
        </button>
      </div>
      {err && <p style={{color:'#ef4444',fontSize:'12px',marginTop:'6px'}}>{err}</p>}
      <p style={{fontSize:'11px',color:'#78716c',marginTop:'8px'}}>
        키는 서버 폴더의 .quiz_env 파일에 저장됩니다.
      </p>
    </div>
  );
}

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

/* ── KoreanExplain: shows pre-generated Korean, or polls until ready ── */
function KoreanExplain({ question }) {
  const initial = question.explanation_ko || '';
  const [state,  setState]  = useState(initial ? 'done' : 'loading');
  const [korean, setKorean] = useState(initial);

  useEffect(() => {
    if (initial) return; // already have it from pre-generation
    let cancelled = false;
    let timerId   = null;

    const poll = () => {
      if (cancelled) return;
      fetch('/api/explain_status?num=' + encodeURIComponent(question.num)
           + (question.pdf_name ? '&pdf=' + encodeURIComponent(question.pdf_name) : ''))
        .then(r => r.json())
        .then(data => {
          if (cancelled) return;
          if (data.ready && data.korean) {
            setKorean(data.korean);
            setState('done');
          } else {
            // Not ready yet — retry in 4 seconds
            timerId = setTimeout(poll, 4000);
          }
        })
        .catch(() => {
          if (!cancelled) timerId = setTimeout(poll, 6000);
        });
    };

    poll();
    return () => { cancelled = true; if (timerId) clearTimeout(timerId); };
  }, [question.num]);

  if (state === 'loading') return (
    <div style={{marginTop:'12px',background:'#1e293b',border:'1px solid #334155',
      borderRadius:'8px',padding:'12px',display:'flex',alignItems:'center',gap:'8px'}}>
      <span style={{fontSize:'13px',color:'#94a3b8'}}>⏳ 한국어 해석 생성 중 (백그라운드)...</span>
    </div>
  );

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
  const [tab,      setTab]     = useState('server'); // 'server' | 'upload'
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

  /* start quiz */
  const start = async()=>{
    const pdfPath = tab==='server' ? sel : (uploaded && uploaded.path);
    if(!pdfPath) return;
    setLoading(true); setErr('');
    try{
      const res  = await fetch('/api/quiz?path='+encodeURIComponent(pdfPath)+'&count='+count);
      const data = await res.json();
      if(data.error) throw new Error(data.error);
      onStart(data.questions, data.total, pdfPath);
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

      <ApiKeyBanner />

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

        <button className="btn btn-primary" onClick={start}
          disabled={loading || !canStart || (tab==='upload' && uploading)}
          style={{width:'100%',padding:'13px',fontSize:'15px'}}>
          {loading ? '⏳ 문제 불러오는 중...' : '🚀 시험 시작'}
        </button>
      </div>

      <p className="muted sm" style={{textAlign:'center'}}>
        {tab==='server'
          ? `${pdfs.length}개 서버 PDF 발견 · 랜덤으로 ${count}문제 출제됩니다`
          : '업로드한 PDF에서 랜덤으로 문제를 출제합니다'}
      </p>
    </div>
  );
}

/* ── QuizScreen ── */
function QuizScreen({ questions, onFinish, pdfPath }){
  const [idx,     setIdx]     = useState(0);
  const [answers, setAnswers] = useState({});
  const timer = useTimer();

  const q      = questions[idx];
  const total  = questions.length;
  const userAns = answers[q.num] || [];
  const ansCount = Object.keys(answers).length;

  const toggle = letter => {
    const cur = answers[q.num] || [];
    let next;
    if(q.is_multiple){
      next = cur.includes(letter) ? cur.filter(x=>x!==letter) : [...cur, letter];
    } else {
      next = cur.includes(letter) ? [] : [letter];
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

  // 문제 번호 기반 시드로 선택지 순서 셔플 (같은 문제는 항상 같은 순서)
  const opts = useMemo(() => {
    const arr = Object.keys(q.options).sort();
    let seed = (q.num * 1234567 + idx * 9999) >>> 0;
    const rand = () => { seed = (seed * 1664525 + 1013904223) & 0xffffffff; return (seed >>> 0) / 0xffffffff; };
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }, [q.num]);

  return(
    <div className="container">
      {/* top bar */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:'14px'}}>
        <div style={{display:'flex',gap:'8px',alignItems:'center'}}>
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
          opts.every(l => q.options[l] === '[옵션 텍스트가 Exhibit 이미지에 포함됨]') && (
          <ExhibitImage pdfPath={pdfPath} pageNum={q.page_num} qNum={q.num} optsMode={true} />
        )}

        {opts.map(letter=>(
          <button key={letter} className={'opt'+(userAns.includes(letter)?' sel':'')}
            onClick={()=>toggle(letter)}>
            <span style={{fontWeight:'700',marginRight:'10px',
              color:userAns.includes(letter)?'#60a5fa':'#64748b'}}>
              {letter}.
            </span>
            {q.options[letter] === '[옵션 텍스트가 Exhibit 이미지에 포함됨]'
              ? <span style={{color:'#94a3b8',fontStyle:'italic',fontSize:'13px'}}>
                  (위 이미지 참조)
                </span>
              : q.options[letter]
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
  const [expanded, setExpanded] = useState(null);

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
        <h3 style={{margin:'22px 0 10px',color:'#86efac'}}>✅ 맞힌 문제 ({correctN}개)</h3>
        <div style={{display:'flex',flexWrap:'wrap',gap:'6px',marginBottom:'24px'}}>
          {results.filter(r=>r.correct).map(r=>(
            <span key={r.num} className="badge b-green">{r.num}</span>
          ))}
        </div>
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

  const handleStart  = (qs, total, path) => { setQuestions(qs); setAnswers({}); setPdfPath(path); setScreen('quiz'); };
  const handleFinish = (ans, t)          => { setAnswers(ans); setElapsed(t); setScreen('results'); };
  const handleRetry  = ()                => setScreen('select');

  if(screen==='select')  return <SelectScreen onStart={handleStart} />;
  if(screen==='quiz')    return <QuizScreen   questions={questions} onFinish={handleFinish} pdfPath={pdfPath} />;
  if(screen==='results') return <ResultsScreen questions={questions} answers={answers} elapsed={elapsed} onRetry={handleRetry} pdfPath={pdfPath} />;
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
