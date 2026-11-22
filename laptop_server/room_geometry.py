"""Geometry for editable room outlines and obstacles, in metres."""

import math


CURVE_STEPS = 20
EPSILON = 1e-8


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(a, b, point):
    return (abs(cross(a, b, point)) <= EPSILON
            and all(min(a[i], b[i]) - EPSILON <= point[i] <= max(a[i], b[i]) + EPSILON
                    for i in range(2)))


def intersects(a, b, c, d):
    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return True
    return (on_segment(a, b, c) or on_segment(a, b, d)
            or on_segment(c, d, a) or on_segment(c, d, b))


def edges(points):
    return zip(points, points[1:] + points[:1])


def in_polygon(points, point, *, boundary=True):
    x, z = point
    inside = False
    for a, b in edges(points):
        if on_segment(a, b, point):
            return boundary
        if (a[1] > z) != (b[1] > z):
            if x < a[0] + (z - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                inside = not inside
    return inside


def sector_bounds(room):
    if room.get("shape") == "custom":
        xs, zs = zip(*room["boundary"])
        return min(xs), min(zs), max(xs), max(zs)
    return 0.0, 0.0, room["width"], room["depth"]


def contains(room, point):
    x, z = point
    return in_polygon(room["boundary"], point) and not any(
        x0 - EPSILON <= x <= x1 + EPSILON and z0 - EPSILON <= z <= z1 + EPSILON
        for x0, z0, x1, z1 in room["cutouts"])


def finite_coordinates(value, count):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("Проверьте координаты на плане")
    if not all(type(v) in (int, float) and 0 <= v <= 50 for v in value):
        raise ValueError("Координаты на плане должны быть от 0 до 50 м")
    return value


def custom_geometry(outline, curves, cutouts):
    if not isinstance(outline, list) or not 3 <= len(outline) <= 32:
        raise ValueError("У контура должно быть от 3 до 32 углов")
    if not isinstance(curves, list) or len(curves) != len(outline):
        raise ValueError("Проверьте изгибы стен")
    if not isinstance(cutouts, list) or len(cutouts) > 12:
        raise ValueError("На плане может быть до 12 вырезов")
    outline = [finite_coordinates(p, 2) for p in outline]
    curves = [None if c is None else finite_coordinates(c, 2) for c in curves]
    cutouts = [finite_coordinates(rect, 4) for rect in cutouts]

    boundary = []
    for (a, b), control in zip(edges(outline), curves):
        if math.dist(a, b) < .01:
            raise ValueError("Разведите соседние углы хотя бы на 1 см")
        boundary.append(a)
        if control is not None:
            for step in range(1, CURVE_STEPS):
                t = step / CURVE_STEPS
                u = 1 - t
                boundary.append([u*u*a[i] + 2*u*t*control[i] + t*t*b[i] for i in range(2)])

    walls = list(edges(boundary))
    for i, (a, b) in enumerate(walls):
        if any(intersects(a, b, c, d) for c, d in walls[
                i + 2:len(walls) - (i == 0)]):
            raise ValueError("Стены пересекаются. Поправьте углы или изгибы")

    width, depth = (round(max(p[i] for p in boundary), 8) for i in range(2))
    if width < 1 or depth < 1:
        raise ValueError("Размеры комнаты по каждой оси должны быть не меньше 1 м")
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in walls)) / 2
    for i, (x0, z0, x1, z1) in enumerate(cutouts):
        if x1 - x0 < .05 - EPSILON or z1 - z0 < .05 - EPSILON:
            raise ValueError("Вырез должен быть не меньше 5 × 5 см")
        corners = [[x0, z0], [x1, z0], [x1, z1], [x0, z1]]
        if (not all(in_polygon(boundary, p, boundary=False) for p in corners)
                or any(intersects(a, b, c, d) for a, b in edges(corners) for c, d in walls)):
            raise ValueError("Вырез должен целиком находиться внутри контура")
        if any(x0 <= u1 and x1 >= u0 and z0 <= v1 and z1 >= v0
               for u0, v0, u1, v1 in cutouts[:i]):
            raise ValueError("Разведите вырезы, чтобы они не касались друг друга")
        area -= (x1 - x0) * (z1 - z0)
    if area < .01:
        raise ValueError("Оставьте не меньше 0,01 м² внутри контура")

    return {"width": width, "depth": depth, "outline": outline, "curves": curves,
            "cutouts": cutouts, "boundary": boundary, "sketch_area": round(area, 3)}
