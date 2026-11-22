from __future__ import annotations
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
import numpy as np
from .core import Network, SimulationConfig, scene_path, stable_seed
from .excel_io import ExcelWriter, read_metadata
from .route_filter import filter_operating_network, route_filter_settings
from .phase_coordination import (
    PHASE_COORDINATION_VERSION,
    coordinate_route_phases,
    phase_coordination_settings,
)

PERIODIC_SCHEDULE_VERSION = "route_periodic_shared_stop_phases_v1"


@dataclass(frozen=True)
class Trip:
    trip_id: str
    bus_id: str
    route_index: int
    departure_s: float


class Schedule:
    def __init__(self, network: Network, rep: int, config: SimulationConfig):
        network.validate()
        if rep < 1:
            raise ValueError("重复编号必须为正整数")
        self.network = network
        self.rep = rep
        self.config = config
        self.arrival_offsets = []
        self.departure_offsets = []
        self.route_durations = []
        for route in network.routes:
            arrival = (
                route.distances_km / config.bus_speed_kmh * 3600
                + np.arange(len(route.stop_ids)) * config.dwell_s
            )
            self.arrival_offsets.append(arrival)
            self.departure_offsets.append(arrival + config.dwell_s)
            self.route_durations.append(
                route.length_km / config.bus_speed_kmh * 3600 + len(route.stop_ids) * config.dwell_s
            )
        self.first_hour = floor((-config.warmup_s - max(self.route_durations, default=0)) / 3600)
        self.route_headways_s = np.array([3600.0 / route.frequency_veh_h for route in network.routes])
        (self.route_phases_s, self.phase_coordination, self.phase_group_audit) = coordinate_route_phases(
            network, self.arrival_offsets, rep, config
        )

    def hour_trips(self, hour: int) -> list[Trip]:
        if int(hour) != hour:
            raise ValueError("hour 必须为整数")
        return self._periodic_trips(hour, range(len(self.network.routes)))

    def _periodic_trips(self, hour: int, route_indices) -> list[Trip]:
        trips = []
        (start_s, end_s) = (hour * 3600, (hour + 1) * 3600)
        for route_index in sorted(route_indices):
            if not 0 <= route_index < len(self.network.routes):
                continue
            (h, phase) = (self.route_headways_s[route_index], self.route_phases_s[route_index])
            sequences = np.arange(
                floor((start_s - phase) / h) - 1, ceil((end_s - phase) / h) + 2, dtype=np.int64
            )
            departures = phase + sequences * h
            inside = (departures >= start_s) & (departures < end_s)
            route = self.network.routes[route_index]
            for (sequence, departure) in zip(sequences[inside], departures[inside]):
                trip_id = f"{route.direction}R{route.route_id}_{int(sequence):+013d}"
                trips.append(Trip(trip_id, trip_id, int(route_index), float(departure)))
        return sorted(trips, key=lambda trip: (trip.departure_s, trip.trip_id))

    def hour_trips_for_routes(self, hour: int, route_indices: set[int]) -> list[Trip]:
        if int(hour) != hour:
            raise ValueError("hour 必须为整数")
        if not route_indices:
            return []
        return self._periodic_trips(hour, route_indices)

    def initial_trips(self):
        for hour in range(self.first_hour, ceil(-self.config.warmup_s / 3600)):
            for trip in self.hour_trips(hour):
                if (
                    trip.departure_s < -self.config.warmup_s
                    and trip.departure_s + self.route_durations[trip.route_index] >= -self.config.warmup_s
                ):
                    yield trip

    def scene_trips(self):
        for hour in range(self.first_hour, 1):
            for trip in self.hour_trips(hour):
                if trip.departure_s + self.route_durations[trip.route_index] >= -self.config.warmup_s:
                    yield trip


def _network_signature(network: Network) -> int:
    return stable_seed(
        network.stations.tolist(),
        [
            (
                route.route_id,
                route.direction,
                route.frequency_veh_h,
                route.length_km,
                route.stop_ids.tolist(),
                route.distances_km.tolist(),
            )
            for route in network.routes
        ],
    )


def schedule_version(config: SimulationConfig) -> str:
    return PERIODIC_SCHEDULE_VERSION


def _scene_settings(config: SimulationConfig) -> dict:
    settings = {
        name: getattr(config, name)
        for name in ("bus_speed_kmh", "dwell_s", "base_seed", "warmup_s", "sheet_rows")
    }
    settings.update(
        scheduling_mode="route_periodic_coordinated",
        phase_coordination_version=PHASE_COORDINATION_VERSION,
        phase_coordination_settings=phase_coordination_settings(),
    )
    return settings


def scene_signature(network: Network, rep: int, config: SimulationConfig) -> str:
    network = filter_operating_network(network, config)
    return str(
        stable_seed(
            schedule_version(config),
            _network_signature(network),
            rep,
            _scene_settings(config),
            route_filter_settings(config),
        )
    )


def build_scene(network: Network, rep: int, config: SimulationConfig) -> Path:
    network = filter_operating_network(network, config)
    path = scene_path(network.scenario, rep, config)
    scene_settings = _scene_settings(config)
    signature = scene_signature(network, rep, config)
    if path.is_file() and read_metadata(path).get("scene_signature") == signature:
        return path
    schedule = Schedule(network, rep, config)
    metadata = {
        **network.scenario.to_dict(),
        "rep": rep,
        **scene_settings,
        "scheduling_mode": "route_periodic_coordinated",
        "schedule_version": schedule_version(config),
        "scene_signature": signature,
        "phase_coordination": schedule.phase_coordination,
        "route_filter_settings": route_filter_settings(config),
        "first_departure_hour": schedule.first_hour,
        "last_departure_hour": 0,
        "time_unit": "s",
        "coordinate_unit": "km",
        "frequency_unit": "veh/h",
    }
    with ExcelWriter(path, config.sheet_rows) as writer:
        writer.metadata(metadata)
        writer.table(
            "routes",
            ["route_id", "direction", "frequency_veh_h", "length_km", "stop_count", "headway_s", "phase_s"],
            (
                (
                    route.route_id,
                    route.direction,
                    route.frequency_veh_h,
                    route.length_km,
                    len(route.stop_ids),
                    schedule.route_headways_s[index],
                    float(schedule.route_phases_s[index]),
                )
                for (index, route) in enumerate(network.routes)
            ),
        )
        columns = [
            "direction",
            "station_id",
            "route_count",
            "combined_headway_s",
            "ideal_half_headway_s",
            "initial_mean_wait_s",
            "coordinated_mean_wait_s",
            "improvement_s",
        ]
        writer.table(
            "phase_group_audit",
            columns,
            ([row[column] for column in columns] for row in schedule.phase_group_audit),
        )
        writer.table(
            "stations",
            ["station_id", "x_km", "y_km"],
            ((index + 1, *position) for (index, position) in enumerate(network.stations)),
        )
        writer.table(
            "route_stops",
            ["route_id", "sequence", "station_id", "distance_km", "arrival_offset_s", "departure_offset_s"],
            (
                (
                    route.route_id,
                    stop + 1,
                    station + 1,
                    route.distances_km[stop],
                    schedule.arrival_offsets[index][stop],
                    schedule.departure_offsets[index][stop],
                )
                for (index, route) in enumerate(network.routes)
                for (stop, station) in enumerate(route.stop_ids)
            ),
        )
        writer.table(
            "trips",
            ["trip_id", "bus_id", "route_id", "departure_s", "route_end_s"],
            (
                (
                    trip.trip_id,
                    trip.bus_id,
                    network.routes[trip.route_index].route_id,
                    trip.departure_s,
                    trip.departure_s + schedule.route_durations[trip.route_index],
                )
                for trip in schedule.scene_trips()
            ),
        )
        writer.table(
            "stop_times",
            ["trip_id", "bus_id", "route_id", "sequence", "station_id", "arrival_s", "departure_s"],
            (
                (
                    trip.trip_id,
                    trip.bus_id,
                    network.routes[trip.route_index].route_id,
                    stop + 1,
                    station + 1,
                    trip.departure_s + schedule.arrival_offsets[trip.route_index][stop],
                    trip.departure_s + schedule.departure_offsets[trip.route_index][stop],
                )
                for trip in schedule.scene_trips()
                for (stop, station) in enumerate(network.routes[trip.route_index].stop_ids)
            ),
        )
    return path
