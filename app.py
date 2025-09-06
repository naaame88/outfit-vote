import os
import sqlite3
from functools import wraps
from datetime import datetime, timedelta
from flask import (
    Flask, render_template, request, redirect, url_for,
    make_response, flash, session
)
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────────────────────────────────────
# Flask 기본 설정
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB (이미지 직접 업로드 안 씀)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "dev-admin")
TZ = ZoneInfo("Asia/Seoul")  # 하루 2표 제한 및 투표 종료 판정은 KST 기준

# ──────────────────────────────────────────────────────────────────────────────
# DB 유틸
# ──────────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # 콘테스트 기본 메타 및 상태
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS contest (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT DEFAULT 'Angel heart · Outfit Contest',
            status TEXT DEFAULT 'submission',          -- submission | voting | results
            voting_start_at TEXT,                      -- ISO datetime (KST)
            voting_end_at TEXT                         -- ISO datetime (KST)
        );
        """
    )

    # 참가작
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            image_url TEXT NOT NULL,
            votes INTEGER DEFAULT 0,
            created_at TEXT
        );
        """
    )

    # 투표 로그 (집계는 entries.votes로 하지만, 감사 용도/추적용)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            ip TEXT,
            created_at TEXT,
            FOREIGN KEY(entry_id) REFERENCES entries(id)
        );
        """
    )

    # contest 싱글톤 보장
    cur.execute("SELECT COUNT(*) AS c FROM contest WHERE id = 1;")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO contest (id) VALUES (1);")

    conn.commit()
    conn.close()

@app.before_first_request
def _on_start():
    init_db()

# ──────────────────────────────────────────────────────────────────────────────
# 시간/상태 유틸
# ──────────────────────────────────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(TZ)

def load_contest(cur=None):
    owns_cursor = False
    conn = None
    if cur is None:
        conn = get_db()
        cur = conn.cursor()
        owns_cursor = True
    cur.execute("SELECT * FROM contest WHERE id = 1;")
    row = cur.fetchone()
    if owns_cursor:
        conn.close()
    return row

def save_contest_status(status: str, start: datetime | None = None, end: datetime | None = None):
    conn = get_db()
    cur = conn.cursor()
    if start is None and end is None:
        cur.execute("UPDATE contest SET status = ? WHERE id = 1;", (status,))
    else:
        start_iso = start.isoformat() if start else None
        end_iso = end.isoformat() if end else None
        cur.execute(
            "UPDATE contest SET status = ?, voting_start_at = ?, voting_end_at = ? WHERE id = 1;",
            (status, start_iso, end_iso),
        )
    conn.commit()
    conn.close()

def parse_iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str)

def auto_close_voting_if_needed():
    """요청 때마다 호출해서, 투표 종료 시간이 지났으면 자동으로 results로 전환."""
    c = load_contest()
    if c is None:
        return
    if c["status"] != "voting":
        return
    end_at = parse_iso(c["voting_end_at"])
    if end_at and now_kst() >= end_at:
        save_contest_status("results")  # 자동 종료

# ──────────────────────────────────────────────────────────────────────────────
# 하루 2표 제한 (KST 기준) - 쿠키 기반
# ──────────────────────────────────────────────────────────────────────────────
def today_cookie_key(prefix="votes"):
    today_str = now_kst().strftime("%Y%m%d")  # 예: 20250906
    return f"{prefix}_{today_str}"

def get_today_vote_count(req) -> int:
    key = today_cookie_key()
    raw = req.cookies.get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0

def inc_today_vote_count(resp, current_count: int):
    key = today_cookie_key()
    new_count = current_count + 1
    # 자정 지나면 자연히 쿠키 키가 바뀌므로 별도 만료 설정 불필요
    resp.set_cookie(key, str(new_count), samesite="Lax")
    return resp

# ──────────────────────────────────────────────────────────────────────────────
# 관리자 보호 데코레이터
# ──────────────────────────────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("is_admin") is True:
            return f(*args, **kwargs)
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(url_for("results"))
    return wrapper

# ──────────────────────────────────────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    auto_close_voting_if_needed()

    conn = get_db()
    cur = conn.cursor()

    c = load_contest(cur)
    cur.execute("SELECT * FROM entries ORDER BY id DESC;")
    entries = cur.fetchall()
    conn.close()

    status = c["status"]
    start_at = parse_iso(c["voting_start_at"])
    end_at = parse_iso(c["voting_end_at"])

    # 템플릿 내부에서 상태 분기: submission / voting / results
    # (index.html 하나로 상태별 화면을 구성해도 되고, 분리 템플릿을 써도 됨)
    today_count = get_today_vote_count(request)
    remaining_today = max(0, 2 - today_count)

    return render_template(
        "index.html",
        contest=c,
        entries=entries,
        status=status,
        voting_start_at=start_at,
        voting_end_at=end_at,
        remaining_today=remaining_today
    )

@app.post("/submit")
def submit():
    """제출 단계에서 참가작 등록 (이미지 URL 기반)."""
    auto_close_voting_if_needed()

    title = (request.form.get("title") or "").strip()
    author = (request.form.get("author") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()

    # 상태 확인
    c = load_contest()
    if c["status"] != "submission":
        flash("지금은 제출 기간이 아닙니다.", "warning")
        return redirect(url_for("index"))

    if not image_url:
        flash("이미지 주소는 필수입니다.", "error")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO entries (title, author, image_url, created_at) VALUES (?, ?, ?, ?)",
        (title, author, image_url, now_kst().isoformat())
    )
    conn.commit()
    conn.close()

    flash("제출되었습니다!", "success")
    return redirect(url_for("index"))

@app.post("/vote/<int:entry_id>")
def vote(entry_id: int):
    """하루 2표 제한(KST), 5일 투표 기간 내에서만 가능."""
    auto_close_voting_if_needed()

    # 콘테스트 상태 확인
    c = load_contest()
    if c["status"] != "voting":
        flash("지금은 투표 기간이 아닙니다.", "warning")
        return redirect(url_for("index"))

    # 5일 투표 기간 내인지 확인
    start_at = parse_iso(c["voting_start_at"])
    end_at = parse_iso(c["voting_end_at"])
    now = now_kst()
    if not (start_at and end_at and start_at <= now < end_at):
        # 기간 밖이면 자동으로 results로 넘겨두고 안내
        if end_at and now >= end_at:
            save_contest_status("results")
        flash("투표 기간이 아닙니다.", "warning")
        return redirect(url_for("index"))

    # 하루 2표 제한
    today_count = get_today_vote_count(request)
    if today_count >= 2:
        flash("오늘은 이미 2표를 모두 사용했습니다. 내일 다시 투표할 수 있어요!", "warning")
        return redirect(url_for("index"))

    # 존재하는 참가작인지 확인
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM entries WHERE id = ?;", (entry_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        flash("해당 참가작을 찾을 수 없습니다.", "error")
        return redirect(url_for("index"))

    # 투표 반영 (entries.votes 증가 + votes 로그)
    cur.execute("UPDATE entries SET votes = COALESCE(votes, 0) + 1 WHERE id = ?;", (entry_id,))
    cur.execute(
        "INSERT INTO votes (entry_id, ip, created_at) VALUES (?, ?, ?);",
        (entry_id, request.remote_addr or "", now.isoformat())
    )
    conn.commit()
    conn.close()

    # 쿠키에 오늘 투표 횟수 +1
    resp = make_response(redirect(url_for("index")))
    resp = inc_today_vote_count(resp, today_count)
    left = max(0, 1 - today_count)  # 이번 표 포함 전 기준으로 계산
    flash(f"투표가 반영되었습니다. (오늘 남은 표: {left}표)", "success")
    return resp

@app.get("/results")
def results():
    auto_close_voting_if_needed()

    conn = get_db()
    cur = conn.cursor()
    c = load_contest(cur)

    # 집계 (내림차순)
    cur.execute("SELECT * FROM entries ORDER BY votes DESC, id ASC;")
    ranking = cur.fetchall()
    conn.close()

    return render_template("results.html", contest=c, ranking=ranking)

# ──────────────────────────────────────────────────────────────────────────────
# 관리자 라우트: 로그인/로그아웃, 5일 투표 시작, 초기화
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/admin/login")
def admin_login():
    key = request.form.get("key", "")
    if key == ADMIN_KEY:
        session["is_admin"] = True
        flash("관리자 로그인 성공", "success")
    else:
        flash("관리자 키가 올바르지 않습니다.", "error")
    return redirect(url_for("results"))

@app.post("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("관리자 로그아웃", "info")
    return redirect(url_for("results"))

@app.post("/admin/start_voting_5days")
@admin_required
def admin_start_voting_5days():
    """
    제출 단계 -> 투표 단계 전환.
    지금 시각(KST)을 시작 시각으로 하고, 정확히 5일 뒤에 종료되도록 설정.
    """
    c = load_contest()
    if c["status"] != "submission":
        flash("현재 상태에서 투표를 시작할 수 없습니다.", "warning")
        return redirect(url_for("results"))

    start = now_kst()
    end = start + timedelta(days=5)  # 5일 투표 기간
    save_contest_status("voting", start, end)
    flash(
        f"투표를 시작했습니다. (시작: {start.strftime('%Y-%m-%d %H:%M')}, 종료: {end.strftime('%Y-%m-%d %H:%M')})",
        "success",
    )
    return redirect(url_for("index"))

@app.post("/admin/reset")
@admin_required
def admin_reset():
    """
    - 모든 제출/투표 데이터 삭제
    - 상태를 'submission'으로 되돌림
    - 투표 기간(시작/종료)도 초기화
    """
    conn = get_db()
    cur = conn.cursor()

    # votes, entries 모두 삭제
    cur.execute("DELETE FROM votes;")
    cur.execute("DELETE FROM entries;")

    # 상태 초기화
    cur.execute(
        "UPDATE contest SET status = 'submission', voting_start_at = NULL, voting_end_at = NULL WHERE id = 1;"
    )

    conn.commit()
    conn.close()

    flash("전체 초기화 완료! 이제 제출 단계로 돌아갑니다.", "success")
    return redirect(url_for("index"))

# ──────────────────────────────────────────────────────────────────────────────
# 앱 실행
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 개발용 서버 실행: 실제 배포는 WSGI/ASGI 서버 사용
    app.run(host="0.0.0.0", port=5000, debug=True)

