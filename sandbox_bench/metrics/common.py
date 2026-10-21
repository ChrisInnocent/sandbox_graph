"""Internal validation and interval operations shared by pure metrics."""
import math


def number(value, name='value', *, nonnegative=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite numeric')
    if nonnegative and value < 0:
        raise ValueError(f'{name} must be nonnegative')
    return value


def union(intervals, start, end):
    merged=[]
    for left,right in sorted(intervals):
        number(left); number(right)
        if right < left:
            raise ValueError('reversed interval')
        left,right=max(start,left),min(end,right)
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1]=(merged[-1][0],max(right,merged[-1][1]))
        else:
            merged.append((left,right))
    return merged


def duration(intervals):
    return sum(right-left for left,right in intervals)


def covered(intervals,left,right):
    return sum(max(0,min(right,b)-max(left,a)) for a,b in intervals)
