from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from numbers import Integral
from pathlib import Path
import re
from typing import Any

import numpy as np

WORKSPACE = Path(__file__).resolve().parents[1]
DEMAND_TAG = "cell_conservative"
CHECKERBOARD_SEED_NAMES = {
    "checkerboard1": "Chessboard0",
    "checkerboard2": "Chessboard1",
    "checkerboard3": "Chessboard4",
    "checkerboard4": "Chessboard5",
}


def project_path(*parts: str | Path) -> Path:
    path = WORKSPACE.joinpath(*parts).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError(f"Path is outside the project: {path}")
    return path


def data_path(kind: str, scenario: Scenario, rep: int | None = None, *folders: str) -> Path:
    filename = f"{kind}_{scenario.key}"
    if rep is not None:
        filename += f"_rep_{rep:03d}"
    return project_path("outputs", kind, *folders, filename + ".xlsx")


def od_path(scenario: Scenario, rep: int) -> Path:
    return data_path("od", scenario, rep)


def require_output(path: Path, stage: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"缺少前置文件 {path}。请先运行 --stages {stage} 生成对应场景和重复号的工作簿。")
    return path


def number_text(value: float) -> str:
    return format(float(value), ".12g")


@dataclass(frozen=True, order=True)
class Scenario:


    city: str
    demand: int
    mu: float
    alpha: float
    citysize: float

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.city):
            raise ValueError("Invalid city name")
        if self.demand <= 0 or int(self.demand) != self.demand:
            raise ValueError("demand 必须是正整数，表示一小时实际乘客总量")
        if not np.all(np.isfinite([self.mu, self.alpha, self.citysize])) or self.mu <= 0 or self.citysize <= 0 or self.alpha < 0:
            raise ValueError("mu、citysize 必须为有限正数，alpha 必须为有限非负数")

    @property
    def key(self) -> str:
        return self._key(self.city)

    @property
    def seed_city(self) -> str:
        return CHECKERBOARD_SEED_NAMES.get(self.city, self.city)

    @property
    def seed_key(self) -> str:
        return self._key(self.seed_city)

    def _key(self, city: str) -> str:
        return f"city_{city}_demand_{int(self.demand)}_mu_{number_text(self.mu)}_alpha_{number_text(self.alpha)}_citysize_{number_text(self.citysize)}"

    @property
    def source_name(self) -> str:


        return self.key[len("city_"):] + ".mat"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":


        demand = float(data["demand"])
        if not demand.is_integer():
            raise ValueError("demand 必须是整数")
        return cls(str(data["city"]), int(demand), float(data["mu"]), float(data["alpha"]), float(data["citysize"]))


@dataclass(frozen=True)
class SimulationConfig:
    bus_speed_kmh: float = 25.0
    walk_speed_kmh: float = 2.0
    dwell_s: float = 30.0
    transfer_s: float = 60.0
    capacity: int = 100000
    warmup_s: float = 7200.0
    base_seed: int = 42
    sheet_rows: int = 500000
    routing_cache_destinations: int = 16
    route_max_headway_min: float = 120.0

    def __post_init__(self) -> None:
        positive = (self.bus_speed_kmh, self.walk_speed_kmh, self.dwell_s, self.route_max_headway_min)
        nonnegative = (self.transfer_s, self.warmup_s)
        if not np.isfinite(positive + nonnegative).all() or min(positive) <= 0 or min(nonnegative) < 0:
            raise ValueError("Invalid speeds, headway, dwell, transfer, or warm-up time")
        counts = (self.capacity, self.sheet_rows, self.routing_cache_destinations, self.base_seed)
        if any(isinstance(value, bool) or not isinstance(value, Integral) for value in counts):
            raise ValueError("Capacity, sheet size, cache size, and base seed must be integers")
        if self.capacity < 1 or not 1 <= self.sheet_rows <= 1048575 or self.routing_cache_destinations < 1:
            raise ValueError("Invalid capacity, sheet size, or cache size")


@dataclass
class Route:


    route_id: int
    direction: str
    frequency_veh_h: float
    length_km: float
    stop_ids: np.ndarray
    distances_km: np.ndarray


@dataclass
class Network:


    scenario: Scenario
    stations: np.ndarray
    routes: list[Route]
    metadata: dict[str, Any] | None = None

    def validate(self) -> None:
        if self.stations.ndim != 2 or self.stations.shape[1] != 2 or not len(self.stations):
            raise ValueError("网络必须至少包含一个二维服务站点")
        if not np.all(np.isfinite(self.stations)) or np.any(self.stations < -1e-8) or np.any(self.stations > self.scenario.citysize + 1e-8):
            raise ValueError("站点坐标非有限值或超出城市边界")
        if not self.routes:
            raise ValueError("网络没有完整公交路径")
        for route in self.routes:
            if route.direction not in ("E", "W", "N", "S") or not np.isfinite(route.frequency_veh_h) or route.frequency_veh_h <= 0:
                raise ValueError(f"线路 {route.route_id} 的方向或频率无效")
            if len(route.stop_ids) != len(route.distances_km) or np.any(route.stop_ids < 0) or np.any(route.stop_ids >= len(self.stations)):
                raise ValueError(f"线路 {route.route_id} 的站序无效")
            if not np.all(np.isfinite(route.distances_km)) or np.any(np.diff(route.distances_km) < -1e-8):
                raise ValueError(f"线路 {route.route_id} 的累计里程不单调")
            if len(route.distances_km) and (route.distances_km[0] < -1e-8 or route.distances_km[-1] > route.length_km + 1e-7):
                raise ValueError(f"线路 {route.route_id} 的站点里程超过路径范围")


@dataclass
class PassengerInput:


    passenger_id: np.ndarray
    departure_s: np.ndarray
    origin_xy: np.ndarray
    destination_xy: np.ndarray

    def __len__(self) -> int:
        return len(self.departure_s)


def stable_seed(*parts: Any) -> int:


    payload = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little")
