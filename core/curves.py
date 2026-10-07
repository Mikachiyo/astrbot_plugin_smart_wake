"""风扇曲线：点列 [(x, y), ...] 之间的线性插值。"""


def normalize_points(points) -> list[list[float]]:
    """清洗点列：转成 [x, y] 浮点对、按 x 排序、去掉非法项。"""
    cleaned = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        cleaned.append([x, y])
    cleaned.sort(key=lambda p: p[0])
    # 相同 x 只保留最后一个
    dedup: list[list[float]] = []
    for p in cleaned:
        if dedup and dedup[-1][0] == p[0]:
            dedup[-1] = p
        else:
            dedup.append(p)
    return dedup


def interp(points, x: float) -> float:
    """对点列做线性插值，x 超出范围时取端点值（封顶/封底）。"""
    pts = normalize_points(points)
    if not pts:
        return 0.0
    if len(pts) == 1 or x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        x1, y1 = pts[i - 1]
        x2, y2 = pts[i]
        if x1 <= x <= x2:
            if x2 == x1:
                return y2
            t = (x - x1) / (x2 - x1)
            return y1 + t * (y2 - y1)
    return pts[-1][1]
