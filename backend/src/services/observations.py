import csv
import hashlib
import io
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.project import SPEC
from src.domain import analysis
from src.models.workflow import AuditEvent, Observation
from src.services.errors import BusinessError
from src.services.validation import validated


MAX_OBSERVATIONS = 5000


def clean_observation(raw):
    row = validated(SPEC['fields'], raw)
    try:
        if hasattr(analysis, 'validate'):
            analysis.validate(row)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc
    return row


def content_key(row):
    return 'data:' + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parse_csv(raw):
    if len(raw) > 512 * 1024:
        raise BusinessError('CSV 文件上限 512 KB', 413)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
        header = reader.fieldnames or []
        known = {item['key'] for item in SPEC['fields']}
        required = {item['key'] for item in SPEC['fields'] if item.get('required', True)}
        if len(set(header)) != len(header) or not required.issubset(header) or not set(header).issubset(known):
            raise BusinessError('CSV 表头与导入模板不匹配')
        rows = []
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise BusinessError('列数与表头不匹配')
                rows.append(clean_observation(row))
            except BusinessError as exc:
                raise BusinessError(f'CSV 第 {reader.line_num} 行，{exc}') from exc
            if len(rows) > 500:
                raise BusinessError('单次导入上限 500 条')
        if not rows:
            raise BusinessError('CSV 里没有观测记录')
        return rows
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BusinessError('请上传 UTF-8 编码的有效 CSV') from exc


def add_observations(db, rows, actor, action):
    keyed = {content_key(row): row for row in rows}
    existing = set(db.scalars(select(Observation.source_key).where(Observation.source_key.in_(keyed))))
    fresh = {key: row for key, row in keyed.items() if key not in existing}
    if fresh:
        _ensure_revision_available(db, list(fresh.values()))
    if db.scalar(select(func.count(Observation.id))) + len(fresh) > MAX_OBSERVATIONS:
        raise BusinessError('当前项目最多保留 5000 条观测记录', 409)
    items = [Observation(source_key=key, payload=row) for key, row in fresh.items()]
    db.add_all(items)
    if items:
        db.add(AuditEvent(actor=actor, action=action, detail=f'新增 {len(items)} 条观测'))
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('有相同记录刚刚写入，请刷新后重试', 409) from exc
    return items


def _ensure_revision_available(db, fresh_rows):
    """同一工单的同一来源版本号只能入账一次；补报或更正必须递增版本号。"""
    wanted = {}
    for row in fresh_rows:
        marker = (row['ticket'], row.get('revision', 1))
        if marker in wanted:
            raise BusinessError(
                f'工单 {marker[0]} 的第 {marker[1]} 版在本次提交中重复，请递增来源版本号', 409)
        wanted[marker] = True
    for payload in db.scalars(select(Observation.payload)):
        marker = (payload['ticket'], payload.get('revision', 1))
        if marker in wanted:
            raise BusinessError(
                f'工单 {marker[0]} 的第 {marker[1]} 版已入账，补报或更正请改用更大的来源版本号', 409)


def current_observation_ids(db):
    """返回每个工单最新来源版本对应观测的主键（按主键排序），用于提交时固化快照。

    已排队的分析在提交时冻结该清单，事后到达的更正不会改变历史任务；
    只有新建的分析才会取到更正后的最新版本。
    """
    latest = {}
    for obs in db.scalars(select(Observation).order_by(Observation.id)):
        ticket = obs.payload['ticket']
        revision = obs.payload.get('revision', 1)
        if ticket not in latest or revision > latest[ticket][0]:
            latest[ticket] = (revision, obs.id)
    return sorted(obs_id for _, obs_id in latest.values())
