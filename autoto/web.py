"""로컬 홈페이지: 분석 결과를 브라우저에서 보고, 분석을 실행합니다.

  python -m autoto.web            # http://127.0.0.1:8000 이 자동으로 열립니다
  python -m autoto.web --port 8080 --no-browser

이 컴퓨터(127.0.0.1)에서만 접속됩니다. 결과는 output/ 폴더의 CSV를 그대로 읽습니다.
"""

import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

DATASETS = {
    "turnover_today": ("turnover/turnover_today.csv", False),
    "turnover_history": ("turnover/turnover_history.csv", False),
    "candidates": ("monthly_candle/candidates.csv", False),
    "backtest_summary": ("monthly_candle/backtest_summary.csv", True),
    "backtest_monthly": ("monthly_candle/backtest_monthly.csv", False),
    "rights": ("rights_offering/third_party_allotments.csv", False),
    "allottees": ("rights_offering/allottees.csv", False),
}


# 숫자처럼 보여도 코드·날짜라 글자로 둘 열 (앞자리 0 유지, 천 단위 쉼표 방지)
TEXT_COLUMNS = {c: str for c in ("종목코드", "접수번호", "접수일", "업종코드", "설립일", "month")}


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _clean(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def read_dataset(path, summary=False):
    """CSV -> {"columns", "rows", "updated"}. 파일이 없으면 None."""
    if not path.exists():
        return None
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=TEXT_COLUMNS)
    if summary:
        # 요약표는 '기간' 같은 글자 행이 섞여 열 전체가 문자열로 읽히므로 숫자는 되돌립니다.
        df = df.rename(columns={df.columns[0]: "항목"})
        for col in df.columns[1:]:
            df[col] = [_to_number(v) for v in df[col]]
    rows = [[_clean(v) for v in row] for row in df.astype(object).itertuples(index=False)]
    updated = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return {"columns": list(df.columns), "rows": rows, "updated": updated}


def load_all(output_dir=OUTPUT_DIR):
    return {name: read_dataset(output_dir / rel, summary) for name, (rel, summary) in DATASETS.items()}


# ---------------------------------------------------------------------------
# 분석 실행

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _number(params, key, default, lo, hi):
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key}: 숫자가 아닙니다")
    if not lo <= value <= hi:
        raise ValueError(f"{key}: {lo}~{hi} 사이여야 합니다")
    return f"{value:g}"


def build_commands(task, params):
    """task 와 화면 입력값을 실행할 명령 목록으로. 허용된 값만 통과시킵니다."""
    py = [sys.executable, "-m"]
    strategy = []
    for key, flag, default, lo, hi in (
        ("min_hit", "--min-hit", 1.0, 0, 1),
        ("max_range", "--max-range", 0.25, 0, 5),
        ("target", "--target", 0.05, 0.001, 1),
        ("lookback", "--lookback", 6, 1, 36),
    ):
        if params.get(key) not in (None, ""):
            value = _number(params, key, default, lo, hi)
            strategy += [flag, str(int(float(value))) if key == "lookback" else value]

    month = params.get("month") or ""
    if month and not MONTH_RE.match(month):
        raise ValueError("month: YYYY-MM 형식이어야 합니다")

    turnover = py + ["autoto.turnover", "--top", _number(params, "top", 200, 1, 3000)]
    screen = py + ["autoto.monthly_candle", "screen"] + (["--month", month] if month else []) + strategy
    backtest = py + ["autoto.monthly_candle", "backtest", "--years", _number(params, "years", 5, 0.5, 20)] + strategy
    rights = py + ["autoto.rights_offering", "--days", _number(params, "days", 365, 1, 3650)]

    commands = {
        "daily": [turnover, screen, rights],
        "turnover": [turnover],
        "monthly_screen": [screen],
        "monthly_backtest": [backtest],
        "rights_offering": [rights],
    }
    if task not in commands:
        raise ValueError(f"알 수 없는 작업: {task}")
    return commands[task]


class Runner:
    """분석을 하나씩 백그라운드에서 실행하고 로그를 모읍니다."""

    def __init__(self, cwd=ROOT):
        self.cwd = cwd
        self.lock = threading.Lock()
        self.log = collections.deque(maxlen=400)
        self.task = None
        self.running = False
        self.returncode = None
        self.started = None

    def start(self, task, commands, env_extra=None):
        with self.lock:
            if self.running:
                raise RuntimeError("이미 실행 중인 작업이 있습니다")
            self.running, self.task, self.returncode = True, task, None
            self.started = dt.datetime.now().strftime("%H:%M:%S")
            self.log.clear()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env_extra or {})}
        threading.Thread(target=self._run, args=(commands, env), daemon=True).start()

    def _run(self, commands, env):
        code = 0
        for cmd in commands:
            name = " ".join(cmd[2:])
            if cmd[2] == "autoto.rights_offering" and not env.get("DART_API_KEY"):
                self.log.append(f"[건너뜀] {name}: DART 인증키가 없습니다")
                continue
            self.log.append(f"$ python -m {name}")
            try:
                proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                for line in proc.stdout:
                    self.log.append(line.rstrip())
                code = proc.wait()
            except OSError as e:
                self.log.append(str(e))
                code = 1
            if code:
                self.log.append(f"[실패] 종료 코드 {code}")
                break
        with self.lock:
            self.running, self.returncode = False, code
        self.log.append("[완료]" if code == 0 else "[중단]")

    def status(self):
        return {"running": self.running, "task": self.task, "returncode": self.returncode,
                "started": self.started, "log": list(self.log)}


# ---------------------------------------------------------------------------


def make_handler(runner, output_dir):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/data":
                self._send(200, load_all(output_dir))
            elif path == "/api/status":
                self._send(200, {**runner.status(), "has_dart_key": bool(os.environ.get("DART_API_KEY"))})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/run":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                params = body.get("params") or {}
                commands = build_commands(body.get("task"), params)
                key = (params.get("dart_key") or "").strip()
                runner.start(body.get("task"), commands, {"DART_API_KEY": key} if key else None)
            except (ValueError, RuntimeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            self._send(200, runner.status())

        def log_message(self, fmt, *args):  # 요청마다 찍히는 로그는 생략
            pass

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Runner(), OUTPUT_DIR))
    url = f"http://127.0.0.1:{args.port}"
    print(f"autoto 홈페이지: {url}  (끄려면 Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
