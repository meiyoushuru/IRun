# SPDX-License-Identifier: MPL-2.0
"""Import GPX geometry and store user-created WGS-84 routes."""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from unicodedata import normalize
from uuid import uuid4
from xml.etree import ElementTree as ET

from .routes import (MAX_ROUTE_BYTES, MAX_ROUTE_POINTS, Point, Route, build_route,
                     parse_point, read_route_file, valid_point_count)


GPX = {"gpx": "http://www.topografix.com/GPX/1/1"}
MAX_ROUTE_NAME_LENGTH = 120
INVALID_ROUTE_NAME_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@dataclass(frozen=True)
class GpxSegment:
    name: str
    description: str
    points: tuple[Point, ...]


class GpxTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("GPX 文件不能包含文档类型声明")


def load_gpx(path: Path) -> list[GpxSegment]:
    contents = read_route_file(path)
    try:
        root = ET.fromstring(contents, parser=ET.XMLParser(target=GpxTreeBuilder()))
    except (ET.ParseError, LookupError) as error:
        raise ValueError("无法读取 GPX 文件，请检查 XML 格式和编码") from error
    if root.tag != f"{{{GPX['gpx']}}}gpx" or root.get("version") != "1.1":
        raise ValueError("请选择 GPX 1.1 文件")

    segments = []
    point_count = 0
    for index, track in enumerate(root.findall("gpx:trk", GPX), 1):
        name = (track.findtext("gpx:name", "", GPX) or "").strip() or path.stem
        for number, segment in enumerate(track.findall("gpx:trkseg", GPX), 1):
            points = segment.findall("gpx:trkpt", GPX)
            point_count += len(points)
            segments.append((name, f"轨迹 {index} · 第 {number} 段", points))
    for index, route in enumerate(root.findall("gpx:rte", GPX), 1):
        name = (route.findtext("gpx:name", "", GPX) or "").strip() or path.stem
        points = route.findall("gpx:rtept", GPX)
        point_count += len(points)
        segments.append((name, f"路线 {index}", points))
    if point_count > MAX_ROUTE_POINTS + 1:
        raise ValueError("GPX 文件最多支持 20000 个坐标点")
    if not any(len(points) >= 2 for _, _, points in segments):
        raise ValueError("GPX 文件中没有包含至少两个坐标点的连续路线")
    valid_segments = [
        GpxSegment(name, description, tuple(parse_point(p.get("lat"), p.get("lon")) for p in points))
        for name, description, points in segments if len(points) >= 2
    ]
    if point_count > MAX_ROUTE_POINTS:
        if len(valid_segments) != 1 or not valid_point_count(valid_segments[0].points):
            raise ValueError("GPX 文件最多支持 20000 个坐标点，闭合重复起点除外")
    return valid_segments


def read_saved_route(path: Path) -> tuple[str, list, bool]:
    try:
        data = json.loads(read_route_file(path))
    except (ValueError, RecursionError) as error:
        raise ValueError("用户路线文件无法读取：" + str(error)) from error
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or type(data.get("closed")) is not bool
            or not isinstance(data.get("name"), str) or not data["name"].strip()
            or not isinstance(data.get("points"), list)):
        raise ValueError("用户路线文件格式不正确")
    return data["name"], data["points"], data["closed"]


def load_saved_route(path: Path) -> Route:
    _, coordinates, closed = read_saved_route(path)
    if not 2 <= len(coordinates) <= MAX_ROUTE_POINTS:
        raise ValueError("路线需要包含 2 至 20000 个坐标点")
    if any(not isinstance(pair, list) or len(pair) != 2 for pair in coordinates):
        raise ValueError("用户路线文件包含无效坐标")
    return build_route(path, [parse_point(lat, lng) for lat, lng in coordinates], closed=closed)


def save_loop(directory: Path, name: str, route: Route) -> Path:
    if not route.closed:
        raise ValueError("请先闭合路线")
    return save_route(directory, name, route)


def save_route(directory: Path, name: str, route: Route, *, existing: Path | None = None,
               reserved_names=None) -> Path:
    path = existing if existing is not None else directory / f"{uuid4().hex}.json"
    with lock_route_names(directory):
        name = unique_route_name(validate_name(name), taken_name_keys(directory, existing, reserved_names))
        data = {
            "version": 1,
            "name": name,
            "closed": route.closed,
            # The closing point is implicit. The saved coordinates remain WGS-84.
            "points": [[p.lat, p.lng] for p in (route.points[:-1] if route.closed else route.points)],
        }
        write_saved_route(path, data)
    return path


def rename_route(path: Path, name: str, *, directory: Path | None = None, reserved_names=None):
    directory = directory if directory is not None else path.parent
    with lock_route_names(directory):
        _, points, closed = read_saved_route(path)
        name = unique_route_name(validate_name(name), taken_name_keys(directory, path, reserved_names))
        write_saved_route(path, {"version": 1, "name": name, "closed": closed, "points": points})
    return name


def route_name_key(name: str) -> str:
    return normalize("NFKC", name).strip().casefold()


def unique_route_name(name: str, taken: set[str]) -> str:
    candidate = name
    number = 2
    while route_name_key(candidate) in taken:
        suffix = f" ({number})"
        candidate = name[:MAX_ROUTE_NAME_LENGTH - len(suffix)].rstrip() + suffix
        number += 1
    return candidate


@contextmanager
def lock_route_names(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".route-names.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OSError("无法锁定路线库，请稍后重试。") from error
        # Closing the file releases the operating system lock.
        yield


def taken_name_keys(directory: Path, exclude: Path | None, reserved_names=None) -> set[str]:
    if reserved_names is not None:
        return {route_name_key(name) for name in reserved_names(exclude)}
    names = set()
    for path in directory.glob("*.json"):
        if path != exclude:
            try:
                name, _, _ = read_saved_route(path)
            except (OSError, ValueError):
                continue
            names.add(route_name_key(name))
    return names


def write_saved_route(path: Path, data: dict):
    write_route_file(path, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def export_gpx(path: Path, name: str, points):
    root = ET.Element("gpx", xmlns=GPX["gpx"], version="1.1", creator="IRun")
    track = ET.SubElement(root, "trk")
    ET.SubElement(track, "name").text = validate_name(name)
    segment = ET.SubElement(track, "trkseg")
    if not valid_point_count(points):
        raise ValueError("GPX 路线需要包含 2 至 20000 个坐标点")
    for point in points:
        point = parse_point(point.lat, point.lng)
        # GPX uses XML decimals and excludes +180 from its longitude range.
        longitude = -180 if point.lng == 180 else point.lng
        ET.SubElement(segment, "trkpt", lat=format(Decimal(str(point.lat)), "f"),
                      lon=format(Decimal(str(longitude)), "f"))
    write_route_file(path, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("请输入路线名称")
    if len(name) > MAX_ROUTE_NAME_LENGTH:
        raise ValueError("路线名称最多支持 120 个字符")
    if INVALID_ROUTE_NAME_CHARS.search(name):
        raise ValueError("路线名称包含无法保存的字符，请修改名称后重试")
    return name


def write_route_file(path: Path, contents: bytes):
    if len(contents) > MAX_ROUTE_BYTES:
        raise ValueError("路线文件过大，上限为 2 MB")
    output = NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(output.name)
    try:
        with output:
            output.write(contents)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
