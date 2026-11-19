import json
import os
import time
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, Response, send_from_directory, abort,
)
from sqlalchemy.orm.exc import StaleDataError

from models import db, TestCase, TestRun, TestResult
from executor import RUNS_DIR
from worker import Worker

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "mysql+pymysql://root:Password123%40mysql@172.17.2.79:3306/testhub?charset=utf8mb4"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


# ---------- 用例列表 ----------
@app.route("/")
def index():
    cases = TestCase.query.order_by(TestCase.updated_at.desc()).all()
    runs = TestRun.query.order_by(TestRun.id.desc()).limit(20).all()
    return render_template("cases.html", cases=cases, runs=runs)


# ---------- 新建 / 编辑 ----------
@app.route("/cases/new")
def new_case():
    return render_template("edit_case.html", case=None)


@app.route("/cases", methods=["POST"])
def create_case():
    case = TestCase(
        name=request.form["name"],
        content=request.form.get("content", '{"steps": []}'),
        updated_by=request.form.get("user", "anonymous"),
    )
    db.session.add(case)
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/cases/<int:case_id>/edit")
def edit_case(case_id):
    case = db.session.get(TestCase, case_id) or abort(404)
    return render_template("edit_case.html", case=case)


@app.route("/cases/<int:case_id>", methods=["POST"])
def update_case(case_id):
    """乐观锁核心：先比对前端 version，再依赖 SQLAlchemy 的 version_id_col。"""
    case = db.session.get(TestCase, case_id) or abort(404)
    client_version = int(request.form.get("version", 0))

    # 第一道防线：前端基于旧版本提交
    if case.version != client_version:
        db.session.rollback()
        return render_template(
            "edit_case.html", case=case,
            error="保存冲突：该用例已被他人修改，请刷新后重新编辑。",
        ), 409

    case.name = request.form["name"]
    case.content = request.form.get("content", "{}")
    case.updated_by = request.form.get("user", "anonymous")

    try:
        db.session.commit()
    except StaleDataError:
        # 第二道防线：并发情况下 SQLAlchemy 在 UPDATE 时发现版本已变
        db.session.rollback()
        case = db.session.get(TestCase, case_id)
        return render_template(
            "edit_case.html", case=case,
            error="保存冲突：该用例在你提交期间已被他人修改。",
        ), 409

    return redirect(url_for("index"))


# ---------- 批量导入 ----------
@app.route("/cases/import", methods=["POST"])
def import_cases():
    file = request.files.get("file")
    if not file:
        return "缺少文件", 400
    data = json.loads(file.read().decode("utf-8"))
    for item in data:
        db.session.add(TestCase(
            name=item["name"],
            content=json.dumps(item.get("content", {"steps": []}), ensure_ascii=False),
            updated_by=request.form.get("user", "anonymous"),
        ))
    db.session.commit()
    return redirect(url_for("index"))


# ---------- 触发执行 ----------
@app.route("/runs", methods=["POST"])
def create_run():
    case_ids = request.form.getlist("case_ids")
    if not case_ids:
        return "未选择用例", 400
    run = TestRun(
        status="pending",
        case_ids=json.dumps([int(i) for i in case_ids]),
        triggered_by=request.form.get("user", "anonymous"),
    )
    db.session.add(run)
    db.session.commit()
    return redirect(url_for("run_detail", run_id=run.id))


@app.route("/runs/<int:run_id>")
def run_detail(run_id):
    run = db.session.get(TestRun, run_id) or abort(404)
    results = TestResult.query.filter_by(run_id=run_id).all()
    return render_template("run_detail.html", run=run, results=results)


# ---------- SSE 实时日志 ----------
@app.route("/runs/<int:run_id>/stream")
def stream_log(run_id):
    log_path = RUNS_DIR / str(run_id) / "pytest.log"

    def generate():
        pos = 0
        while True:
            chunk = None
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    line = f.readline()
                    if line:
                        pos = f.tell()
                        chunk = line.rstrip("\n")

            if chunk:
                # 用 json.dumps 规避 SSE 对换行的限制
                yield f"data: {json.dumps(chunk)}\n\n"
            else:
                with app.app_context():
                    run = db.session.get(TestRun, run_id)
                    status = run.status if run else None
                if status in ("passed", "failed", "error"):
                    yield "event: done\ndata: finished\n\n"
                    break
                yield ": keepalive\n\n"
                time.sleep(0.3)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 报告静态托管 ----------
@app.route("/reports/<int:run_id>/<path:filename>")
def serve_report(run_id, filename):
    report_dir = RUNS_DIR / str(run_id) / "allure-report"
    if not report_dir.exists():
        abort(404)
    return send_from_directory(report_dir, filename)


# ---------- 状态轮询 ----------
@app.route("/api/runs/<int:run_id>/status")
def run_status(run_id):
    run = db.session.get(TestRun, run_id) or abort(404)
    return jsonify({"status": run.status, "total": run.total,
                    "passed": run.passed, "failed": run.failed})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    # debug=False + use_reloader=False，避免 Worker 被启动两次
    worker = Worker(app)
    worker.start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)