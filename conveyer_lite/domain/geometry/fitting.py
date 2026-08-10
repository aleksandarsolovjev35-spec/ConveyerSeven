import numpy as np
from itertools import combinations

_MAX_POINTS = 15


def split_top_row(role_points, expected_count=5):
    if len(role_points) <= expected_count:
        return role_points, []

    working = role_points
    pre_rejected = []

    if len(working) > _MAX_POINTS:
        sorted_by_y = sorted(working, key=lambda p: p[1])
        median_y = sorted_by_y[len(sorted_by_y) // 2][1]
        by_dist = sorted(working, key=lambda p: abs(p[1] - median_y))
        working = by_dist[:_MAX_POINTS]
        pre_rejected = by_dist[_MAX_POINTS:]

    best_combo = None
    best_error = float("inf")

    for combo in combinations(working, expected_count):
        pts = sorted(combo, key=lambda p: p[0])
        xs = np.array([p[0] for p in pts], dtype=np.float64)
        ys = np.array([p[1] for p in pts], dtype=np.float64)
        if len(xs) < 2:
            continue
        a, b = np.polyfit(xs, ys, 1)
        error = float(np.mean((ys - (a * xs + b)) ** 2))
        if error < best_error:
            best_error = error
            best_combo = list(combo)

    if best_combo is None:
        return role_points, []

    rejected = [p for p in working if p not in best_combo]
    rejected.extend(pre_rejected)
    return best_combo, rejected