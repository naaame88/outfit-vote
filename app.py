import os, sqlite3, uuid, datetime
from mimetypes import guess_type
from flask import Flask, render_template, request, redirect, url_for, make_response, flash, session
from werkzeug.exceptions import RequestEntityTooLarge

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "dev-secret")
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 업로드 8MB 제한

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")  # 배포 시 환경변수로 교체

# =========================
# DB 유틸
# =========================
def db():
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    # SQLite 외래키 활성화 (필수: on delete cascade 등 동작 보장)
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    con = db(); cur = con.cursor()
    cur.executescript("""
    create table if not exists contests(
      id integer primary key,
      title text not null default 'Angel Heart',
      status text not null default 'submission', -- submission | voting | closed
      created_at timestamp not null default CURRENT_TIMESTAMP,
      voting_opened_at text, -- ISO8601 문자열로 저장
      voting_ends_at text,   -- ISO8601 문자열로 저장
      max_entries integer not null default 10,   -- 총 수용 인원(기본 10명)
      votes_per_user integer not null default 2  -- 1인 2표
    );
    create table if not exists outfits(
      id integer primary key autoincrement,
      contest_id integer not null references contests(id) on delete cascade,
      title text,
      image_url text,
      creator_id text,
      created_at timestamp not null default CURRENT_TIMESTAMP
    );
    create table if not exists votes(
      id integer primary key autoincrement,
      contest_id integer not null references contests(id) on delete cascade,
      outfit_id integer not null references outfits(id) on delete cascade,
      voter_id text,
      created_at timestamp not null default CURRENT_TIMESTAMP
    );
    create unique index if not exists uniq_vote_per_outfit_per_voter
      on votes(contest_id, outfit_id, voter_id);
    create unique index if not exists uniq_submission_per_creator
      on outfits(contest_id, creator_id);
    """)
    cur.execute("select count(*) c from contests")
    if cur.fetchone()["c"] == 0:
        cur.execute("insert into contests(id,title) values(1,'Angel Heart')")
    con.commit(); con.close()

# =========================
# 쿠키/보안 유틸
# =========================
def ensure_voter_cookie(resp, voter_id):
    """응답에 voter_id 쿠키를 보장(없으면 생성)"""
    if not voter_id:
        voter_id = str(uuid.uuid4())
    resp.set_cookie(
        "voter_id", voter_id,
        max_age=60*60*24*365*5,  # 5년
        httponly=True,
        samesite="Lax",
        secure=bool(request.is_secure)  # HTTPS에서만 Secure
    )
    return resp

def ensure_voter():
    """
    쓰기 라우트 보호: 쿠키 없으면 한 번 리다이렉트하여 쿠키 심은 뒤 재요청 유도.
    반환: (voter_id, redirect_response or None)
    """
    vid = request.cookies.get("voter_id")
    if not vid:
        vid = str(uuid.uuid4())
        # 현재 URL에 재요청시키며 쿠키 세팅
        resp = redirect(request.url)
        resp.set_cookie(
            "voter_id", vid,
            max_age=60*60*24*365*5,
            httponly=True,
            samesite="Lax",
            secure=bool(request.is_secure)
        )
        return vid, resp
    return vid, None

def get_voter_id():
    """읽기 용: 없으면 매번 새로 만들지 말고 None 반환(ensure_voter가 관리)"""
    return request.cookies.get("voter_id")

def file_allowed(filename):
    return "." in filename and filename.rsplit(".",1)[1].lower() in {"png","jpg","jpeg","gif","webp"}

def sniff_is_image(path):
    mime, _ = guess_type(path)
    return mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}

# =========================
# 상태 자동 전환
# =========================
def _parse_iso(dtval):
    if dtval is None:
        return None
    if isinstance(dtval, datetime.datetime):
        return dtval
    # SQLite TEXT로 저장된 ISO8601
    return datetime.datetime.fromisoformat(str(dtval))

def phase_auto_close_if_needed():
    con = db(); cur = con.cursor()
    cur.execute("select status, voting_ends_at from contests where id=1")
    row = cur.fetchone()
    if row and row["status"] == "voting" and row["voting_ends_at"]:
        ends_at = _parse_iso(row["voting_ends_at"])
        if ends_at and datetime.datetime.utcnow() >= ends_at:
            # 상태가 아직 voting인 경우에만 닫기 (원자적)
            cur.execute("update contests set status='closed' where id=1 and status='voting'")
            con.commit()
    con.close()

# =========================
# 에러 핸들러
# =========================
@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(e):
    flash("업로드 용량 제한(8MB)을 초과했습니다.", "error")
    return redirect(url_for("index"))

# =========================
# 라우트
# =========================
@app.route("/", methods=["GET"])
def index():
    phase_auto_close_if_needed()
    voter_id = get_voter_id()
    if not voter_id:
        # index에서도 쿠키 보장
        voter_id = str(uuid.uuid4())

    con = db(); cur = con.cursor()
    cur.execute("select * from contests where id=1")
    contest = cur.fetchone()

    cur.execute("select * from outfits where contest_id=? order by created_at asc", (1,))
    outfits = cur.fetchall()
    entries_count = len(outfits)

    cur.execute("select count(*) c from outfits where contest_id=? and creator_id=?", (1, voter_id))
    i_submitted = cur.fetchone()["c"] > 0

    cur.execute("select count(*) c from votes where contest_id=? and voter_id=?", (1, voter_id))
    my_votes = cur.fetchone()["c"]

    # contest_id를 join 조건에 포함(방어적)
    cur.execute("""
      select o.id, count(v.id) as vote_count
      from outfits o
      left join votes v
        on v.outfit_id = o.id and v.contest_id = o.contest_id
      where o.contest_id=?
      group by o.id
    """, (1,))
    counts = {r["id"]: r["vote_count"] for r in cur.fetchall()}
    con.close()

    # 제출 전에는 목록 숨김. 단, '제출자'는 바로 볼 수 있도록 예외 허용.
    show_gallery = (contest["status"] != "submission") or i_submitted

    # 관리자 모드: 쿼리스트링 ?key=ADMIN_KEY 가 일치하면 세션 플래그로 승격, URL에 다시 노출 금지
    if request.args.get("key") == ADMIN_KEY:
        session["is_admin"] = True

    show_admin = bool(session.get("is_admin") is True)

    resp = make_response(render_template(
        "index.html",
        contest=contest,
        outfits=outfits,
        entries_count=entries_count,
        i_submitted=i_submitted,
        my_votes=my_votes,
        counts=counts,
        votes_left=max(0, int(contest["votes_per_user"]) - int(my_votes)),
        show_gallery=show_gallery,
        show_admin=show_admin
    ))
    return ensure_voter_cookie(resp, voter_id)

@app.post("/submit")
def submit():
    # 쓰기 라우트는 반드시 쿠키 보장
    voter_id, redirect_resp = ensure_voter()
    if redirect_resp:
        return redirect_resp

    title = (request.form.get("title") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    file = request.files.get("image_file")

    con = db(); cur = con.cursor()
    cur.execute("select * from contests where id=1")
    contest = cur.fetchone()
    if contest["status"] != "submission":
        con.close(); flash("지금은 제출 기간이 아닙니다.", "error"); return redirect(url_for("index"))

    cur.execute("select count(*) c from outfits where contest_id=?", (1,))
    if cur.fetchone()["c"] >= contest["max_entries"]:
        con.close(); flash("제출이 마감되었습니다.", "error"); return redirect(url_for("index"))

    cur.execute("select count(*) c from outfits where contest_id=? and creator_id=?", (1, voter_id))
    if cur.fetchone()["c"] > 0:
        con.close(); flash("이미 제출했습니다.", "error"); return redirect(url_for("index"))

    saved_url = None
    if file and file.filename and file_allowed(file.filename):
        ext = file.filename.rsplit(".",1)[1].lower()
        fname = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_FOLDER, fname)
        try:
            file.save(path)
        except Exception:
            con.close(); flash("파일 저장 중 오류가 발생했습니다.", "error"); return redirect(url_for("index"))
        # 최소 이미지 검증
        if not sniff_is_image(path):
            try:
                os.remove(path)
            except Exception:
                pass
            con.close(); flash("유효한 이미지 파일이 아닙니다.", "error"); return redirect(url_for("index"))
        saved_url = f"/static/uploads/{fname}"
    elif image_url:
        saved_url = image_url
    else:
        con.close(); flash("이미지 파일을 올리거나 이미지 URL을 입력하세요.", "error"); return redirect(url_for("index"))

    if not title:
        title = "Untitled Outfit"

    try:
        cur.execute("insert into outfits(contest_id,title,image_url,creator_id) values(?,?,?,?)",
                    (1, title, saved_url, voter_id))
        con.commit()
    except sqlite3.IntegrityError:
        con.close(); flash("제출 처리 중 충돌이 발생했습니다. 다시 시도해 주세요.", "error"); return redirect(url_for("index"))

    # 현재 인원 확인 후, 정원 도달 시 투표 시작(7일 뒤 자동 종료). 상태 전환은 원자적으로 수행.
    cur.execute("select count(*) c from outfits where contest_id=?", (1,))
    count = cur.fetchone()["c"]
    if count >= contest["max_entries"]:
        opened = datetime.datetime.utcnow().isoformat()
        ends = (datetime.datetime.utcnow() + datetime.timedelta(days=3)).isoformat()
        cur.execute("""
          update contests
          set status='voting', voting_opened_at=?, voting_ends_at=?
          where id=1 and status='submission'
        """, (opened, ends))
        con.commit()

    con.close()
    flash("제출이 완료되었습니다!", "ok")
    # 응답에도 쿠키를 재보장
    resp = redirect(url_for("index"))
    return ensure_voter_cookie(resp, voter_id)

@app.post("/vote/<int:oid>")
def vote(oid):
    voter_id, redirect_resp = ensure_voter()
    if redirect_resp:
        return redirect_resp

    con = db(); cur = con.cursor()

    cur.execute("select * from contests where id=1")
    contest = cur.fetchone()
    if contest["status"] != "voting":
        con.close(); flash("지금은 투표 기간이 아닙니다.", "error"); return redirect(url_for("index"))

    # 표 수 한도
    cur.execute("select count(*) c from votes where contest_id=? and voter_id=?", (1, voter_id))
    used = cur.fetchone()["c"]
    if used >= contest["votes_per_user"]:
        con.close(); flash("투표 가능 횟수를 모두 사용했습니다.", "error"); return redirect(url_for("index"))

    # 유효한 코디인지 확인
    cur.execute("select count(*) c from outfits where id=? and contest_id=?", (oid, 1))
    if cur.fetchone()["c"] == 0:
        con.close(); flash("해당 코디가 없습니다.", "error"); return redirect(url_for("index"))

        # (추가) 자기 작품 투표 금지
        if row["creator_id"] == voter_id:
            con.close(); flash("자기 작품에는 투표할 수 없습니다.", "error"); return redirect(url_for("index"))

    try:
        cur.execute("insert into votes(contest_id,outfit_id,voter_id) values(?,?,?)", (1, oid, voter_id))
        con.commit()
    except sqlite3.IntegrityError:
        # UNIQUE 제약 위반(중복 투표) 등
        con.close(); flash("이미 이 코디에 투표했거나 처리 중 충돌이 있었습니다.", "error"); return redirect(url_for("index"))

    con.close()
    flash("투표되었습니다!", "ok")
    resp = redirect(url_for("index"))
    return ensure_voter_cookie(resp, voter_id)

# ====== 관리자: 개별/전체 삭제 ======
def _delete_local_image_if_exists(image_url: str):
    # /static/uploads/ 로컬 업로드만 물리 파일 삭제
    if image_url and image_url.startswith("/static/uploads/"):
        path = os.path.join(BASE_DIR, image_url.lstrip("/"))
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass  # 실패해도 서비스 계속

def _require_admin():
    if not session.get("is_admin"):
        return False
    return True

@app.post("/admin/delete/<int:oid>")
def admin_delete(oid):
    if not _require_admin():
        return "Forbidden", 403

    con = db(); cur = con.cursor()
    cur.execute("select image_url from outfits where id=? and contest_id=?", (oid, 1))
    row = cur.fetchone()
    if row:
        _delete_local_image_if_exists(row["image_url"])
        cur.execute("delete from outfits where id=? and contest_id=?", (oid, 1))
        con.commit()
        flash("코디를 삭제했습니다.", "ok")
    else:
        flash("대상 코디가 없습니다.", "error")
    con.close()
    # 관리자 모드 URL에 key 노출 금지(세션으로 유지)
    return redirect(url_for("index"))

@app.post("/admin/delete_all")
def admin_delete_all():
    if not _require_admin():
        return "Forbidden", 403

    con = db(); cur = con.cursor()
    cur.execute("select image_url from outfits where contest_id=?", (1,))
    for r in cur.fetchall():
        _delete_local_image_if_exists(r["image_url"])
    cur.execute("delete from outfits where contest_id=?", (1,))
    con.commit(); con.close()
    flash("모든 코디를 삭제했습니다.", "ok")
    return redirect(url_for("index"))

@app.get("/results")
def results():
    con = db(); cur = con.cursor()
    cur.execute("select * from contests where id=1")
    contest = cur.fetchone()

    # 투표 종료 상태만 결과 페이지 허용
    if not contest or contest["status"] != "closed":
        con.close()
        flash("아직 결과를 볼 수 없습니다.", "error")
        return redirect(url_for("index"))

    # 상위 3개: 득표수 내림차순, 동률이면 먼저 제출한 순
    cur.execute("""
        with vc as (
          select outfit_id, count(*) as votes
          from votes
          where contest_id=1
          group by outfit_id
        )
        select o.*, coalesce(vc.votes, 0) as votes
        from outfits o
        left join vc on vc.outfit_id = o.id
        where o.contest_id=1
        order by votes desc, o.created_at asc
        limit 3
    """)
    top3 = cur.fetchall()
    con.close()

    return render_template("results.html", contest=contest, top3=top3)

@app.route("/admin/close", methods=["GET", "POST"])
def admin_close():
    if not _require_admin():
        return "Forbidden", 403
    con = db(); cur = con.cursor()
    cur.execute("update contests set status='closed' where id=1")
    con.commit(); con.close()
    flash("투표가 강제로 종료되었습니다.", "ok")
    return redirect(url_for("index"))

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

@app.get("/admin/status")
def admin_status():
    con = db(); cur = con.cursor()
    cur.execute("select status, voting_opened_at, voting_ends_at from contests where id=1")
    row = cur.fetchone(); con.close()
    if not row:
        return "No contest found"
    return f"status={row['status']} opened={row['voting_opened_at']} ends={row['voting_ends_at']}"

