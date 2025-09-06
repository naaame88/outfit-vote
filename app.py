import os
import uuid
import datetime
from mimetypes import guess_type

from flask import (
    Flask, render_template, request, redirect, url_for,
    make_response, flash, session
)
from werkzeug.exceptions import RequestEntityTooLarge

# --- Postgres driver ---
import psycopg2
from psycopg2 import extras, errors

# ============================================================================
# 기본 설정
# ============================================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 업로드 8MB 제한

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")  # 배포 시 환경변수로 교체

# 한국시간 기준 하루 2표
VOTES_PER_DAY = int(os.environ.get("VOTES_PER_DAY", 2))
# 투표 기간(일) 기본 5일
VOTING_PERIOD_DAYS = int(os.environ.get("VOTING_PERIOD_DAYS", 5))

# ============================================================================
# DB 유틸 (Supabase Postgres)
# ============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required (Supabase Postgres connection string)")

def db():
    # 매 요청마다 새 연결(간단 구현)
    return psycopg2.connect(DATABASE_URL, cursor_factory=extras.RealDictCursor)


def init_db():
    """idempotent: 여러 번 호출돼도 안전"""
    conn = db(); cur = conn.cursor()
    # 스키마
    cur.execute(
        """
        create table if not exists contests(
          id bigserial primary key,
          title text not null default 'Angel Heart',
          status text not null default 'submission', -- submission | voting | closed
          created_at timestamptz not null default now(),
          voting_opened_at timestamptz,
          voting_ends_at timestamptz,
          max_entries integer not null default 10,    -- 총 수용 인원
          votes_per_user integer not null default 2   -- (의미 변경) '하루' 표 수
        );

        create table if not exists outfits(
          id bigserial primary key,
          contest_id bigint not null references contests(id) on delete cascade,
          title text,
          image_url text,
          creator_id text,
          created_at timestamptz not null default now()
        );

        create table if not exists votes(
          id bigserial primary key,
          contest_id bigint not null references contests(id) on delete cascade,
          outfit_id bigint not null references outfits(id) on delete cascade,
          voter_id text,
          created_at timestamptz not null default now()
        );
        """
    )
    # 유니크 인덱스(한 사람이 같은 작품에 중복 투표 방지)
    cur.execute(
        """
        do $$ begin
          if not exists (select 1 from pg_indexes where indexname='uniq_vote_per_outfit_per_voter') then
            create unique index uniq_vote_per_outfit_per_voter on votes(contest_id, outfit_id, voter_id);
          end if;
          if not exists (select 1 from pg_indexes where indexname='uniq_submission_per_creator') then
            create unique index uniq_submission_per_creator on outfits(contest_id, creator_id);
          end if;
        end $$;
        """
    )
    # contests 기본 레코드(id=1) 보장 + 설정 값 동기화
    cur.execute("select count(*) as c from contests where id=1")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "insert into contests(id, title, votes_per_user) values (1, 'Angel Heart', %s)",
            (VOTES_PER_DAY,)
        )
    else:
        # votes_per_user를 .env의 VOTES_PER_DAY와 맞춰둠(선택)
        cur.execute("update contests set votes_per_user=%s where id=1", (VOTES_PER_DAY,))
    conn.commit(); cur.close(); conn.close()


# ============================================================================
# 쿠키/보안 유틸
# ============================================================================
def ensure_voter_cookie(resp, voter_id):
    """응답에 voter_id 쿠키를 보장(없으면 생성)"""
    if not voter_id:
        voter_id = str(uuid.uuid4())
    resp.set_cookie(
        "voter_id", voter_id,
        max_age=60*60*24*365*5,  # 5년
        httponly=True,
        samesite="Lax",
        secure=bool(request.is_secure),
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
        resp = redirect(request.url)
        resp.set_cookie(
            "voter_id", vid,
            max_age=60*60*24*365*5,
            httponly=True,
            samesite="Lax",
            secure=bool(request.is_secure),
        )
        return vid, resp
    return vid, None

def get_voter_id():
    return request.cookies.get("voter_id")

def file_allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"png", "jpg", "jpeg", "gif", "webp"}

def sniff_is_image(path):
    mime, _ = guess_type(path)
    return mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}

# ============================================================================
# 상태 자동 전환
# ============================================================================
def phase_auto_close_if_needed():
    """voting 종료 시간이 지났으면 closed로 전환"""
    conn = db(); cur = conn.cursor()
    cur.execute("select status, voting_ends_at from contests where id=%s", (1,))
    row = cur.fetchone()
    if row and row["status"] == "voting" and row["voting_ends_at"]:
        if row["voting_ends_at"] <= datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc):
            cur.execute("update contests set status='closed' where id=1 and status='voting'")
            conn.commit()
    cur.close(); conn.close()


# ============================================================================
# 에러 핸들러
# ============================================================================
@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(e):
    flash("업로드 용량 제한(8MB)을 초과했습니다.", "error")
    return redirect(url_for("index"))


# ============================================================================
# 도우미: 오늘(한국시간) 날짜 비교용 WHERE 절
#   - created_at AT TIME ZONE 'Asia/Seoul' ::date
# ============================================================================
DATE_TZ = "Asia/Seoul"
TODAY_SQL = f"(created_at AT TIME ZONE '{DATE_TZ}')::date = (now() AT TIME ZONE '{DATE_TZ}')::date"


# ============================================================================
# 라우트
# ============================================================================
@app.get("/")
def index():
    phase_auto_close_if_needed()
    voter_id = get_voter_id() or str(uuid.uuid4())

    conn = db(); cur = conn.cursor()

    # 대회 정보
    cur.execute("select * from contests where id=%s", (1,))
    contest = cur.fetchone()

    # 작품 목록
    cur.execute("select * from outfits where contest_id=%s order by created_at asc", (1,))
    outfits = cur.fetchall()
    entries_count = len(outfits)

    # 내가 제출했는가
    cur.execute("select count(*) as c from outfits where contest_id=%s and creator_id=%s", (1, voter_id))
    i_submitted = cur.fetchone()["c"] > 0

    # '오늘' 내가 쓴 표 수(한국시간 기준)
    cur.execute(
        f"""
        select count(*) as c
        from votes
        where contest_id=%s and voter_id=%s
          and {TODAY_SQL}
        """,
        (1, voter_id)
    )
    my_votes_today = cur.fetchone()["c"]

    # 각 작품 득표수(총합)
    cur.execute(
        """
        with vc as (
          select outfit_id, count(*) as vote_count
          from votes
          where contest_id=%s
          group by outfit_id
        )
        select o.id, coalesce(vc.vote_count,0) as vote_count
        from outfits o
        left join vc on vc.outfit_id = o.id
        where o.contest_id=%s
        """,
        (1, 1)
    )
    counts = {r["id"]: r["vote_count"] for r in cur.fetchall()}

    cur.close(); conn.close()

    # 제출 화면 노출 여부(원본 로직 유지)
    show_gallery = (contest["status"] != "submission") or i_submitted

    # 관리자 퀵 로그인 지원 (?key=ADMIN_KEY)
    if request.args.get("key") == ADMIN_KEY:
        session["is_admin"] = True
    show_admin = bool(session.get("is_admin") is True)

    resp = make_response(render_template(
        "index.html",
        contest=contest,
        outfits=outfits,
        entries_count=entries_count,
        i_submitted=i_submitted,
        # 과거 템플릿 호환을 위해 이름 유지하되 '오늘 남은 표'로 의미 변경
        my_votes=my_votes_today,
        counts=counts,
        votes_left=max(0, int(contest["votes_per_user"]) - int(my_votes_today)),
        show_gallery=show_gallery,
        show_admin=show_admin
    ))
    return ensure_voter_cookie(resp, voter_id)


@app.post("/submit")
def submit():
    voter_id, redirect_resp = ensure_voter()
    if redirect_resp:
        return redirect_resp

    title = (request.form.get("title") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    file = request.files.get("image_file")

    conn = db(); cur = conn.cursor()
    cur.execute("select * from contests where id=%s", (1,))
    contest = cur.fetchone()

    if contest["status"] != "submission":
        cur.close(); conn.close()
        flash("지금은 제출 기간이 아닙니다.", "error")
        return redirect(url_for("index"))

    # 정원 체크
    cur.execute("select count(*) as c from outfits where contest_id=%s", (1,))
    if cur.fetchone()["c"] >= contest["max_entries"]:
        cur.close(); conn.close()
        flash("제출이 마감되었습니다.", "error")
        return redirect(url_for("index"))

    # 1인 1제출 제한
    cur.execute("select count(*) as c from outfits where contest_id=%s and creator_id=%s", (1, voter_id))
    if cur.fetchone()["c"] > 0:
        cur.close(); conn.close()
        flash("이미 제출했습니다.", "error")
        return redirect(url_for("index"))

    # 이미지 저장 (파일 또는 URL)
    saved_url = None
    if file and file.filename and file_allowed(file.filename):
        ext = file.filename.rsplit(".", 1)[1].lower()
        fname = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_FOLDER, fname)
        try:
            file.save(path)
        except Exception:
            cur.close(); conn.close()
            flash("파일 저장 중 오류가 발생했습니다.", "error")
            return redirect(url_for("index"))
        if not sniff_is_image(path):
            try: os.remove(path)
            except Exception: pass
            cur.close(); conn.close()
            flash("유효한 이미지 파일이 아닙니다.", "error")
            return redirect(url_for("index"))
        saved_url = f"/static/uploads/{fname}"
    elif image_url:
        saved_url = image_url
    else:
        cur.close(); conn.close()
        flash("이미지 파일을 올리거나 이미지 URL을 입력하세요.", "error")
        return redirect(url_for("index"))

    if not title:
        title = "Untitled Outfit"

    try:
        cur.execute(
            "insert into outfits(contest_id,title,image_url,creator_id) values(%s,%s,%s,%s)",
            (1, title, saved_url, voter_id)
        )
        conn.commit()
    except errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        flash("제출 처리 중 충돌이 발생했습니다. 다시 시도해 주세요.", "error")
        return redirect(url_for("index"))

    # 정원 도달 시 자동으로 투표 시작(기간: 환경변수 또는 기본 5일)
    cur.execute("select count(*) as c from outfits where contest_id=%s", (1,))
    count = cur.fetchone()["c"]
    if count >= contest["max_entries"]:
        opened = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
        ends = opened + datetime.timedelta(days=VOTING_PERIOD_DAYS)
        cur.execute(
            """
            update contests
               set status='voting',
                   voting_opened_at=%s,
                   voting_ends_at=%s
             where id=1 and status='submission'
            """,
            (opened, ends)
        )
        conn.commit()

    cur.close(); conn.close()
    flash("제출이 완료되었습니다!", "ok")
    resp = redirect(url_for("index"))
    return ensure_voter_cookie(resp, voter_id)


@app.post("/vote/<int:oid>")
def vote(oid):
    """하루 2표(한국시간 기준), 자기 작품 투표 금지, 작품당 중복 투표 금지"""
    voter_id, redirect_resp = ensure_voter()
    if redirect_resp:
        return redirect_resp

    conn = db(); cur = conn.cursor()
    cur.execute("select * from contests where id=%s", (1,))
    contest = cur.fetchone()
    if contest["status"] != "voting":
        cur.close(); conn.close()
        flash("지금은 투표 기간이 아닙니다.", "error")
        return redirect(url_for("index"))

    # 오늘 내가 사용한 표 수
    cur.execute(
        f"""
        select count(*) as c
          from votes
         where contest_id=%s and voter_id=%s
           and {TODAY_SQL}
        """,
        (1, voter_id)
    )
    used_today = cur.fetchone()["c"]
    if used_today >= contest["votes_per_user"]:
        cur.close(); conn.close()
        flash("오늘 투표 가능 횟수를 모두 사용했습니다. 내일 다시 투표할 수 있어요!", "error")
        return redirect(url_for("index"))

    # 유효한 코디인지 + 자기 작품 제한
    cur.execute("select id, creator_id from outfits where id=%s and contest_id=%s", (oid, 1))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        flash("해당 코디가 없습니다.", "error")
        return redirect(url_for("index"))
    if row["creator_id"] == voter_id:
        cur.close(); conn.close()
        flash("자기 작품에는 투표할 수 없습니다.", "error")
        return redirect(url_for("index"))

    # 작품당 중복 투표 방지(같은 날 여러 번도 금지) — uniq 인덱스로 보장
    try:
        cur.execute("insert into votes(contest_id,outfit_id,voter_id) values(%s,%s,%s)", (1, oid, voter_id))
        conn.commit()
    except errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        flash("이미 이 코디에 투표했거나 처리 중 충돌이 있었습니다.", "error")
        return redirect(url_for("index"))

    cur.close(); conn.close()
    flash("투표되었습니다!", "ok")
    resp = redirect(url_for("index"))
    return ensure_voter_cookie(resp, voter_id)


# ============================================================================
# 관리자: 개별/전체 삭제, 초기화, 강제 시작/종료
# ============================================================================
def _delete_local_image_if_exists(image_url: str):
    if image_url and image_url.startswith("/static/uploads/"):
        path = os.path.join(BASE_DIR, image_url.lstrip("/"))
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

def _require_admin():
    return bool(session.get("is_admin"))

@app.post("/admin/delete/<int:oid>")
def admin_delete(oid):
    if not _require_admin():
        return "Forbidden", 403

    conn = db(); cur = conn.cursor()
    cur.execute("select image_url from outfits where id=%s and contest_id=%s", (oid, 1))
    row = cur.fetchone()
    if row:
        _delete_local_image_if_exists(row["image_url"])
        cur.execute("delete from outfits where id=%s and contest_id=%s", (oid, 1))
        conn.commit()
        flash("코디를 삭제했습니다.", "ok")
    else:
        flash("대상 코디가 없습니다.", "error")
    cur.close(); conn.close()
    return redirect(url_for("index"))

@app.post("/admin/delete_all")
def admin_delete_all():
    if not _require_admin():
        return "Forbidden", 403

    conn = db(); cur = conn.cursor()
    cur.execute("select image_url from outfits where contest_id=%s", (1,))
    for r in cur.fetchall():
        _delete_local_image_if_exists(r["image_url"])
    cur.execute("delete from outfits where contest_id=%s", (1,))
    conn.commit(); cur.close(); conn.close()
    flash("모든 코디를 삭제했습니다.", "ok")
    return redirect(url_for("index"))

@app.post("/admin/reset")
def admin_reset():
    """전체 초기화: 모든 제출/투표 삭제 + 상태 초기화(제출 단계)"""
    if not _require_admin():
        return "Forbidden", 403

    conn = db(); cur = conn.cursor()
    # 업로드 파일 삭제
    cur.execute("select image_url from outfits where contest_id=%s", (1,))
    for r in cur.fetchall():
        _delete_local_image_if_exists(r["image_url"])

    # 테이블 정리
    cur.execute("delete from votes where contest_id=%s", (1,))
    cur.execute("delete from outfits where contest_id=%s", (1,))
    cur.execute(
        """
        update contests
           set status='submission',
               voting_opened_at=null,
               voting_ends_at=null,
               votes_per_user=%s
         where id=1
        """,
        (VOTES_PER_DAY,)
    )
    conn.commit(); cur.close(); conn.close()

    flash("전체 초기화 완료! 이제 제출 단계로 돌아갑니다.", "ok")
    return redirect(url_for("index"))

@app.get("/admin/status")
def admin_status():
    conn = db(); cur = conn.cursor()
    cur.execute("select status, voting_opened_at, voting_ends_at from contests where id=1")
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return "No contest found"
    return f"status={row['status']} opened={row['voting_opened_at']} ends={row['voting_ends_at']}"

@app.post("/admin/start_voting_5days")
def admin_start_voting_5days():
    """관리자: 지금부터 5일(또는 env 설정) 투표 시작"""
    if not _require_admin():
        return "Forbidden", 403

    conn = db(); cur = conn.cursor()
    cur.execute("select status from contests where id=1")
    row = cur.fetchone()
    if not row or row["status"] != "submission":
        cur.close(); conn.close()
        flash("현재 상태에서 투표를 시작할 수 없습니다.", "error")
        return redirect(url_for("index"))

    opened = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    ends = opened + datetime.timedelta(days=VOTING_PERIOD_DAYS)
    cur.execute(
        """
        update contests
           set status='voting',
               voting_opened_at=%s,
               voting_ends_at=%s
         where id=1 and status='submission'
        """,
        (opened, ends)
    )
    conn.commit(); cur.close(); conn.close()
    flash("투표를 시작했습니다. (기간: {}일)".format(VOTING_PERIOD_DAYS), "ok")
    return redirect(url_for("index"))

@app.route("/admin/close", methods=["GET", "POST"])
def admin_close():
    if not _require_admin():
        return "Forbidden", 403
    conn = db(); cur = conn.cursor()
    cur.execute("update contests set status='closed' where id=1")
    conn.commit(); cur.close(); conn.close()
    flash("투표가 강제로 종료되었습니다.", "ok")
    return redirect(url_for("index"))


# ============================================================================
# 결과 화면
# ============================================================================
@app.get("/results")
def results():
    conn = db(); cur = conn.cursor()
    cur.execute("select * from contests where id=%s", (1,))
    contest = cur.fetchone()

    if not contest or contest["status"] != "closed":
        cur.close(); conn.close()
        flash("아직 결과를 볼 수 없습니다.", "error")
        return redirect(url_for("index"))

    cur.execute(
        """
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
        """
    )
    top3 = cur.fetchall()
    cur.close(); conn.close()
    return render_template("results.html", contest=contest, top3=top3)


# ============================================================================
# 앱 시작 시 스키마 보장 (Flask 3: before_first_request 제거 대체)
# ============================================================================
init_db()

if __name__ == "__main__":
    # 로컬 실행용. 배포는 Procfile의 gunicorn 커맨드 사용
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
