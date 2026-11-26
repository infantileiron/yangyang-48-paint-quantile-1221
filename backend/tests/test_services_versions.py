import pytest
from sqlalchemy import select

from src.constants.enums import RunStatus
from src.models.workflow import AnalysisRun, Observation
from src.schemas.requests import CreateRun
from src.services.errors import BusinessError
from src.services.observations import add_observations, clean_observation, current_observation_ids
from src.services.runs import create_run, run_pending

ANALYST = 'analyst@demo.local'


def obs(ticket, revision, status, used, day='2026-11-10', area=18.0, passes=2):
    return clean_observation({
        'ticket': ticket, 'revision': revision, 'day': day,
        'area': area, 'passes': passes, 'status': status, 'used': used,
    })


def completed_set(prefix, n, start_day=1):
    return [
        clean_observation({
            'ticket': f'{prefix}-{i:02}', 'revision': 1,
            'day': f'2026-11-{start_day + i:02}',
            'area': 12 + (i % 6) * 2.0, 'passes': (i % 4) + 1,
            'status': '已完工',
            'used': round(70 + 11 * (12 + (i % 6) * 2.0) * ((i % 4) + 1) + (i % 5 - 2) * 2, 1),
        })
        for i in range(n)
    ]


def test_clean_observation_defaults_revision_and_validates():
    row = clean_observation({
        'ticket': 'V1', 'day': '2026-11-10', 'area': 18.0, 'passes': 2,
        'status': '待喷', 'used': 0,
    })
    assert row['revision'] == 1
    explicit = clean_observation({
        'ticket': 'V1', 'revision': 3, 'day': '2026-11-10', 'area': 18.0, 'passes': 2,
        'status': '待喷', 'used': 0,
    })
    assert explicit['revision'] == 3


def test_same_ticket_same_revision_is_rejected(db):
    add_observations(db, [obs('A1', 1, '待喷', 0)], ANALYST, 'observation.created')
    with pytest.raises(BusinessError, match='来源版本号'):
        add_observations(db, [obs('A1', 1, '已完工', 120.0)], ANALYST, 'observation.created')


def test_higher_revision_supersedes_and_identical_rerun_skips(db):
    add_observations(db, [obs('A1', 1, '待喷', 0)], ANALYST, 'observation.created')
    add_observations(db, [obs('A1', 2, '已完工', 120.0)], ANALYST, 'observation.created')
    ids = current_observation_ids(db)
    payloads = list(db.scalars(select(Observation.payload).where(Observation.id.in_(ids))))
    assert len(payloads) == 1
    assert payloads[0]['revision'] == 2 and payloads[0]['status'] == '已完工'
    # 完全相同的记录重复导入仍按内容去重跳过，不会误报版本冲突。
    again = add_observations(db, [obs('A1', 2, '已完工', 120.0)], ANALYST, 'observation.imported')
    assert again == []


def test_queued_run_keeps_submission_snapshot_after_correction(db):
    # 规则五：更正入账后，已排队任务仍按提交时版本；新任务才使用更正。
    add_observations(db, completed_set('Q', 14), ANALYST, 'observation.imported')
    add_observations(db, [obs('LATE', 1, '待喷', 0, day='2026-11-16', area=16, passes=2)],
                     ANALYST, 'observation.created')

    request = CreateRun(title='首次分析', parameters={})
    first = create_run(db, request, ANALYST)
    first_ids = list(first.observation_ids)
    assert first.sample_size == 15  # 14 正常完工 + LATE 待喷 v1

    # 排队期间补报完工（更正），并修正一张正常完工单。
    add_observations(db, [obs('LATE', 2, '已完工', 98.0, day='2026-11-22', area=16, passes=2)],
                     ANALYST, 'observation.created')
    corrected = obs('Q-05', 2, '已完工', 500.0, day='2026-11-06', area=22.0, passes=3)
    add_observations(db, [corrected], ANALYST, 'observation.created')

    # 已排队任务的冻结清单不变（不含排队后到达的两条更正）。
    queued = db.get(AnalysisRun, first.id)
    assert list(queued.observation_ids) == first_ids
    assert queued.sample_size == 15
    assert queued.status == RunStatus.QUEUED

    # 执行老任务：迟到单仍是待喷（提交时所见），不出现回填。
    assert run_pending(db) is True
    queued = db.get(AnalysisRun, first.id)
    assert queued.status == RunStatus.SUCCEEDED
    kinds = {row['ticket']: row['kind'] for row in queued.result['rows']}
    assert kinds.get('LATE') == '待喷建议'
    assert not any(row['kind'] == '迟到完工回填' for row in queued.result['rows'])

    # 新任务冻结更正后的全部版本：含两条新记录，工单数不变。
    second = create_run(db, CreateRun(title='更正后分析', parameters={}), ANALYST)
    assert second.sample_size == 17  # 15 + LATE v2 + Q-05 v2（按版本冻结，工单仍为 15 单）
    assert run_pending(db) is True
    second_run = db.get(AnalysisRun, second.id)
    assert second_run.status == RunStatus.SUCCEEDED
    second_rows = {row['ticket']: row for row in second_run.result['rows']}
    assert second_rows['LATE']['kind'] == '迟到完工回填'
    # Q-05 是训练样本、不进结果明细；直接核对冻结快照解析出的最新版本为 v2。
    from src.domain.analysis import latest_per_ticket
    frozen = list(db.scalars(
        select(Observation.payload).where(Observation.id.in_(second.observation_ids))))
    assert latest_per_ticket(frozen)['Q-05']['revision'] == 2
    assert latest_per_ticket(frozen)['Q-05']['used'] == 500.0
