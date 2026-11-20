import json
import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"

TEST_TEMPLATE = '''
import pytest, requests, allure

CASES = {cases_literal}

@pytest.mark.parametrize("case", CASES, ids=[str(c["id"]) for c in CASES])
def test_case(case):
    with allure.step(f"用例: {{case['name']}}"):
        for step in case.get("steps", []):
            with allure.step(f"步骤: {{step.get('name', '')}}"):
                method = step.get("method", "GET").upper()
                url = step["url"]
                resp = requests.request(method, url, timeout=10)
                allure.attach(
                    str(resp.status_code),
                    name="status_code",
                    attachment_type=allure.attachment_type.TEXT,
                )
                assert resp.status_code == step.get("expect_status", 200), \
                    f"期望状态码 {{step.get('expect_status', 200)}}，实际 {{resp.status_code}}"
'''


# def _render_test_file(cases, target: Path):
#     """用 repr 生成安全的 Python 字面量，避免任意代码执行。"""
#     cases_literal = repr(cases)
#     target.write_text(TEST_TEMPLATE.format(cases_literal=cases_literal), encoding="utf-8")

def prepare_test_file(cases, target_path: Path):
    """准备测试文件：融合 visual (可视化) 和 script (导入源码) 两种模式"""
    code_blocks = []

    for case in cases:
        if case.case_type == "script":
            # 如果是导入的源码，直接追加
            code_blocks.append(f"\n# --- 导入的用例: {case.name} (ID: {case.id}) ---")
            code_blocks.append(case.content)
            code_blocks.append("\n")
        else:
            # 如果是可视化用例，调用 render_json_to_code
            try:
                content = json.loads(case.content)
                visual_code = render_json_to_code(content, case.id, case.name)
                code_blocks.append(visual_code)
            except Exception as e:
                code_blocks.append(f"\n# 用例 {case.name} 解析失败: {e}\n")

    target_path.write_text("\n".join(code_blocks), encoding="utf-8")


def _find_prev_history(run_id: int, project: str):
    """找到上一次同项目已完成 run 的 history 目录，用于趋势图连续。"""
    from models import TestRun, db
    prev = (
        TestRun.query.filter(
            TestRun.id < run_id,
            TestRun.status.in_(["passed", "failed"]),
        )
        .order_by(TestRun.id.desc())
        .first()
    )
    if not prev or not prev.report_path:
        return None
    history = Path(prev.report_path) / "history"
    return history if history.exists() else None


def run_cases(run_id: int, cases: list) -> dict:
    """执行一组用例，返回产物路径与退出码。"""
    run_dir = RUNS_DIR / str(run_id)
    work_dir = run_dir / "work"
    results_dir = run_dir / "allure-results"
    report_dir = run_dir / "allure-report"
    log_path = run_dir / "pytest.log"

    work_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    test_file = work_dir / "test_generated.py"
    # _render_test_file(cases, test_file)
    prepare_test_file(cases, test_file)


    # 搬运历史，保证趋势图连续
    prev_history = _find_prev_history(run_id, "default")
    if prev_history:
        shutil.copytree(prev_history, results_dir / "history", dirs_exist_ok=True)

    # 执行 pytest，日志同时写入文件（供 SSE 读取）
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                "pytest", str(test_file), "-v",
                "--alluredir", str(results_dir),
                "--capture=tee-sys",
            ],
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            log.write(line)
            log.flush()
        proc.wait()

    # 生成静态报告
    subprocess.run(
        ["allure", "generate", str(results_dir), "-o", str(report_dir), "--clean"],
        check=False,
    )

    return {
        "returncode": proc.returncode,
        "report_dir": str(report_dir),
        "results_dir": str(results_dir),
        "log_path": str(log_path),
    }


def parse_results(results_dir: str) -> list:
    """解析 allure-results/*.json，写入结构化结果。"""
    out = []
    for p in Path(results_dir).glob("*-result.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        duration = sum(
            (s.get("stop", 0) - s.get("start", 0))
                for s in data.get("steps", [])
        ) / 1000.0
        out.append({
            "name": data.get("name"),
            "status": data.get("status"),
            "duration": duration,
            "history_id": data.get("historyId"),
        })
    return out


def _parse_value(v):
    """将前端传来的字符串转换为 Python 表达式/字面量"""
    if isinstance(v, bool):
        return "True" if v else "False"
    elif isinstance(v, str):
        # 支持前端传入变量表达式，如 data['url']
        if (v.startswith("data[") and v.endswith("]")) or (v.startswith('f"') and v.endswith('"')):
            return v
            return repr(v)  # 自动处理引号和转义
    else:
        return repr(v)


def render_json_to_code(content, case_id, case_name):
    """核心：将单条可视化用例渲染为 Python 函数代码"""
    lines = []
    steps = content.get("steps", [])
    param_data = content.get("parametrize", "[None]")

    safe_name = "".join([c if c.isalnum() else "_" for c in case_name])
    func_name = f"test_{case_id}_{safe_name}"

    # 1. 生成装饰器 (参数化)
    lines.append(f'@pytest.mark.parametrize("data", {param_data})')
    # 2. 生成函数定义 (注入 page fixture)
    lines.append(f'def {func_name}(page, data):')
    lines.append('    browser = BasePage(page)')

    # 3. 渲染所有步骤
    for step in steps:
        target = step.get("target", "browser")
        action = step.get("action")
        if not action: continue

        args = step.get("args", [])
        kwargs = step.get("kwargs", {})

        formatted_args = [_parse_value(a) for a in args]
        formatted_kwargs = [f"{k}={_parse_value(v)}" for k, v in kwargs.items()]
        params_str = ", ".join(formatted_args + formatted_kwargs)

        lines.append(f'    {target}.{action}({params_str})')

    lines.append("\n")  # 用例之间空一行
    return "\n".join(lines)