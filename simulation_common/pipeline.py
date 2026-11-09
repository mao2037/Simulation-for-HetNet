from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np

from .core import WORKSPACE, Network, Scenario, SimulationConfig, data_path, number_text, od_path, project_path, require_output
from .demand import generate_warmup
from .engine import PassengerSimulation
from .excel_io import load_passengers, read_metadata, read_table_rows
from .cell_conservative_demand import demand_source_identity, load_cell_conservative_demand
from .model_metrics import STRATEGIES, write_model_metrics_table
from .route_filter import filter_operating_network, route_filter_settings
from .schedule import Schedule, Trip


def missing_prerequisites(scenario: Scenario, reps: range | tuple[int, ...],
                          stages: frozenset[str]) -> dict[str, Path]:
    missing = {}
    if "network" not in stages and stages & {"scene", "simulation"}:
        network = data_path("network", scenario)
        if not network.is_file():
            missing["network"] = network
    if "simulation" in stages:
        for stage in ("od", "scene"):
            if stage in stages:
                continue
            for rep in reps:
                path = od_path(scenario, rep) if stage == "od" else data_path("scene", scenario, rep)
                if not path.is_file():
                    missing[stage] = path
                    break
    return missing


def export_network(scenario: Scenario) -> Path:
    source = project_path("inputs", "model", scenario.source_name)
    if not source.is_file():
        raise FileNotFoundError(f"Missing network solution MAT: {source}")
    path = data_path("network", scenario)
    matlab = shutil.which("matlab")
    if not matlab:
        raise FileNotFoundError("The network stage requires MATLAB on PATH")
    arguments = [f"'{scenario.city}'", str(int(scenario.demand)), number_text(scenario.mu),
                 number_text(scenario.alpha), number_text(scenario.citysize)]
    command = f"export_discrete_network({', '.join(arguments)});"
    launcher = [matlab, "-wait"] if os.name == "nt" else [matlab]
    subprocess.run([*launcher, "-sd", str(WORKSPACE), "-batch", command], check=True, cwd=WORKSPACE)
    metadata = read_metadata(path)
    if Scenario.from_dict(metadata) != scenario or str(metadata.get("includes_export_completed")).lower() != "true":
        raise ValueError(f"MATLAB did not produce a complete network for {scenario.key}")
    return path


class WorkbookSchedule(Schedule):


    def __init__(self, network: Network, rep: int, config: SimulationConfig, path: Path):


        network = filter_operating_network(network, config)
        super().__init__(network, rep, config)

        metadata = read_metadata(path)
        if Scenario.from_dict(metadata) != network.scenario or int(metadata["rep"]) != rep:
            raise ValueError("场景 Excel 的五参数或重复编号与仿真不符")
        if json.loads(metadata["route_filter_settings"]) != route_filter_settings(config):
            raise ValueError("Scene route filtering differs from the current configuration")
        for name in ("bus_speed_kmh", "dwell_s", "base_seed", "warmup_s"):
            if float(metadata[name]) != float(getattr(config, name)):
                raise ValueError(f"场景 Excel 中 {name} 与本次配置不符，请重新构建场景")
        self.saved_first_hour = int(metadata["first_departure_hour"])
        self.saved_last_hour = int(metadata["last_departure_hour"])
        self.saved_trips = {hour: [] for hour in range(self.saved_first_hour, self.saved_last_hour + 1)}

        route_lookup = {route.route_id: index for index, route in enumerate(network.routes)}

        identifiers = set()
        for row in read_table_rows(path, "trips"):

            trip = Trip(str(row["trip_id"]), str(row["bus_id"]), route_lookup[int(row["route_id"])], float(row["departure_s"]))
            if trip.trip_id in identifiers or trip.bus_id != trip.trip_id or not np.isfinite(trip.departure_s):
                raise ValueError("场景存在重复班次、非独立车辆号或无效发车时刻")
            identifiers.add(trip.trip_id)
            self.saved_trips.setdefault(int(np.floor(trip.departure_s / 3600)), []).append(trip)

        route_rows = list(read_table_rows(path, "routes"))
        if len(route_rows) != len(network.routes) or len({int(row["route_id"]) for row in route_rows}) != len(network.routes):
            raise ValueError("场景线路表存在缺失或重复")
        for row in route_rows:

            route = network.routes[route_lookup[int(row["route_id"])]]
            if str(row["direction"]) != route.direction or not np.isclose(float(row["frequency_veh_h"]), route.frequency_veh_h, rtol=1e-12, atol=1e-12) or abs(float(row["length_km"]) - route.length_km) > 1e-8:
                raise ValueError("场景线路频率或几何长度与网络不一致，请重建场景")

        stop_count = 0
        seen_stops = set()
        for row in read_table_rows(path, "route_stops"):
            route_index = route_lookup[int(row["route_id"])]
            sequence = int(row["sequence"]) - 1
            if (route_index, sequence) in seen_stops or sequence < 0 or sequence >= len(network.routes[route_index].stop_ids):
                raise ValueError("场景线路站序重复或越界")
            seen_stops.add((route_index, sequence))
            if int(row["station_id"]) - 1 != network.routes[route_index].stop_ids[sequence] or abs(float(row["arrival_offset_s"]) - self.arrival_offsets[route_index][sequence]) > 1e-6 or abs(float(row["departure_offset_s"]) - self.departure_offsets[route_index][sequence]) > 1e-6:
                raise ValueError("场景的线路站序或到站偏移与网络不一致")
            stop_count += 1
        if stop_count != sum(len(route.stop_ids) for route in network.routes):
            raise ValueError("场景线路站序存在缺失")
        for trips in self.saved_trips.values():
            trips.sort(key=lambda trip: (trip.departure_s, trip.trip_id))

    def hour_trips(self, hour: int) -> list[Trip]:


        if self.saved_first_hour <= hour <= self.saved_last_hour:
            return self.saved_trips.get(hour, [])
        return super().hour_trips(hour)

    def hour_trips_for_routes(self, hour: int, route_indices: set[int]) -> list[Trip]:
        if self.saved_first_hour <= hour <= self.saved_last_hour:
            return [trip for trip in self.saved_trips.get(hour, []) if trip.route_index in route_indices]
        return super().hour_trips_for_routes(hour, route_indices)


def simulate_scenario(network: Network, rep: int, config: SimulationConfig) -> dict[str, tuple]:
    network = filter_operating_network(network, config)
    scenario = network.scenario
    od_file = require_output(od_path(scenario, rep), "od")
    scene_file = require_output(data_path("scene", scenario, rep), "scene")
    metadata = read_metadata(od_file)
    if Scenario.from_dict(metadata) != scenario or int(metadata["rep"]) != rep:
        raise ValueError(f"OD scenario or replication mismatch: {od_file}")
    _, source = load_cell_conservative_demand(scenario)
    identity = {**demand_source_identity(source), "base_seed": config.base_seed,
                "formal_start_s": 0, "formal_end_s": 3600}
    if any(metadata.get(name) != value for name, value in identity.items()):
        raise ValueError(f"OD input differs from the current seed or demand MAT. Run the od stage: {od_file}")
    formal = load_passengers(od_file)
    if (np.any(formal.departure_s < 0) or np.any(formal.departure_s >= 3600)
            or np.any(formal.origin_xy < 0) or np.any(formal.destination_xy < 0)
            or np.any(formal.origin_xy > scenario.citysize) or np.any(formal.destination_xy > scenario.citysize)):
        raise ValueError(f"OD times or coordinates are outside the scenario bounds: {od_file}")
    schedule = WorkbookSchedule(network, rep, config, scene_file)
    background = generate_warmup(scenario, rep, config)
    times_by_strategy = {}
    for strategy in STRATEGIES:
        engine = PassengerSimulation(network, formal, rep, config, strategy, background, schedule)
        times_by_strategy[strategy] = engine.run().objective_times(scenario.demand)
        del engine
        gc.collect()
    return write_model_metrics_table(network, rep, times_by_strategy, config)
