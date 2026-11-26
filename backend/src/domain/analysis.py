import json
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye, hstack

_SPEC = json.loads(Path(__file__).with_name("spec.json").read_text(encoding="utf-8"))

COMPLETED, PENDING = "已完工", "待喷"

COLUMNS = (
    ("ticket", "喷涂工单"),
    ("revision", "工单版本"),
    ("day", "交接日"),
    ("kind", "样本类型"),
    ("load", "涂覆总面积（dm²）"),
    ("passes", "遍数"),
    ("used", "完工耗用（mL）"),
    ("planned", "建议备料（mL）"),
    ("status", "复核说明"),
)


def _validate_fields(values, fields, *, defaults=False):
    result = {}
    for field in fields:
        key = field["key"]
        optional = not field.get("required", True)
        if key not in values or values[key] is None:
            # 可选字段带默认值时（如来源版本号），无论是否处于参数默认模式都回落到默认值。
            if optional and "default" in field:
                value = field["default"]
            else:
                value = field.get("default") if defaults else None
        else:
            value = values[key]
        kind = field["type"]
        if kind == "number" or kind == "integer":
            valid = type(value) in (int, float) and isfinite(value) and field["min"] <= value <= field["max"]
            valid = valid and (kind != "integer" or int(value) == value)
            if valid:
                value = int(value) if kind == "integer" else float(value)
        elif kind == "choice":
            valid = value in field["choices"]
        else:
            valid = isinstance(value, str) and 0 < len(value.strip()) <= 300
            if valid and kind == "date":
                try:
                    valid = date.fromisoformat(value).isoformat() == value
                except ValueError:
                    valid = False
            if valid:
                value = value.strip()
        if not valid:
            raise ValueError(f"「{field['label']}」格式或范围无效")
        result[key] = value
    return result


def _inputs(rows, parameters, identity="ticket"):
    clean = [_validate_fields(row, _SPEC["fields"]) for row in rows]
    # 同一工单的同一来源版本号只能入账一次，重复登记直接拒绝。
    seen = set()
    for row in clean:
        marker = (row[identity], row["revision"])
        if marker in seen:
            raise ValueError(
                f"工单 {row[identity]} 的第 {row['revision']} 版已存在，"
                "补报或更正请改用更大的来源版本号"
            )
        seen.add(marker)
    params = _validate_fields(parameters, _SPEC["parameters"], defaults=True)
    return clean, params


def latest_per_ticket(rows):
    """每个工单只保留来源版本号最大的那条；旧版本即使乱序后送也不参与。"""
    latest = {}
    for row in rows:
        current = latest.get(row["ticket"])
        if current is None or row["revision"] > current["revision"]:
            latest[row["ticket"]] = row
    return dict(sorted(latest.items(), key=lambda item: (item[1]["day"], item[0])))


def _versions_of(rows, ticket):
    return sorted((row for row in rows if row["ticket"] == ticket),
                  key=lambda row: row["revision"])


def validate(row):
    clean = row if "revision" in row else {**row, "revision": 1}
    if (clean["used"] == 0) != (clean["status"] == PENDING):
        raise ValueError("待喷须填 0 毫升，已完工须填正耗用")


def _split_days(rows, holdout, minimum):
    days = sorted({row["day"] for row in rows})
    if len(days) < holdout + minimum:
        raise ValueError(f"时间验证需要至少 {holdout + minimum} 个已完成日期")
    cutoff = days[-holdout]
    return [row for row in rows if row["day"] < cutoff], [row for row in rows if row["day"] >= cutoff]


def _day(start, offset):
    return (date.fromisoformat(start) + timedelta(days=offset)).isoformat()


def _shown(value, places=2):
    return "—" if value is None else round(float(value), places)


def _output(headline, summary, columns, rows, metrics, chart_name, chart_unit, chart_data):
    return {
        "headline": headline,
        "summary": summary,
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": rows,
        "metrics": [{"label": label, "value": value, "unit": unit} for label, value, unit in metrics],
        "chart": {
            "label": chart_name, "unit": chart_unit,
            "points": [{"label": str(label), "value": value} for label, value in chart_data],
        },
    }


def _design(rows):
    return np.array([(1, row["area"] * row["passes"], row["passes"]) for row in rows])


def _fit(rows, tau):
    x = _design(rows)
    y = np.array([row["used"] for row in rows])
    if len(rows) < 8 or np.linalg.matrix_rank(x) < x.shape[1]:
        raise ValueError("训练样本须至少有八份已完工耗用，且面积遍数和遍数不可共线")
    identity = eye(len(x), format="csr")
    options = np.r_[np.zeros(x.shape[1]), np.full(len(x), tau), np.full(len(x), 1 - tau)]
    matrix = hstack((csr_matrix(x), identity, -identity), format="csr")
    model = linprog(options, A_eq=matrix, b_eq=y, bounds=[(None, None)] * x.shape[1] + [(0, None)] * (2 * len(x)),
                    method="highs")
    if not model.success:
        raise ValueError("当前记录无法拟合用量分位，请检查近似相同的工单")
    return model.x[:x.shape[1]]


def analyze(rows, parameters):
    rows, opts = _inputs(rows, parameters)
    for row in rows:
        validate(row)

    # 规则一/二：每单按来源版本号认最新；乱序补送的旧记录不会把完工单改回待喷。
    current = latest_per_ticket(rows)

    # 规则三/四：迟到的完工单只做单号回填验算，绝不混进当天拟合与留出。
    # 迟到补报判据：存在版本号更早、交接日不晚于当前完工日的待喷（先以待喷占料、后补完工）。
    # 与入账先后无关——乱序送达也按版本与日期识别。若旧待喷的交接日反而更晚，
    # 说明它是被新版本否决的异常旧记录，最新完工按正常单处理，不做回填。
    late, regular_completed, pending = [], [], []
    for ticket, row in current.items():
        history = _versions_of(rows, ticket)
        if row["status"] == PENDING:
            pending.append(row)
        else:
            basis_candidates = [
                version for version in history
                if version["status"] == PENDING
                and version["revision"] < row["revision"]
                and version["day"] <= row["day"]
            ]
            if basis_candidates:
                basis = max(basis_candidates, key=lambda version: version["revision"])
                late.append((row, basis))
            else:
                regular_completed.append(row)

    regular_completed.sort(key=lambda row: (row["day"], row["ticket"]))
    pending.sort(key=lambda row: (row["day"], row["ticket"]))

    train, evaluation = _split_days(regular_completed, opts["holdout"], 4)
    tau = opts["quantile"] / 100
    tested_coefficients = _fit(train, tau)
    # 备料建议只在当天收工前已入账的正常完工单上重训，迟到完工不进入。
    production_coefficients = _fit(regular_completed, tau)

    def build_row(group, training, beta, kind, feature_source=None):
        columns = _design(training)
        lo, hi = columns.min(axis=0), columns.max(axis=0)
        for item in group:
            if feature_source is not None:
                shown_row, basis_row = item
                basis_revision = basis_row["revision"]
            else:
                shown_row = basis_row = item
                basis_revision = item["revision"]
            x = _design([basis_row])[0]
            raw = max(0.0, float(x @ beta))
            out_of_range = bool(np.any((x < lo) | (x > hi)))
            if out_of_range:
                state = "外推，试喷确认"
            elif shown_row["status"] == COMPLETED:
                state = "需补料" if shown_row["used"] > raw + 1e-8 else "覆盖"
            else:
                state = "备料复核"
            yield {
                "ticket": shown_row["ticket"],
                "revision": f"v{shown_row['revision']}" + (f"（按 v{basis_revision} 特征回填）" if feature_source else ""),
                "day": shown_row["day"],
                "kind": kind,
                "load": _shown(x[1]),
                "passes": basis_row["passes"],
                "used": shown_row["used"] if shown_row["status"] == COMPLETED else "—",
                "planned": _shown(raw),
                "status": state,
            }

    test = list(build_row(evaluation, train, tested_coefficients, "时间留出"))
    plans = list(build_row(pending, regular_completed, production_coefficients, "待喷建议"))
    backfill = list(build_row(late, regular_completed, production_coefficients, "迟到完工回填",
                              feature_source=True))

    actual = np.array([row["used"] for row in evaluation])
    estimate = np.maximum(0, _design(evaluation) @ tested_coefficients)
    error = actual - estimate
    hits = int((error <= 1e-8).sum())
    loss = np.maximum(tau * error, (tau - 1) * error).mean()

    def covered(rows_out):
        covered_count = 0
        for entry in rows_out:
            covered_count += int(entry["used"] != "—" and entry["used"] <= entry["planned"] + 1e-8)
        return covered_count

    late_hits = covered(backfill)
    late_rate = _shown(100 * late_hits / len(backfill)) if backfill else "—"
    total_planned = round(sum(row["planned"] for row in plans), 2)

    summary = (
        f"按来源版本号取每单最新记录，共 {len(current)} 单（{len(regular_completed)} 单正常完工、"
        f"{len(pending)} 单待喷、{len(backfill)} 单迟到完工）。当天拟合与留出仅使用收工前已入账的 "
        f"{len(regular_completed)} 张正常完工单：较早 {len(train)} 张拟合 {opts['quantile']}% 用量，"
        f"末 {len(test)} 单有 {hits} 单被报量覆盖。迟到完工单不进入拟合，按单号用待喷时版本的特征回填验算。"
        "待喷备料建议在全部正常完工单上重训，超出历史涂覆量和遍数时请先试喷。"
    )
    metrics = [
        ("留出实际覆盖率", _shown(100 * hits / len(test)), "%"),
        ("留出 Pinball 损失", _shown(loss), "mL"),
        ("迟到完工回填覆盖率", late_rate, "%"),
        ("待喷工单", len(plans), "单"),
        ("待喷备料合计", total_planned, "mL"),
        ("迟到回填工单", len(backfill), "单"),
    ]
    return _output(
        "比例布景喷涂备料建议", summary, COLUMNS, plans + backfill + test, metrics,
        "各待喷工单估计用量分位", "mL",
        [(row["ticket"], row["planned"]) for row in plans],
    )


def seed():
    rows = []
    jitter = (-11, -7, -6, -5, -1, 2, 4, 9, 18, 25, -2)
    for i in range(42):
        passes = i % 5 + 1
        area = 8 + ((i * 11) % 29) + (i % 4) * 0.5
        used = 14 + 3.8 * area * passes + 5.5 * passes + jitter[i % len(jitter)]
        rows.append({"ticket": f"YH-{i + 1:03}", "revision": 1,
                     "day": _day("2026-11-01", i // 3),
                     "area": area, "passes": passes, "status": COMPLETED, "used": round(used, 1)})
    # 两张仍待喷的工单。
    for i, (area, passes) in enumerate(((33.5, 4), (88, 4)), 2):
        rows.append({"ticket": f"YH-P{i:02}", "revision": 1, "day": "2026-11-21",
                     "area": area, "passes": passes, "status": PENDING, "used": 0})
    # YH-P01 先以待喷入账（v1），随后补报完工（v2，迟到）：补报后不再占用备料，只做回填验算。
    rows.append({"ticket": "YH-P01", "revision": 1, "day": "2026-11-21",
                 "area": 28, "passes": 2, "status": PENDING, "used": 0})
    rows.append({"ticket": "YH-P01", "revision": 2, "day": "2026-11-25",
                 "area": 28, "passes": 2, "status": COMPLETED, "used": 238.0})
    return rows
