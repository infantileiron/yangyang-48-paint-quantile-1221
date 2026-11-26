import pytest

from src.domain import analysis


def completed_batch(prefix, n, start_day=1, area0=10.0):
    rows = []
    for i in range(n):
        rows.append({
            'ticket': f'{prefix}-{i:02}', 'revision': 1,
            'day': f'2026-11-{start_day + i:02}',
            'area': area0 + (i % 7) * 1.5,
            'passes': (i % 4) + 1,
            'status': analysis.COMPLETED,
            'used': round(60 + 9.5 * (area0 + (i % 7) * 1.5) * ((i % 4) + 1) + (i % 5 - 2) * 3, 1),
        })
    return rows


PARAMS = {'quantile': 85, 'holdout': 2}


def test_latest_revision_wins_and_out_of_order_delivery():
    # 规则二：即使完工 v2 先到、待喷 v1 乱序后送，仍按版本号认最新完工，不会变回待喷。
    # 待喷日(11-04)早于完工日(11-05)，属先占料后补完工 -> 迟到回填，与入账顺序无关。
    rows = completed_batch('T', 12) + [
        {'ticket': 'X1', 'revision': 2, 'day': '2026-11-05', 'area': 20, 'passes': 2,
         'status': analysis.COMPLETED, 'used': 120.0},
        {'ticket': 'X1', 'revision': 1, 'day': '2026-11-04', 'area': 20, 'passes': 2,
         'status': analysis.PENDING, 'used': 0},
    ]
    result = analysis.analyze(rows, PARAMS)
    assert not any(row['ticket'] == 'X1' and row['kind'] == '待喷建议' for row in result['rows'])
    current = analysis.latest_per_ticket(rows)
    assert current['X1']['status'] == analysis.COMPLETED
    assert current['X1']['revision'] == 2
    backfill = [row for row in result['rows'] if row['ticket'] == 'X1']
    assert backfill and all(row['kind'] == '迟到完工回填' for row in backfill)


def test_stale_pending_with_later_day_is_rejected_not_backfilled():
    # 规则二：完工 v2 交接日更早；旧待喷 v1 交接日反而更晚，是被新版本否决的异常旧记录，
    # 不能把完工单改回待喷，也不构成迟到回填，按正常完工进入拟合。
    rows = completed_batch('S', 12) + [
        {'ticket': 'Z1', 'revision': 2, 'day': '2026-11-04', 'area': 20, 'passes': 2,
         'status': analysis.COMPLETED, 'used': 120.0},
        {'ticket': 'Z1', 'revision': 1, 'day': '2026-11-08', 'area': 20, 'passes': 2,
         'status': analysis.PENDING, 'used': 0},
    ]
    result = analysis.analyze(rows, PARAMS)
    assert not any(row['ticket'] == 'Z1' and row['kind'] == '迟到完工回填' for row in result['rows'])
    assert not any(row['ticket'] == 'Z1' and row['kind'] == '待喷建议' for row in result['rows'])
    assert analysis.latest_per_ticket(rows)['Z1']['status'] == analysis.COMPLETED


def test_missing_revision_defaults_to_one():
    rows = completed_batch('D', 12)
    rows[0].pop('revision')
    result = analysis.analyze(rows, PARAMS)
    assert result['metrics']


def test_duplicate_same_ticket_revision_rejected():
    rows = completed_batch('D', 12)
    rows.append(dict(rows[0]))
    with pytest.raises(ValueError, match='来源版本号'):
        analysis.analyze(rows, PARAMS)


def test_late_completion_is_backfilled_not_fitted():
    # 规则三/四：v1 待喷 -> v2 迟到完工，应回填验算且不进拟合/留出、不再占用备料。
    base = completed_batch('B', 14)
    baseline = analysis.analyze(base, PARAMS)
    rows = base + [
        {'ticket': 'L1', 'revision': 1, 'day': '2026-11-16', 'area': 15, 'passes': 2,
         'status': analysis.PENDING, 'used': 0},
        {'ticket': 'L1', 'revision': 2, 'day': '2026-11-22', 'area': 15, 'passes': 2,
         'status': analysis.COMPLETED, 'used': 95.0},
    ]
    result = analysis.analyze(rows, PARAMS)

    # 正常完工单仍是 14 张，留出指标不变 => 迟到单没有混进当天拟合/留出。
    holdout_baseline = [m for m in baseline['metrics'] if m['label'] == '留出实际覆盖率'][0]
    holdout_now = [m for m in result['metrics'] if m['label'] == '留出实际覆盖率'][0]
    assert holdout_now['value'] == holdout_baseline['value']

    backfill = [r for r in result['rows'] if r['kind'] == '迟到完工回填']
    assert len(backfill) == 1
    row = backfill[0]
    assert row['ticket'] == 'L1'
    assert row['revision'] == 'v2（按 v1 特征回填）'
    assert row['used'] == 95.0
    assert isinstance(row['planned'], (int, float))
    assert row['status'] in ('覆盖', '需补料')
    # 不再出现在待喷建议中。
    assert not any(r['ticket'] == 'L1' and r['kind'] == '待喷建议' for r in result['rows'])

    metrics = {m['label']: m['value'] for m in result['metrics']}
    assert metrics['迟到回填工单'] == 1
    assert metrics['迟到完工回填覆盖率'] in (0.0, 100.0)
    assert metrics['待喷工单'] == 0


def test_pending_ticket_still_reserves_material_with_version_label():
    # 规则一：纯待喷单仍占用备料；规则六：结果行带版本与备料量。
    rows = completed_batch('P', 12) + [
        {'ticket': 'W1', 'revision': 1, 'day': '2026-11-19', 'area': 22, 'passes': 3,
         'status': analysis.PENDING, 'used': 0},
        {'ticket': 'W2', 'revision': 3, 'day': '2026-11-19', 'area': 30, 'passes': 2,
         'status': analysis.PENDING, 'used': 0},
    ]
    result = analysis.analyze(rows, PARAMS)
    plans = {r['ticket']: r for r in result['rows'] if r['kind'] == '待喷建议'}
    assert set(plans) == {'W1', 'W2'}
    assert plans['W1']['revision'] == 'v1'
    assert plans['W2']['revision'] == 'v3'
    assert isinstance(plans['W1']['planned'], (int, float)) and plans['W1']['planned'] >= 0
    metrics = {m['label']: m['value'] for m in result['metrics']}
    assert metrics['待喷工单'] == 2
    assert metrics['待喷备料合计'] == round(plans['W1']['planned'] + plans['W2']['planned'], 2)
    # 规则六：结果列含工单版本与备料量。
    assert {'ticket', 'revision', 'planned'}.issubset({c['key'] for c in result['columns']})


def test_correction_to_completed_ticket_uses_latest_version():
    # 更正：同一完工单 v2 修正耗用，latest 取 v2，v1 不重复计数。
    rows = completed_batch('C', 12)
    rows.append({'ticket': 'C-03', 'revision': 2, 'day': rows[3]['day'],
                 'area': rows[3]['area'], 'passes': rows[3]['passes'],
                 'status': analysis.COMPLETED, 'used': round(rows[3]['used'] + 40, 1)})
    result = analysis.analyze(rows, PARAMS)
    c03 = [r for r in result['rows'] if r['ticket'] == 'C-03']
    assert all(r['revision'] == 'v2' for r in c03)
    # 工单数没有因版本叠加而翻倍。
    assert len({r['ticket'] for r in result['rows']}) <= 12
