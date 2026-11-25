import json
from datetime import date, timedelta
from math import isfinite
from pathlib import Path
import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye, hstack

_SPEC = json.loads(Path(__file__).with_name("spec.json").read_text(encoding="utf-8"))


def _validate_fields(values, fields, *, defaults=False):
    result = {}
    for field in fields:
        key = field["key"]
        value = values.get(key, field.get("default") if defaults else None)
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
    if len({row[identity] for row in clean}) != len(clean):
        raise ValueError(f"存在重复的{next(field['label'] for field in _SPEC['fields'] if field['key'] == identity)}，请检查单据")
    params = _validate_fields(parameters, _SPEC["parameters"], defaults=True)
    return sorted(clean, key=lambda row: (row.get("day", ""), row[identity])), params


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


def validate(row):
    clean = _validate_fields(row, _SPEC["fields"])
    if (clean["used"] == 0) != (clean["status"] == PENDING):
        raise ValueError("待喷须填 0 毫升，已完工须填正耗用")


COMPLETED, PENDING = "已完工", "待喷"
COLUMNS = (("ticket", "喷涂工单"), ("day", "交接日"), ("kind", "样本类型"),
           ("load", "涂覆总面积（dm²）"), ("passes", "遍数"), ("used", "完工耗用（mL）"),
           ("planned", "建议备料（mL）"), ("status", "复核说明"))


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
    completed = [row for row in rows if row["status"] == COMPLETED]
    pending = [row for row in rows if row["status"] == PENDING]
    train, evaluation = _split_days(completed, opts["holdout"], 4)
    tau = opts["quantile"] / 100
    tested_coefficients = _fit(train, tau)
    production_coefficients = _fit(completed, tau)

    def detail(group, training, beta, kind):
        columns = _design(training)
        lo, hi = columns.min(axis=0), columns.max(axis=0)
        features = _design(group)
        for row, x in zip(group, features):
            raw = max(0.0, float(x @ beta))
            out_of_range = bool(np.any((x < lo) | (x > hi)))
            status = "外推，试喷确认" if out_of_range else ("需补料" if row["used"] > raw + 1e-8 else "覆盖")
            if row["status"] == PENDING and not out_of_range:
                status = "备料复核"
            yield {"ticket": row["ticket"], "day": row["day"], "kind": kind, "load": _shown(x[1]),
                   "passes": row["passes"], "used": row["used"] if row["status"] == COMPLETED else "—",
                   "planned": _shown(raw), "status": status}

    test = list(detail(evaluation, train, tested_coefficients, "时间留出"))
    plans = list(detail(pending, completed, production_coefficients, "待喷建议"))
    actual = np.array([row["used"] for row in evaluation])
    estimate = np.maximum(0, _design(evaluation) @ tested_coefficients)
    error = actual - estimate
    hits = sum(error <= 1e-8)
    loss = np.maximum(tau * error, (tau - 1) * error).mean()
    summary = (f"在较早的 {len(train)} 张完工单拟合 {opts['quantile']}% 用量，末 {len(test)} 单有 {hits} 单被报量覆盖。"
               "待喷备料建议使用全部已知完工单重训，超出历史涂覆量和遍数时请先试喷。")
    return _output("比例布景喷涂备料建议", summary, COLUMNS, plans + test,
                   [("留出实际覆盖率", _shown(100 * hits / len(test)), "%"), ("留出 Pinball 损失", _shown(loss), "mL"),
                    ("待喷工单", len(plans), "单")],
                   "各待喷工单估计用量分位", "mL", [(row["ticket"], row["planned"]) for row in plans])


def seed():
    rows = []
    jitter = (-11, -7, -6, -5, -1, 2, 4, 9, 18, 25, -2)
    for i in range(42):
        passes = i % 5 + 1
        area = 8 + ((i * 11) % 29) + (i % 4) * 0.5
        used = 14 + 3.8 * area * passes + 5.5 * passes + jitter[i % len(jitter)]
        rows.append({"ticket": f"YH-{i+1:03}", "day": _day("2026-11-01", i // 3),
                     "area": area, "passes": passes, "status": COMPLETED, "used": round(used, 1)})
    for i, (area, passes) in enumerate(((28, 2), (33.5, 4), (88, 4)), 1):
        rows.append({"ticket": f"YH-P{i:02}", "day": "2026-11-21", "area": area, "passes": passes, "status": PENDING, "used": 0})
    return rows
