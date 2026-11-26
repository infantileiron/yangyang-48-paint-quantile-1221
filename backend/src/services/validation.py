import math
from datetime import date

from src.services.errors import BusinessError


def validated(fields, supplied, *, defaults=False):
    extra = set(supplied) - {item['key'] for item in fields}
    if extra:
        raise BusinessError(f'无法识别字段 {", ".join(sorted(extra))}')
    clean = {}
    for field in fields:
        key, label, kind = (field[prop] for prop in ('key', 'label', 'type'))
        optional = not field.get('required', True)
        if key not in supplied or supplied[key] is None or str(supplied[key]).strip() == '':
            # 可选且带默认值的字段（如来源版本号），或参数默认模式下的默认值，缺失时回落。
            if (optional and 'default' in field) or (defaults and 'default' in field):
                clean[key] = field['default']
                continue
            if field.get('required', True):
                raise BusinessError(f'请填写「{label}」')
            continue
        value = supplied[key]
        try:
            if kind in ('number', 'integer'):
                if isinstance(value, bool):
                    raise ValueError
                value = float(value)
                if not math.isfinite(value) or value < field.get('min', -1e12) or value > field.get('max', 1e12):
                    raise ValueError
                if kind == 'integer':
                    if not value.is_integer():
                        raise ValueError
                    value = int(value)
            elif kind == 'date':
                value = date.fromisoformat(str(value)).isoformat()
            elif kind == 'choice':
                if value not in field['choices']:
                    raise ValueError
            elif not isinstance(value, str) or len(value.strip()) > 300:
                raise ValueError
            else:
                value = value.strip()
        except (TypeError, ValueError, OverflowError) as exc:
            raise BusinessError(f'「{label}」的格式或取值范围不正确') from exc
        clean[key] = value
    return clean
