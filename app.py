import os, sqlite3, uuid, datetime
from flask import Flask, render_template, request, redirect, url_for, make_response, flash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "dev-secret")
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 업로드 8MB 제한

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")  # 반드시 배포 시 환경변수로 교체

def db():
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db(); cur = con.cursor()
    cur.executescript("""
    create table if not exists contests(
      id integer primary key,
      title text not null default 'Outfit Contest',
      status text not null default 'submission', -- submission | voting | closed
      created_at timestamp not null default CURRENT_TIMESTAMP,
      voting_opened_at timestamp,
      voting_ends_at timestamp,
      max_entries integer not null default 20,   -- 20명 제출
      votes_per_user integer not null default 3  -- 1인 3표
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
        cur.execute("insert into contests(id,title) values(1,'Outfit Contest')")
    con.commit(); con.close()

def ensure_voter_cookie(resp, voter_id):
    if not voter_id:
        voter_id = str(uuid.uuid4())
    # 5년 쿠키로 익명 식별
    resp.set_cookie("voter_id", voter_id, max_age=60*60*24*365*5, httponly=True, samesite="Lax")
    return resp

def get_voter_id():
    return request.cookies.get("voter_id") or str(uuid.uuid4())

def file_allowed(filename):
    return "." in filename and filename.rsplit(".",1)[1].lower() in {"png","jpg","jpeg","gif","webp"}

def phase_auto_close_if_needed():
    con = db(); cur = con.cursor()
    cur.execute("select status, voting_ends_at from contests where id=1")
    row = cur.fetchone()
    if row and row["status"] == "voting" and row["voting_ends_at"]:
        if datetime.datetime.utcnow() >= datetime.datetime.fromisoformat(str(row["voting_ends_at"])) :
            cur.execute("update contests set status='closed' where id=1")
            con.commit()
    con.close()

@app.route("/", methods=["GET"])
def index():
    phase_auto_close_if_needed()
    voter_id = get_voter_id()

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

    cur.execute("""
      select o.id, count(v.id) as vote_count
      from outfits o left join votes v on v.outfit_id=o.id
      where o.contest_id=? group by o.id
    """, (1,))
    counts = {r["id"]: r["vote_count"] for r in cur.fetchall()}
    con.close()

    # 제출 전에는 목록 숨김. 단, '제출자'는 바로 볼 수 있도록 예외 허용.
    show_gallery = (contest["status"] != "submission") or i_submitted

    # 관리자 모드: 쿼리스트링 ?key=ADMIN_KEY 가 일치하면 관리자 UI 노출
    show_admin = (request.args.get("key") == ADMIN_KEY)

    resp = make_response(render_template(
        "index.html",
        contest=contest,
        outfits=outfits,
        entries_count=entries_count,
        i_submitted=i_submitted,
        my_votes=my_votes,
        counts=counts,
        votes_left=max(0, contest["votes_per_user"] - my_votes),
        show_gallery=show_gallery,
        show_admin=show_admin,
        admin_key=request.args.get("key")
    ))
    return ensure_voter_cookie(resp, voter_id)

@app.post("/submit")
def submit():
    voter_id = get_voter_id()
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
        file.save(path)
        saved_url = f"/static/uploads/{fname}"
    elif image_url:
        saved_url = image_url
    else:
        con.close(); flash("이미지 파일을 올리거나 이미지 URL을 입력하세요.", "error"); return redirect(url_for("index"))

    if not title:
        title = "Untitled Outfit"

    cur.execute("insert into outfits(contest_id,title,image_url,creator_id) values(?,?,?,?)",
                (1, title, saved_url, voter_id))
    con.commit()

    # 제출자가 20명 찼으면 투표 시작(7일 뒤 자동 종료)
    cur.execute("select count(*) c from outfits where contest_id=?", (1,))
    count = cur.fetchone()["c"]
    if count >= contest["max_entries"]:
        opened = datetime.datetime.utcnow()
        ends = opened + datetime.timedelta(days=7)
        cur.execute("update contests set status='voting', voting_opened_at=?, voting_ends_at=? where id=1",
                    (opened.isoformat(), ends.isoformat()))
        con.commit()

    con.close()
    flash("제출이 완료되었습니다!", "ok")
    return redirect(url_for("index"))

@app.post("/vote/<int:oid>")
def vote(oid):
    voter_id = get_voter_id()
    con = db(); cur = con.cursor()

    cur.execute("select * from contests where id=1")
    contest = cur.fetchone()
    if contest["status"] != "voting":
        con.close(); flash("지금은 투표 기간이 아닙니다.", "error"); return redirect(url_for("index"))

    # 본인 제출자는 투표 불가
    cur.execute("select count(*) c from outfits where contest_id=? and creator_id=?", (1, voter_id))
    if cur.fetchone()["c"] > 0:
        con.close(); flash("본인 제출자는 투표할 수 없습니다.", "error"); return redirect(url_for("index"))

    # 표 수 한도
    cur.execute("select count(*) c from votes where contest_id=? and voter_id=?", (1, voter_id))
    used = cur.fetchone()["c"]
    if used >= contest["votes_per_user"]:
        con.close(); flash("투표 가능 횟수를 모두 사용했습니다.", "error"); return redirect(url_for("index"))

    # 중복 투표 방지(같은 코디)
    cur.execute("select count(*) c from votes where contest_id=? and voter_id=? and outfit_id=?", (1, voter_id, oid))
    if cur.fetchone()["c"] > 0:
        con.close(); flash("이미 이 코디에 투표했습니다.", "error"); return redirect(url_for("index"))

    # 유효한 코디인지 확인
    cur.execute("select count(*) c from outfits where id=? and contest_id=?", (oid, 1))
    if cur.fetchone()["c"] == 0:
        con.close(); flash("해당 코디가 없습니다.", "error"); return redirect(url_for("index"))

    cur.execute("insert into votes(contest_id,outfit_id,voter_id) values(?,?,?)", (1, oid, voter_id))
    con.commit(); con.close()
    flash("투표되었습니다!", "ok")
    return redirect(url_for("index"))

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

@app.post("/admin/delete/<int:oid>")
def admin_delete(oid):
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
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
    return redirect(url_for("index", key=key))  # 관리자 모드 유지

@app.post("/admin/delete_all")
def admin_delete_all():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return "Forbidden", 403

    con = db(); cur = con.cursor()
    cur.execute("select image_url from outfits where contest_id=?", (1,))
    for r in cur.fetchall():
        _delete_local_image_if_exists(r["image_url"])
    cur.execute("delete from outfits where contest_id=?", (1,))
    con.commit(); con.close()
    flash("모든 코디를 삭제했습니다.", "ok")
    return redirect(url_for("index", key=key))

init_db()

if __name__ == "__main__":
    # 로컬 개발용
    app.run(debug=True)
