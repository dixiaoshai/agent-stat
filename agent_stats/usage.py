"""Source-backed cache observations; unknown categories are never zero-filled."""


def _count(value):
    return type(value) is int and value >= 0


def aggregate_cache(turns, blocked=False):
    """Use paired input/cache samples for both quantities and their token ratio.

    Cache reads are a subset of input, never an additional token category.
    When accounting is unresolved the caller must label observed subtotals and
    the observed input ratio as potentially overlapping.
    """
    paired = [(t.get('input_tokens'), t.get('cached_input_tokens')) for t in turns]
    valid = [(inp, cached) for inp, cached in paired
             if _count(inp) and _count(cached) and cached <= inp]
    writes = [t['cache_write_input_tokens'] for t in turns
              if _count(t.get('cache_write_input_tokens'))]
    inputs = sum(inp for inp, _ in valid) if valid else None
    cached = sum(value for _, value in valid) if valid else None
    return {
        'cached_input_known': cached,
        'uncached_input_known': inputs - cached if valid else None,
        'input_known': inputs,
        'hit_rate': cached / inputs if inputs else None,
        'samples': len(valid),
        'turns': len(turns),
        'complete': bool(turns) and len(valid) == len(turns),
        'aggregation_blocked': blocked,
        'cache_write_known': sum(writes) if writes else None,
        'cache_write_samples': len(writes),
    }
