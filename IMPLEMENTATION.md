# Stock RL Trader Implementation

## Goal

실제 상장 종목의 현재가를 주기적으로 조회하고, 실제 주문 없이 자동 모의매매를 수행한다. 체결 시점 가격과 수익률을 기록하며, 해당 결과를 Q-learning reward로 사용한다.

## Current Behavior

- 로컬 Python 서버: `python run.py`
- 접속 주소: `http://127.0.0.1:8000`
- 자동 판단 주기: 5분
- 실제 비중 조정 쿨다운: 30분
- 자동 후보 교체 주기: 6시간
- 거래 가능 시간:
  - 한국 종목: KST 평일 09:00-15:30
  - 미국 종목: New York 평일 09:30-16:00
- 종목 수: 최대 8개
- 장부 저장: `data/runs/live_session.json`
- 실제 주문 API 없음

## UI

- shadcn/ui 계열의 neutral 카드, badge, table 스타일
- 티커와 종목명 동시 표시
- 총자산, 누적 수익률, 안전 정책 수익률, 실현 손익, 매도 승률, 판단 횟수 표시
- 종목별 미실현 손익 표기: 예 `삼성전자 +10.00% (+1,000)`
- 종목별 장중/장외 상태 표시
- 종목별 최근 가격 미니 차트 표시
- 자동 후보 교체 목록과 다음 교체 예정 시각 표시

## Automatic Symbol Rotation

AI API 없이도 자동 종목 교체는 가능하다. 현재 구현은 뉴스나 자연어를 해석하지 않고, Yahoo Finance 과거 가격 데이터를 기반으로 다음 정량 지표를 점수화한다.

- 최근 30일 수익률
- 최근 90일 수익률
- 최근 거래량
- 최근 변동성

6시간마다 후보를 다시 점수화하고 상위 8개를 `targetUniverse`로 저장한다. 기존 보유 종목이 새 후보에서 밀리면 새 목표 비중에서 제외되어 이후 리밸런싱 때 매도 대상으로 처리된다.

## Safety Policy v3

- 행동 공간: 보유, 현금 대기, 상위 1종목, 상위 3종목, 전체 동일비중
- reward: 시장 변동 수익률 - 직전 거래 비용 패널티 - 낙폭 패널티
- 탐색률: 프로필별 초기값에서 점진적으로 감소, 최저 2%
- 정책 기준 최대 낙폭 `-5%` 도달 시 2시간 현금 대기
- 손실 제한 휴식 종료 후 현재 현금 자산을 새 기준점으로 재시작
- 기존 Q-table은 `policyArchives`에 보관하고 새 Q-table을 학습
- 기존 체결 장부, 전체 누적 수익률, 실현 손익은 유지

## API

```text
GET  /api/health
GET  /api/live/state
POST /api/live/settings
POST /api/live/reset
POST /api/live/step
POST /api/symbols/suggest
POST /api/market-data
POST /api/backtest
POST /api/train
POST /api/advice
```
## Safety policy v4

- `ACTION_CASH` preserves current weights instead of targeting zero exposure.
- `_rebalance` blocks normal sells until the position has been held for 2 hours.
- `_rebalance` only allows profit exits at `+0.3%` or better and stop-loss exits at `-2.5%` or worse.
- `lastSymbolBuyAt` and `lastSymbolSellAt` are tracked and migrated from the ledger.
- Re-entry into a just-sold symbol is blocked for 2 hours.
