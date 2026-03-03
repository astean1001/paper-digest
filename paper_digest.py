#!/usr/bin/env python3
"""
Paper Digest - 매일 새로운 논문을 자동으로 요약해 GitHub Pages에 게시
"""

import os
import json
import sqlite3
import hashlib
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import time
import random

import arxiv
import requests
import anthropic

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
PAPERS_PER_DAY = 5          # 하루 최대 논문 수
OUTPUT_DIR = Path("docs")   # GitHub Pages는 /docs 또는 /root 사용
DB_PATH = Path("seen_papers.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 관심 분야 쿼리 목록 (arXiv + Semantic Scholar 공용)
TOPICS = [
    # Smart contract / formal verification
    "smart contract vulnerability detection",
    "formal verification solidity",
    "bytecode analysis ethereum",
    # Program repair / synthesis
    "automated program repair",
    "program synthesis equality saturation",
    "MBA deobfuscation",
    # MEV / blockchain
    "MEV maximal extractable value",
    "Solana blockchain performance",
    # Music analytics
    "music popularity prediction cross-platform",
    "music streaming analytics",
]

# Semantic Scholar 검색에 쓸 토픽 (arXiv 카테고리와 별개로 관심사 검색)
S2_TOPICS = [
    "smart contract formal verification",
    "program repair synthesis",
    "blockchain MEV",
    "music popularity metrics",
]


# ─────────────────────────────────────────────
# DB 초기화
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_papers (
            paper_id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            seen_at TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn, paper_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    return row is not None


def mark_seen(conn, paper_id: str, title: str, source: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen_papers VALUES (?, ?, ?, ?)",
        (paper_id, title, source, datetime.now().isoformat()),
    )
    conn.commit()


# ─────────────────────────────────────────────
# arXiv 검색
# ─────────────────────────────────────────────
def fetch_arxiv(conn, max_results=20) -> list[dict]:
    candidates = []
    # 최근 30일 논문 위주
    date_from = datetime.now() - timedelta(days=30)

    for topic in random.sample(TOPICS, min(4, len(TOPICS))):
        try:
            search = arxiv.Search(
                query=topic,
                max_results=max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )
            for r in search.results():
                pid = r.entry_id.split("/")[-1]  # e.g. "2401.12345v1" → "2401.12345v1"
                if not is_seen(conn, f"arxiv:{pid}"):
                    candidates.append({
                        "id": f"arxiv:{pid}",
                        "title": r.title,
                        "authors": ", ".join(a.name for a in r.authors[:3]),
                        "abstract": r.summary[:1200],
                        "url": r.entry_id,
                        "published": r.published.strftime("%Y-%m-%d") if r.published else "",
                        "source": "arXiv",
                        "topic": topic,
                    })
        except Exception as e:
            print(f"[arXiv] {topic} 검색 오류: {e}")
        time.sleep(1)

    return candidates


# ─────────────────────────────────────────────
# Semantic Scholar 검색
# ─────────────────────────────────────────────
def fetch_semantic_scholar(conn, max_results=20) -> list[dict]:
    candidates = []
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    fields = "paperId,title,authors,abstract,year,externalIds,url"

    for topic in random.sample(S2_TOPICS, min(3, len(S2_TOPICS))):
        try:
            params = {
                "query": topic,
                "limit": max_results,
                "fields": fields,
            }
            resp = requests.get(base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            for paper in data.get("data", []):
                pid = paper.get("paperId", "")
                if not pid:
                    continue
                full_id = f"s2:{pid}"
                if is_seen(conn, full_id):
                    continue
                abstract = paper.get("abstract") or ""
                if not abstract:
                    continue
                authors_list = paper.get("authors", [])
                candidates.append({
                    "id": full_id,
                    "title": paper.get("title", ""),
                    "authors": ", ".join(a["name"] for a in authors_list[:3]),
                    "abstract": abstract[:1200],
                    "url": paper.get("url") or f"https://www.semanticscholar.org/paper/{pid}",
                    "published": str(paper.get("year", "")),
                    "source": "Semantic Scholar",
                    "topic": topic,
                })
        except Exception as e:
            print(f"[S2] {topic} 검색 오류: {e}")
        time.sleep(1)

    return candidates


# ─────────────────────────────────────────────
# Claude로 요약
# ─────────────────────────────────────────────
def summarize_paper(client: anthropic.Anthropic, paper: dict) -> str:
    prompt = f"""다음 논문을 한국어로 요약해주세요. 연구자 관점에서 핵심을 파악하기 쉽게 작성해주세요.

제목: {paper['title']}
저자: {paper['authors']}
출처: {paper['source']} ({paper['published']})
관련 주제: {paper['topic']}

초록:
{paper['abstract']}

다음 형식으로 작성해주세요:
**한 줄 요약**: (핵심을 1~2문장으로)
**문제 정의**: (무엇을 해결하려 했는가)
**핵심 기여**: (주요 방법론 또는 발견)
**의의**: (왜 중요한가, 내 연구와의 연관성)
"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─────────────────────────────────────────────
# HTML 생성
# ─────────────────────────────────────────────
def generate_html_post(paper: dict, summary: str, post_date: str) -> tuple[str, str]:
    """(filename, html_content) 반환"""
    safe_title = "".join(c if c.isalnum() else "-" for c in paper["title"][:50]).strip("-")
    filename = f"{post_date}-{safe_title}.html"

    source_badge_color = "#4a90d9" if paper["source"] == "arXiv" else "#2ecc71"

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{paper['title']}</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
<nav><a href="../index.html">← 전체 목록</a></nav>
<article class="paper">
  <header>
    <span class="badge" style="background:{source_badge_color}">{paper['source']}</span>
    <span class="badge topic">{paper['topic']}</span>
    <h1>{paper['title']}</h1>
    <div class="meta">
      <span>✍️ {paper['authors']}</span>
      <span>📅 {paper['published']}</span>
      <a href="{paper['url']}" target="_blank" rel="noopener">원문 보기 →</a>
    </div>
  </header>
  <section class="summary">
    <h2>📝 요약</h2>
    <div class="summary-content">{summary.replace(chr(10), '<br>')}</div>
  </section>
  <section class="abstract">
    <h2>📄 원문 초록</h2>
    <p>{paper['abstract']}</p>
  </section>
</article>
</body>
</html>"""
    return filename, html


def generate_index(posts: list[dict], existing_posts: list[dict]) -> str:
    """메인 index.html 생성"""
    all_posts = posts + existing_posts
    # 날짜 내림차순 정렬
    all_posts.sort(key=lambda x: x.get("date", ""), reverse=True)

    cards = ""
    for p in all_posts[:100]:  # 최근 100개만
        source_color = "#4a90d9" if p.get("source") == "arXiv" else "#2ecc71"
        cards += f"""
    <article class="card">
      <div class="card-header">
        <span class="badge" style="background:{source_color}">{p.get('source','')}</span>
        <span class="date">{p.get('date','')}</span>
      </div>
      <h2><a href="posts/{p['filename']}">{p['title']}</a></h2>
      <p class="one-liner">{p.get('one_liner','')}</p>
      <div class="card-footer">
        <span class="topic-tag">{p.get('topic','')}</span>
        <a href="posts/{p['filename']}" class="read-more">읽기 →</a>
      </div>
    </article>"""

    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Digest — 매일 새로운 논문</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <h1 class="site-title">📚 Paper Digest</h1>
    <p class="site-desc">스마트 컨트랙트 보안 · 프로그램 합성 · MEV · 음악 분석</p>
    <p class="updated">마지막 업데이트: {today} · 총 {len(all_posts)}편</p>
  </div>
</header>
<main class="feed">{cards}
</main>
<footer>
  <p>Powered by arXiv · Semantic Scholar · Claude API</p>
</footer>
</body>
</html>"""


def generate_css() -> str:
    return """
:root {
  --bg: #0d0f14;
  --surface: #161920;
  --surface2: #1e2230;
  --border: #2a2f3f;
  --text: #e8eaf0;
  --text-muted: #8891a8;
  --accent: #7c6af7;
  --accent2: #4fd1c5;
  --font-display: 'DM Serif Display', Georgia, serif;
  --font-body: 'IBM Plex Sans', 'Noto Sans KR', sans-serif;
  --font-mono: 'IBM Plex Mono', monospace;
}

@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono&family=Noto+Sans+KR:wght@400;500&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  line-height: 1.7;
  min-height: 100vh;
}

/* ── Header ── */
.site-header {
  border-bottom: 1px solid var(--border);
  padding: 3rem 2rem 2rem;
  background: linear-gradient(135deg, #0d0f14 0%, #161230 100%);
}
.header-inner { max-width: 860px; margin: 0 auto; }
.site-title {
  font-family: var(--font-display);
  font-size: clamp(2rem, 5vw, 3rem);
  letter-spacing: -0.02em;
  background: linear-gradient(135deg, #fff 30%, var(--accent) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.site-desc { color: var(--text-muted); margin-top: .4rem; font-size: .95rem; }
.updated { color: var(--text-muted); font-size: .8rem; margin-top: .6rem; font-family: var(--font-mono); }

/* ── Feed ── */
.feed {
  max-width: 860px;
  margin: 2.5rem auto;
  padding: 0 1.5rem;
  display: grid;
  gap: 1.2rem;
}

/* ── Card ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.4rem 1.6rem;
  transition: border-color .2s, transform .2s;
}
.card:hover {
  border-color: var(--accent);
  transform: translateY(-2px);
}
.card-header {
  display: flex;
  align-items: center;
  gap: .6rem;
  margin-bottom: .7rem;
}
.date { font-size: .78rem; color: var(--text-muted); font-family: var(--font-mono); margin-left: auto; }
.card h2 { font-size: 1.05rem; font-weight: 600; line-height: 1.4; }
.card h2 a { color: var(--text); text-decoration: none; }
.card h2 a:hover { color: var(--accent2); }
.one-liner { color: var(--text-muted); font-size: .88rem; margin-top: .5rem; }
.card-footer { display: flex; align-items: center; justify-content: space-between; margin-top: 1rem; }
.topic-tag { font-size: .75rem; color: var(--accent); font-family: var(--font-mono); }
.read-more { font-size: .82rem; color: var(--accent2); text-decoration: none; font-weight: 500; }
.read-more:hover { text-decoration: underline; }

/* ── Badge ── */
.badge {
  display: inline-block;
  padding: .15rem .55rem;
  border-radius: 4px;
  font-size: .72rem;
  font-weight: 600;
  color: #fff;
  font-family: var(--font-mono);
  letter-spacing: .03em;
}
.badge.topic { background: var(--surface2); color: var(--text-muted); }

/* ── Post (article) ── */
nav { max-width: 760px; margin: 1.5rem auto 0; padding: 0 1.5rem; }
nav a { color: var(--text-muted); text-decoration: none; font-size: .88rem; }
nav a:hover { color: var(--accent2); }

.paper {
  max-width: 760px;
  margin: 2rem auto 4rem;
  padding: 0 1.5rem;
}
.paper header { margin-bottom: 2rem; }
.paper h1 {
  font-family: var(--font-display);
  font-size: clamp(1.4rem, 3vw, 2rem);
  line-height: 1.3;
  margin: .8rem 0 .6rem;
}
.meta { display: flex; flex-wrap: wrap; gap: .8rem; font-size: .85rem; color: var(--text-muted); }
.meta a { color: var(--accent2); text-decoration: none; }
.meta a:hover { text-decoration: underline; }

.summary, .abstract {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.6rem;
  margin-bottom: 1.5rem;
}
.summary h2, .abstract h2 {
  font-size: 1rem;
  font-weight: 600;
  margin-bottom: 1rem;
  color: var(--accent2);
}
.summary-content { font-size: .95rem; line-height: 1.8; }
.abstract p { color: var(--text-muted); font-size: .9rem; line-height: 1.8; }

/* ── Footer ── */
footer { text-align: center; padding: 2rem; color: var(--text-muted); font-size: .8rem; border-top: 1px solid var(--border); }

@media (max-width: 600px) {
  .site-header { padding: 2rem 1rem 1.5rem; }
  .feed { padding: 0 1rem; }
}
"""


# ─────────────────────────────────────────────
# 기존 포스트 메타데이터 로드
# ─────────────────────────────────────────────
def load_existing_posts(posts_dir: Path) -> list[dict]:
    meta_file = OUTPUT_DIR / "posts_meta.json"
    if meta_file.exists():
        with open(meta_file) as f:
            return json.load(f)
    return []


def save_posts_meta(posts_meta: list[dict]):
    meta_file = OUTPUT_DIR / "posts_meta.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(posts_meta, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# Git push
# ─────────────────────────────────────────────
def git_push(post_date: str):
    try:
        subprocess.run(["git", "add", "docs/"], check=True)
        subprocess.run(["git", "add", "seen_papers.db"], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"📚 논문 요약 업데이트: {post_date}"],
            check=True,
        )
        subprocess.run(["git", "push"], check=True)
        print("[Git] push 완료")
    except subprocess.CalledProcessError as e:
        print(f"[Git] push 실패: {e}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    conn = init_db()

    # 출력 디렉토리 준비
    OUTPUT_DIR.mkdir(exist_ok=True)
    posts_dir = OUTPUT_DIR / "posts"
    posts_dir.mkdir(exist_ok=True)

    post_date = datetime.now().strftime("%Y-%m-%d")

    # ── 논문 수집 ──
    print("📡 arXiv 검색 중...")
    arxiv_papers = fetch_arxiv(conn)
    print(f"   → 신규 후보: {len(arxiv_papers)}편")

    print("📡 Semantic Scholar 검색 중...")
    s2_papers = fetch_semantic_scholar(conn)
    print(f"   → 신규 후보: {len(s2_papers)}편")

    # 합치고 셔플 후 상위 N편 선택
    all_candidates = arxiv_papers + s2_papers
    random.shuffle(all_candidates)
    selected = all_candidates[:PAPERS_PER_DAY]
    print(f"\n✅ 선택된 논문: {len(selected)}편\n")

    if not selected:
        print("새로운 논문이 없습니다.")
        return

    # ── 요약 & HTML 생성 ──
    new_posts_meta = []
    existing_posts = load_existing_posts(posts_dir)

    for i, paper in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] 요약 중: {paper['title'][:60]}...")
        try:
            summary = summarize_paper(client, paper)
            filename, html = generate_html_post(paper, summary, post_date)

            # 파일 저장
            with open(posts_dir / filename, "w", encoding="utf-8") as f:
                f.write(html)

            # 첫 번째 줄(한 줄 요약) 추출
            one_liner = ""
            for line in summary.split("\n"):
                if "한 줄 요약" in line:
                    one_liner = line.split(":", 1)[-1].strip().lstrip("*").strip()
                    break

            new_posts_meta.append({
                "filename": filename,
                "title": paper["title"],
                "date": post_date,
                "source": paper["source"],
                "topic": paper["topic"],
                "one_liner": one_liner,
                "url": paper["url"],
            })

            # DB에 등록
            mark_seen(conn, paper["id"], paper["title"], paper["source"])
            print(f"   ✓ 완료")
            time.sleep(0.5)

        except Exception as e:
            print(f"   ✗ 오류: {e}")

    # ── 인덱스 & CSS 재생성 ──
    all_posts_meta = new_posts_meta + existing_posts
    save_posts_meta(all_posts_meta)

    with open(OUTPUT_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(generate_index(new_posts_meta, existing_posts))

    with open(OUTPUT_DIR / "style.css", "w", encoding="utf-8") as f:
        f.write(generate_css())

    print(f"\n🎉 완료! {len(new_posts_meta)}편 게시됨")

    # ── Git push ──
    git_push(post_date)
    conn.close()


if __name__ == "__main__":
    main()
