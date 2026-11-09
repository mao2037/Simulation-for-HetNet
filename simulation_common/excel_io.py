from __future__ import annotations

import json
from pathlib import Path
import posixpath
from tempfile import NamedTemporaryFile
from typing import Any, Iterable
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import numpy as np
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .core import Network, PassengerInput, Route, Scenario, project_path


class ExcelWriter:


    def __init__(self, path: Path, sheet_rows: int = 500000):
        self.path = project_path(path)
        self.sheet_rows = sheet_rows
        self.book = Workbook(write_only=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def table(self, name: str, headers: list[str] | tuple, rows: Iterable) -> int:


        count, sheet, part = 0, None, 0
        for row in rows:
            if count % self.sheet_rows == 0:
                part += 1
                sheet = self._sheet(name if part == 1 else f"{name[:26]}_{part:03d}", headers)
            sheet.append([self._value(value) for value in row])
            count += 1
        if sheet is None:
            self._sheet(name, headers)
        return count

    def _sheet(self, name: str, headers: Iterable):


        sheet = self.book.create_sheet(name[:31])
        sheet.freeze_panes = "A2"
        cells = []
        for index, header in enumerate(headers, 1):

            cell = WriteOnlyCell(sheet, str(header))
            cell.font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="234E70")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            cells.append(cell)
            sheet.column_dimensions[get_column_letter(index)].width = min(44, max(16, len(str(header)) + 2))
        sheet.row_dimensions[1].height = 32
        sheet.append(cells)
        return sheet

    @staticmethod
    def _value(value: Any) -> Any:
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not np.isfinite(value):
            return None
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return value

    def metadata(self, values: dict[str, Any]) -> None:
        self.table("metadata", ["key", "value"], values.items())

    def close(self) -> Path:


        with NamedTemporaryFile(dir=self.path.parent, suffix=".partial.xlsx", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            self.book.save(temporary)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.close()


def _xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _archive_member(target: str, base: str = "xl") -> str:
    return posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))


def _rich_text(element) -> str:


    pieces = []
    for child in element:
        if _xml_name(child.tag) == "t":
            pieces.append(child.text or "")
        elif _xml_name(child.tag) == "r":
            pieces.extend(item.text or "" for item in child if _xml_name(item.tag) == "t")
    return "".join(pieces)


def _shared_strings(archive: ZipFile, member: str) -> list[str]:


    strings = []
    root = None
    with archive.open(member) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = element
            if event == "end" and _xml_name(element.tag) == "si":
                strings.append(_rich_text(element).replace("x005F_", ""))
                element.clear()
                root.clear()
    return strings


def _column_number(reference: str) -> int:


    number = 0
    for character in reference.replace("$", "").upper():
        if not "A" <= character <= "Z":
            break
        number = number * 26 + ord(character) - ord("A") + 1
    if not number:
        raise ValueError(f"无效Excel单元格坐标：{reference}")
    return number - 1


def read_table_rows(path: Path, name: str):


    with ZipFile(project_path(path)) as archive:

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))

        targets = {relation.attrib["Id"]: _archive_member(relation.attrib["Target"])
                   for relation in relationships if relation.attrib.get("TargetMode") != "External"}
        shared_member = next((_archive_member(relation.attrib["Target"]) for relation in relationships
                              if relation.attrib.get("Type", "").endswith("/sharedStrings")
                              and relation.attrib.get("TargetMode") != "External"), None)

        strings = None
        for sheet in workbook.iter():
            if _xml_name(sheet.tag) != "sheet":
                continue

            title = sheet.attrib.get("name", "")
            suffix = title[len(name) + 1:] if title.startswith(name + "_") else ""
            if title != name and not suffix.isdigit():
                continue

            relationship_id = next(value for attribute, value in sheet.attrib.items() if _xml_name(attribute) == "id")
            member = targets[relationship_id]

            headers, sheet_data = None, None
            with archive.open(member) as stream:
                for event, element in ET.iterparse(stream, events=("start", "end")):
                    if event == "start" and _xml_name(element.tag) == "sheetData":
                        sheet_data = element
                    if event != "end" or _xml_name(element.tag) != "row":
                        continue

                    cells = {}
                    column = -1
                    for cell in element:
                        if _xml_name(cell.tag) != "c":
                            continue
                        column = _column_number(cell.attrib["r"]) if "r" in cell.attrib else column + 1

                        kind = cell.attrib.get("t", "n")
                        value_node = next((child for child in cell if _xml_name(child.tag) == "v"), None)
                        inline = next((child for child in cell if _xml_name(child.tag) == "is"), None)

                        value = value_node.text if value_node is not None else None
                        if kind == "inlineStr":
                            value = _rich_text(inline) if inline is not None else None
                        elif value is not None and kind == "s":
                            if strings is None:
                                if shared_member is None:
                                    raise ValueError("目标表引用共享字符串，但工作簿缺少sharedStrings关系")
                                strings = _shared_strings(archive, shared_member)
                            value = strings[int(value)]
                        elif value is not None and kind == "b":
                            value = bool(int(value))
                        elif value is not None and kind == "n":
                            value = float(value) if any(character in value for character in ".eE") else int(value)
                        cells[column] = value
                    if headers is None:
                        headers = [cells.get(index) for index in range(max(cells, default=-1) + 1)]
                    elif any(value is not None for value in cells.values()):
                        yield {header: cells.get(index) for index, header in enumerate(headers)}
                    element.clear()
                    if sheet_data is not None:
                        sheet_data.clear()


def read_metadata(path: Path) -> dict[str, Any]:
    return {str(row["key"]): row["value"] for row in read_table_rows(path, "metadata")}


def load_network(path: Path, expected_scenario: Scenario | None = None) -> Network:


    metadata = read_metadata(path)

    scenario = Scenario.from_dict(metadata)
    if expected_scenario is not None and scenario != expected_scenario:
        raise ValueError(f"网络Excel的五参数与所选场景不一致：{path}；请复制匹配场景的前置文件")
    station_rows = sorted(read_table_rows(path, "stations"), key=lambda row: int(row["station_id"]))

    station_lookup = {int(row["station_id"]): index for index, row in enumerate(station_rows)}
    stations = np.array([[row["x_km"], row["y_km"]] for row in station_rows], dtype=float)

    stop_groups: dict[int, list] = {}
    for row in read_table_rows(path, "route_stops"):
        stop_groups.setdefault(int(row["route_id"]), []).append((int(row["sequence"]), station_lookup[int(row["station_id"])], float(row["distance_km"])))

    routes = []
    for row in sorted(read_table_rows(path, "routes"), key=lambda value: int(value["route_id"])):

        stops = sorted(stop_groups.pop(int(row["route_id"]), []))
        routes.append(Route(int(row["route_id"]), str(row["direction"]), float(row["frequency_veh_h"]), float(row["length_km"]),
                            np.array([stop[1] for stop in stops], dtype=np.int32),
                            np.array([stop[2] for stop in stops], dtype=float)))
    if stop_groups:
        raise ValueError("线路站序引用了不存在的完整路径")

    network = Network(scenario, stations, routes, metadata)
    network.validate()
    return network


def write_passengers(path: Path, passengers: PassengerInput, metadata: dict, sheet_rows: int = 500000) -> Path:
    with ExcelWriter(path, sheet_rows) as writer:
        writer.metadata(metadata)
        writer.table("passengers", ["passenger_id", "departure_s", "origin_x_km", "origin_y_km", "destination_x_km", "destination_y_km"],
                     ((passengers.passenger_id[index], passengers.departure_s[index], *passengers.origin_xy[index], *passengers.destination_xy[index]) for index in range(len(passengers))))
    return path


def load_passengers(path: Path) -> PassengerInput:


    metadata = read_metadata(path)

    count = int(metadata["demand"])
    values = np.empty((count, 6), dtype=float)
    index = 0
    for row in read_table_rows(path, "passengers"):
        if index >= count:
            raise ValueError("OD 行数超过场景 demand")
        values[index] = [row[name] for name in ("passenger_id", "departure_s", "origin_x_km", "origin_y_km", "destination_x_km", "destination_y_km")]
        index += 1
    if index != count or not np.all(np.isfinite(values)):
        raise ValueError("OD 行数与 demand 不符或含非有限数值")
    if len(np.unique(values[:, 0])) != count:
        raise ValueError("乘客编号重复")

    order = np.lexsort((values[:, 0], values[:, 1]))
    values = values[order]
    return PassengerInput(values[:, 0].astype(np.int64), values[:, 1], values[:, 2:4], values[:, 4:6])
