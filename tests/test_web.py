import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from autoto import web


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def test_read_dataset_keeps_leading_zeros_and_nulls(tmp_path):
    write(tmp_path / "turnover/turnover_today.csv", "종목코드,종목명,회전율\n005930,삼성전자,1.5\n000660,SK하이닉스,\n")
    ds = web.read_dataset(tmp_path / "turnover/turnover_today.csv")
    assert ds["columns"] == ["종목코드", "종목명", "회전율"]
    assert ds["rows"] == [["005930", "삼성전자", 1.5], ["000660", "SK하이닉스", None]]
    assert web.read_dataset(tmp_path / "missing.csv") is None


def test_summary_index_column_is_named(tmp_path):
    write(tmp_path / "s.csv", ",전략(선별),기준선(전종목)\n거래수,3,9\n")
    ds = web.read_dataset(tmp_path / "s.csv", summary=True)
    assert ds["columns"][0] == "항목" and ds["rows"][0][0] == "거래수"


def test_build_commands():
    daily = web.build_commands("daily", {"month": "2026-11", "min_hit": "0.8"})
    assert [c[2] for c in daily] == ["autoto.turnover", "autoto.monthly_candle", "autoto.rights_offering"]
    assert daily[1][3:] == ["screen", "--month", "2026-11", "--min-hit", "0.8"]
    bt = web.build_commands("monthly_backtest", {"years": "3", "lookback": "4"})
    assert bt[0][3:] == ["backtest", "--years", "3", "--lookback", "4"]


@pytest.mark.parametrize("task,params", [
    ("rm", {}),
    ("monthly_screen", {"month": "2026-11; rm -rf /"}),
    ("monthly_backtest", {"years": "abc"}),
    ("monthly_screen", {"min_hit": "2"}),
])
def test_build_commands_rejects_bad_input(task, params):
    with pytest.raises(ValueError):
        web.build_commands(task, params)


def wait_done(runner):
    for _ in range(100):
        if not runner.running:
            return
        time.sleep(0.05)
    raise AssertionError("runner did not finish")


def test_runner_runs_commands_in_order_and_stops_on_failure(tmp_path):
    runner = web.Runner(cwd=tmp_path)
    ok = [sys.executable, "-c", "print('first')"]
    fail = [sys.executable, "-c", "import sys; sys.exit(3)"]
    never = [sys.executable, "-c", "print('never')"]
    runner.start("x", [ok, fail, never])
    wait_done(runner)
    st = runner.status()
    assert st["returncode"] == 3
    assert "first" in st["log"] and "never" not in st["log"]


def test_runner_skips_rights_offering_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    runner = web.Runner(cwd=tmp_path)
    runner.start("rights_offering", web.build_commands("rights_offering", {}))
    wait_done(runner)
    assert runner.status()["returncode"] == 0
    assert any("건너뜀" in line for line in runner.status()["log"])


def test_http_api(tmp_path):
    write(tmp_path / "monthly_candle/candidates.csv", "종목코드,종목명,적중률\n123456,에이,1.0\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(web.Runner(cwd=tmp_path), tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "autoto" in page
        data = json.load(urllib.request.urlopen(base + "/api/data"))
        assert data["candidates"]["rows"] == [["123456", "에이", 1.0]]
        assert data["rights"] is None
        req = urllib.request.Request(base + "/api/run", data=json.dumps({"task": "nope"}).encode(), method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
    finally:
        server.shutdown()
