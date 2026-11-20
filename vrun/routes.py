# SPDX-License-Identifier: MPL-2.0
"""Route parsing and distance estimates. Coordinates are WGS-84 internally."""
from dataclasses import dataclass
from pathlib import Path
import ast
import math
from bisect import bisect_right
from collections.abc import Sequence
from functools import cached_property
from itertools import accumulate

from geopy.distance import geodesic

from .coordinates import bd09_to_wgs84


def route_kind(closed):
    return "闭合环线" if closed else "往返路线"


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float


def longitude_delta(start: float, end: float) -> float:
    delta = end - start
    return (delta + 180) % 360 - 180 if abs(delta) > 180 else delta


def interpolate(start: Point, end: Point, ratio: float) -> Point:
    if ratio == 1:
        return end
    longitude = start.lng + longitude_delta(start.lng, end.lng) * ratio
    if not -180 <= longitude <= 180:
        longitude = (longitude + 180) % 360 - 180
    return Point(start.lat + (end.lat - start.lat) * ratio, longitude)


@dataclass(frozen=True)
class Route:
    path: Path
    points: tuple[Point, ...]
    segment_lengths: tuple[float, ...]
    closed: bool = True

    @property
    def length_m(self) -> float:
        return self.cumulative_lengths[-1]

    @property
    def cycle_length_m(self) -> float:
        return self.length_m if self.closed else 2 * self.length_m

    @property
    def kind(self) -> str:
        return route_kind(self.closed)

    @property
    def distance_summary(self) -> str:
        if self.closed:
            return f"每圈约 {self.length_m:.0f} 米"
        return f"单程约 {self.length_m:.0f} 米 · 往返约 {self.cycle_length_m:.0f} 米"

    def geometry_fraction(self, cycle_fraction: float) -> float:
        return cycle_fraction if self.closed else 2 * min(cycle_fraction, 1 - cycle_fraction)

    def cycle_position_at(self, distance_m: float) -> Point:
        return self.position_at(distance_m if self.closed else min(distance_m, self.cycle_length_m - distance_m))

    @cached_property
    def cumulative_lengths(self) -> tuple[float, ...]:
        return tuple(accumulate(self.segment_lengths, initial=0.0))

    def position_at(self, distance_m: float) -> Point:
        """Interpolate by travelled arc length, independent of route point density."""
        if not math.isfinite(distance_m):
            raise ValueError("路线距离必须是有限数值")
        if distance_m <= 0:
            return self.points[0]
        if distance_m >= self.length_m:
            return self.points[-1]
        segment = bisect_right(self.cumulative_lengths, distance_m) - 1
        start, end = self.points[segment:segment + 2]
        ratio = (distance_m - self.cumulative_lengths[segment]) / self.segment_lengths[segment]
        return interpolate(start, end, ratio)

    def samples(self, speed: float, interval: float = 0.25):
        """Yield interpolated points and time deltas along the stored geometry."""
        if not math.isfinite(speed) or speed <= 0 or interval <= 0:
            raise ValueError("速度和更新间隔必须大于零")
        for start, end, distance in zip(self.points, self.points[1:], self.segment_lengths):
            if distance <= 0:
                continue
            duration = distance / speed
            count = max(1, math.ceil(duration / interval))
            for index in range(1, count + 1):
                ratio = index / count
                yield interpolate(start, end, ratio), duration / count


MAX_ROUTE_BYTES = 2 * 1024 * 1024
MAX_ROUTE_POINTS = 20000


def valid_point_count(points: Sequence[Point]) -> bool:
    return (2 <= len(points) <= MAX_ROUTE_POINTS
            or len(points) == MAX_ROUTE_POINTS + 1 and points[0] == points[-1])


def read_route_file(path: Path) -> bytes:
    with path.open("rb") as file:
        contents = file.read(MAX_ROUTE_BYTES + 1)
    if len(contents) > MAX_ROUTE_BYTES:
        raise ValueError("路线文件过大，上限为 2 MB")
    return contents


def parse_point(lat, lng) -> Point:
    try:
        if isinstance(lat, bool) or isinstance(lng, bool):
            raise ValueError
        lat, lng = float(lat), float(lng)
        if not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("路线包含无效坐标，请检查经纬度") from error
    return Point(lat, lng)


def load_route(path: Path) -> Route:
    contents = read_route_file(path).decode("utf-8-sig")
    try:
        records = ast.literal_eval(contents.strip())
    except (ValueError, SyntaxError, RecursionError) as error:
        raise ValueError("路线格式不正确，需要包含 lat、lng 的坐标列表") from error
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, (list, tuple)) or not 2 <= len(records) <= MAX_ROUTE_POINTS:
        raise ValueError("路线需要包含 2 至 20000 个坐标点")
    points = []
    for record in records:
        try:
            if not isinstance(record, dict):
                raise ValueError
            point = parse_point(record["lat"], record["lng"])
        except (KeyError, ValueError) as error:
            raise ValueError("路线包含无效坐标，请检查 lat、lng") from error
        converted = bd09_to_wgs84({"lat": point.lat, "lng": point.lng})
        points.append(parse_point(converted["lat"], converted["lng"]))
    return build_loop(path, points)


def build_loop(path: Path, coordinates: Sequence[Point]) -> Route:
    """Build a loop from WGS-84 points after the caller has chosen to close it."""
    return build_route(path, coordinates, closed=True)


def build_route(path: Path, coordinates: Sequence[Point], *, closed: bool) -> Route:
    if not valid_point_count(coordinates) or not closed and len(coordinates) > MAX_ROUTE_POINTS:
        raise ValueError("路线需要包含 2 至 20000 个坐标点")
    points = []
    for point in coordinates:
        point = parse_point(point.lat, point.lng)
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 2:
        raise ValueError("路线需要至少两个不同的坐标点")
    if not closed and points[-1] == points[0]:
        raise ValueError("往返路线的起点和终点须不同")
    if closed and points[-1] != points[0]:
        points.append(points[0])
    lengths = tuple(geodesic((a.lat, a.lng), (b.lat, b.lng)).meters for a, b in zip(points, points[1:]))
    if sum(lengths) < 1:
        raise ValueError("路线总长度不足 1 米")
    return Route(path, tuple(points), lengths, closed)


def pace_seconds(speed: float) -> int:
    return math.floor(1000 / speed + 0.5)


def pace_text(speed: float) -> str:
    if not math.isfinite(speed) or speed <= 0:
        return "—"
    seconds = pace_seconds(speed)
    return f"{seconds // 60}:{seconds % 60:02d} / 公里"


@dataclass
class LapCounter:
    lap_length_m: float
    target_m: float | None = None
    laps: int = 0
    notified: bool = False

    @property
    def distance_m(self) -> float:
        return self.laps * self.lap_length_m

    def complete_lap(self) -> bool:
        self.laps += 1
        return self.check_target(self.distance_m)

    def check_target(self, distance_m: float) -> bool:
        reached = self.target_m is not None and distance_m >= self.target_m and not self.notified
        if reached:
            self.notified = True
        return reached
