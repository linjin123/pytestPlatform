import json
import threading
import time
from datetime import datetime

from models import db, TestCase, TestRun, TestResult
from executor import run_cases, parse_results


class Worker:
    """单线程串行 Worker，从 pending 任务表拉取并执行。"""

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                with self.app.app_context():
                    run = (
                        TestRun.query
                        .filter_by(status="pending")
                        .order_by(TestRun.id)
                        .first()
                    )
                    if run:
                        self._execute(run)
                    else:
                        time.sleep(1)
            except Exception as e:  # 防止线程因异常退出
                print(f"[worker] loop error: {e}")
                time.sleep(2)

    def _execute(self, run: TestRun):
        run.status = "running"
        run.started_at = datetime.utcnow()
        db.session.commit()

        case_ids = json.loads(run.case_ids)
        cases = TestCase.query.filter(TestCase.id.in_(case_ids)).all()
        case_dicts = [c.to_dict() for c in cases]

        try:
            result = run_cases(run.id, case_dicts)
            parsed = parse_results(result["results_dir"])

            passed = sum(1 for r in parsed if r["status"] == "passed")
            failed = len(parsed) - passed

            for r in parsed:
                db.session.add(TestResult(
                    run_id=run.id,
                    case_id=None,
                    name=r["name"],
                    status=r["status"],
                    duration=r["duration"],
                    history_id=r["history_id"],
                ))

            run.status = "passed" if failed == 0 else "failed"
            run.total = len(parsed)
            run.passed = passed
            run.failed = failed
            run.report_path = result["report_dir"]
        except Exception as e:
            run.status = "error"
            print(f"[worker] run {run.id} error: {e}")
        finally:
            run.finished_at = datetime.utcnow()
            db.session.commit()