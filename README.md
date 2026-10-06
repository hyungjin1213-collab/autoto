# autoto — 종합 투자 분석

한국 주식(코스피·코스닥)을 여러 관점에서 걸러 보는 도구 모음입니다.
GitHub Actions가 평일 아침마다 돌려서 결과 CSV를 `output/`에 커밋합니다.

| 모듈 | 하는 일 | 결과 (`output/…`) |
|---|---|---|
| `autoto.turnover` | 회전율(거래량/상장주식수) 순위, 5·20·60일 평균, 급증배수 | `turnover/turnover_today.csv`, `turnover/turnover_history.csv` |
| `autoto.monthly_candle` | 월봉매매법 후보 선별 + 백테스트 | `monthly_candle/candidates.csv`, `monthly_candle/backtest_*.csv` |
| `autoto.rights_offering` | DART 유상증자 중 제3자배정 공시와 배정대상자 수집 | `rights_offering/third_party_allotments.csv`, `rights_offering/allottees.csv` |

CSV는 UTF-8 BOM이라 엑셀에서 바로 열립니다.

## 홈페이지 (내 컴퓨터에서)

```bash
python -m autoto.web        # 브라우저에서 http://127.0.0.1:8000 이 열립니다
```

윈도우는 `start.bat`, 맥은 `start.sh` 를 더블클릭해도 됩니다 (필요한 패키지 설치 후 실행).

| 탭 | 내용 |
|---|---|
| 종목 검색 | 종목명·코드 하나로 회전율, 월봉매매 후보 여부, 유상증자 공시를 한 화면에 |
| 회전율 | 급증 종목과 당일 전체 순위 (정렬·검색) |
| 월봉매매 | 이번 달 후보와 매도목표가 |
| 백테스트 | 누적수익률 그래프와 기준선 비교 |
| 유상증자 | 제3자배정 공시와 배정대상자, DART 원문 링크 |
| 실행 | 버튼으로 분석 실행, 조건 변경, 진행 로그 |

`output/` 폴더의 CSV를 읽어서 보여주므로 GitHub Actions가 만든 결과를 `git pull` 로 받아 바로 볼 수도 있습니다.
이 컴퓨터에서만 접속되며(127.0.0.1), 화면에 넣은 DART 인증키는 저장하지 않습니다.

## 월봉매매법

월봉이 **횡보**하는데 매달 **(고가 − 시가) / 시가 ≥ 5%** 가 꾸준히 나온 종목을
**월초 첫 거래일 시가에 매수**하고, **매일 매수가 +5%에 지정가 매도**를 걸어두는 전략입니다.

| 조건 | 기본값 | 옵션 |
|---|---|---|
| 선별에 쓰는 기간 | 직전 6개월 | `--lookback 6` |
| 목표 수익률 | 5% | `--target 0.05` |
| 직전 기간 중 목표 도달한 달 비율 | 100% (6개월 모두) | `--min-hit 1.0` (`0.8` = 6개월 중 5개월) |
| 횡보폭: 월말 종가 최고/최저 − 1 | 25% 이하 | `--max-range 0.25` |
| 일평균 거래대금 | 1억원 이상 | `--min-amount 1e8` |
| 주가 | 1,000원 이상 | `--min-price 1000` |
| 왕복 거래비용 (수수료+거래세) | 0.25% | `--cost 0.0025` |

매매 규칙:
- 목표가 = 매수가 × 1.05를 **호가단위로 올림**한 가격 (`매도목표가` 열 그대로 주문하면 됩니다)
- 그달 중 고가가 목표가에 닿으면 목표가에 매도. 그날 시가가 이미 목표가 위면 시가에 체결
- 끝까지 못 닿으면 **그달 마지막 거래일 종가에 매도**

### 백테스트로 검증하기

```bash
python -m autoto.monthly_candle backtest --years 5
```

- 매달 **그 이전 달까지의 데이터만으로** 종목을 고릅니다. 미래 정보는 쓰지 않습니다.
- 같은 매매 규칙을 선별 없이 전 종목에 적용한 **기준선**과 나란히 보여줍니다.
  전략의 목표도달률과 평균수익률이 기준선보다 높아야 선별이 의미가 있습니다.
- `backtest_summary.csv`: 거래수, 목표도달률, 평균·중앙 수익률, 못 닿았을 때 평균 손익, 누적수익률, 최대낙폭
- `backtest_monthly.csv`: 월별 전략 수익률(선별 종목 동일가중, 후보 없는 달은 0), 기준선, 선별 종목 수
- `backtest_trades.csv`: 종목·월별 거래 전부

주의할 점:
- **생존 편향**: 종목 목록이 현재 상장사 기준이라 그 사이 상장폐지된 종목은 빠져 있어 결과가 좋게 나옵니다.
- 월초 시가에 실제로 그 가격에 살 수 있다고 가정합니다. 거래량이 적은 종목은 체결이 어려울 수 있습니다.
- 하루 중 고가와 저가 중 어느 쪽이 먼저 왔는지는 일봉으로 알 수 없지만, 이 전략은 손절이 없어 결과에 영향이 없습니다.

## 실행

### GitHub에서

1. (유상증자용) https://opendart.fss.or.kr 에서 인증키를 받아
   **Settings → Secrets and variables → Actions** 에 `DART_API_KEY` 로 추가합니다.
   키가 없으면 매일 실행에서 유상증자만 건너뜁니다.
2. **Actions → Autoto → Run workflow** 에서 작업을 고릅니다.
   - `daily`: 회전율 + 이번 달 월봉 후보 + 유상증자 (평일 07:47 자동 실행)
   - `monthly_backtest`: 월봉매매법 백테스트 (전 종목 시세를 받으므로 수십 분 걸립니다)
   - `extra` 칸에 `--min-hit 0.8 --max-range 0.3` 처럼 조건을 바꿔 넣을 수 있습니다.
3. 결과는 실행 화면의 Artifact와 `output/` 폴더에 올라옵니다.

### 로컬에서

```bash
pip install -r requirements.txt
python -m autoto.turnover --top 100
python -m autoto.monthly_candle screen                   # 이번 달 후보
python -m autoto.monthly_candle screen --month 2026-11   # 다음 달 후보 (전월 마감 후)
python -m autoto.monthly_candle backtest --years 5 --min-hit 0.8
python -m autoto.monthly_candle screen --codes 005930 000660   # 특정 종목만
DART_API_KEY=발급받은키 python -m autoto.rights_offering --days 365
python -m pytest -q
```

시세는 FinanceDataReader(KRX 종목 목록, 네이버 일봉, 수정주가)로 받고 `.cache/prices/`에 12시간 동안 캐시합니다.
