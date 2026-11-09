"""Current standard short-context API equivalents, never actual account charges."""
from decimal import Decimal


PRICING = {
    'currency': 'USD',
    'checked_at': '2026-11-08',
    'source_url': 'https://developers.openai.com/api/docs/pricing?latest-pricing-expanded=true',
    'basis': '当前 API Standard 短上下文价格',
    'assumptions': [
        '按当前单价估算历史 Token 的 API 等价费用，不是实际账单或订阅扣费。',
        '按 Standard 短上下文费率估算；轮次聚合无法还原逐请求长上下文加价或实际服务等级。',
        '未记录缓存写入时按无单独计费写入估算；已记录写入但缺少 TTL 时无法估算。',
        '普通输入 = 输入 − 缓存输入；推理已包含在输出中，不再相加。不含工具等附加费。',
        '无匹配公开单价或缺少有效分类用量时保留未知；覆盖不完整时仅展示已知小计。',
        '父子按各会话新增用量合计；分叉历史基线无法确认时保留未知。',
    ],
    # USD per million tokens. Exact model IDs only: no guessed family aliases.
    'models': {
        'gpt-6-astra': {'input': 10, 'cached_input': 1, 'output': 50},
        'gpt-6.1-sol': {'input': 2, 'cached_input': .10, 'output': 10},
        'gpt-6.1-terra': {'input': 1, 'cached_input': .05, 'output': 5},
        'gpt-6-luna': {'input': .10, 'cached_input': .01, 'output': .50},
    },
}


def _amount(value):
    # Keep sub-cent values; round only floating-point transport noise.
    return float(round(value, 12))


def estimate_turn(turn):
    """Enrich a copy at query time, so price changes do not require reimports."""
    result = dict(turn, estimated_cost=None, estimated_cost_reason=None, cost_breakdown=None)

    def unavailable(reason):
        result['estimated_cost_reason'] = reason
        return result

    rates = PRICING['models'].get(turn.get('model'))
    if rates is None:
        return unavailable('模型没有匹配的公开单价')
    fields = [turn.get(k) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')]
    if any(type(v) is not int or v < 0 for v in fields):
        return unavailable('缺少有效的输入、缓存输入或输出用量')
    inp, cached, output = fields
    if cached > inp:
        return unavailable('缓存输入大于输入用量，分类不一致')
    total = turn.get('token_total')
    if total is not None and (type(total) is not int or total != inp + output):
        return unavailable('Token 总量与输入、输出分类不一致')
    writes = turn.get('cache_write_input_tokens')
    if turn.get('cache_write_observed'):
        return unavailable('已观察到缓存写入，但缺少写入 TTL，无法匹配费率')
    if writes is not None:
        if type(writes) is not int or writes < 0:
            return unavailable('缓存写入用量无效')
        if writes > 0:
            return unavailable('已记录缓存写入，但缺少写入 TTL，无法匹配费率')
    counts = {'input': inp - cached, 'cached_input': cached, 'output': output}
    amounts = {key: Decimal(counts[key]) * Decimal(str(rates[key])) / 1000000 for key in counts}
    result['estimated_cost'] = _amount(sum(amounts.values()))
    result['cost_breakdown'] = {key: _amount(value) for key, value in amounts.items()}
    return result


def aggregate_cost(turns, blocked=False):
    values = [t['estimated_cost'] for t in turns if t.get('estimated_cost') is not None]
    known = _amount(sum((Decimal(str(v)) for v in values), Decimal(0))) if values else None
    return {
        'estimated_cost': known if values and len(values) == len(turns) and not blocked else None,
        'estimated_cost_known': None if blocked else known,
        'observed_estimated_cost_known': known,
        'cost_samples': len(values),
    }
