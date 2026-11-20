from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class TestCase(db.Model):
    __tablename__ = "test_case"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    project = db.Column(db.String(100), default="default")
    content = db.Column(db.Text, nullable=False, default='{"steps": []}')
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_by = db.Column(db.String(100), default="anonymous")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    case_type = db.Column(db.String(20), default="visual")
    __mapper_args__ = {"version_id_col": version}


    # 关键：声明 version 列为乐观锁列。
    # SQLAlchemy 会在 UPDATE 时自动加 WHERE version = <加载时的值>，
    # 并自动递增 version；影响行数为 0 时抛 StaleDataError。
    __mapper_args__ = {"version_id_col": version}

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "project": self.project,
            "content": json.loads(self.content or "{}"),
            "version": self.version,
            "case_type": self.case_type
        }


class TestRun(db.Model):
    __tablename__ = "test_run"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(20), default="pending")  # pending/running/passed/failed/error
    case_ids = db.Column(db.Text, nullable=False)          # JSON 数组
    triggered_by = db.Column(db.String(100), default="anonymous")
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    report_path = db.Column(db.String(300))
    total = db.Column(db.Integer, default=0)
    passed = db.Column(db.Integer, default=0)
    failed = db.Column(db.Integer, default=0)


class TestResult(db.Model):
    __tablename__ = "test_result"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("test_run.id"), index=True)
    case_id = db.Column(db.Integer)
    name = db.Column(db.String(200))
    status = db.Column(db.String(20))
    duration = db.Column(db.Float, default=0.0)
    history_id = db.Column(db.String(100))

class ActionMethod(db.Model):
    __tablename__ = "action_method"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True) # 方法名，如 click_by_text
    target = db.Column(db.String(100), default="browser")         # 对象，如 browser, browser.page
    args_json = db.Column(db.Text, default="[]")                  # 位置参数，如 ["url"]
    kwargs_json = db.Column(db.Text, default="[]")                # 关键字参数，如 ["text", "mark"]