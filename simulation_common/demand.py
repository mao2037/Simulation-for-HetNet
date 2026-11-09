from __future__ import annotations

from pathlib import Path

import numpy as np

from .core import PassengerInput, Scenario, SimulationConfig, od_path, stable_seed
from .excel_io import read_metadata, write_passengers
from .cell_conservative_demand import CELL_CONSERVATIVE_DEMAND_VERSION, demand_source_identity, load_cell_conservative_demand


def generate_passengers(scenario: Scenario, rep: int, config: SimulationConfig,
                        start_s: float = 0, duration_s: float = 3600,
                        stream: str = "formal") -> PassengerInput:


    if rep < 1 or duration_s < 0 or not np.isfinite([start_s, duration_s]).all():
        raise ValueError("重复号必须为正整数，时段必须为有限非负长度")

    count = int(round(scenario.demand * duration_s / 3600))
    seed_fields = (int(config.base_seed), scenario.seed_city, int(scenario.demand), float(scenario.citysize),
                   int(rep), stream, float(start_s), float(duration_s), CELL_CONSERVATIVE_DEMAND_VERSION)
    rng = np.random.default_rng(stable_seed(*seed_fields))

    departure = rng.uniform(start_s, start_s + duration_s, count)
    od, _ = load_cell_conservative_demand(scenario)
    origin, destination = od.sample(count, rng)

    identifiers = np.arange(1, count + 1, dtype=np.int64) + (0 if stream == "formal" else scenario.demand)

    order = np.lexsort((identifiers, departure))
    return PassengerInput(identifiers[order], departure[order], origin[order], destination[order])


def generate_warmup(scenario: Scenario, rep: int, config: SimulationConfig) -> PassengerInput:


    parts = []

    start = -config.warmup_s
    while start < 0:

        duration = min(3600.0, -start)
        parts.append(generate_passengers(scenario, rep, config, start, duration, "warmup"))
        start += duration
    if not parts:
        return PassengerInput(np.empty(0, dtype=np.int64), np.empty(0), np.empty((0, 2)), np.empty((0, 2)))

    departure = np.concatenate([part.departure_s for part in parts])
    origin = np.concatenate([part.origin_xy for part in parts])
    destination = np.concatenate([part.destination_xy for part in parts])
    return PassengerInput(np.arange(scenario.demand + 1, scenario.demand + len(departure) + 1, dtype=np.int64), departure, origin, destination)


def generate_od(scenario: Scenario, rep: int, config: SimulationConfig) -> Path:
    path = od_path(scenario, rep)
    _, source = load_cell_conservative_demand(scenario)
    identity = {**scenario.to_dict(), "rep": rep, "base_seed": config.base_seed,
                "formal_start_s": 0, "formal_end_s": 3600, **demand_source_identity(source)}
    if path.is_file():
        existing = read_metadata(path)
        if all(existing.get(name) == value for name, value in identity.items()):
            return path
    metadata = {**identity, **source, "formal_passenger_count": scenario.demand,
                "coordinate_unit": "km", "time_unit": "s"}
    passengers = generate_passengers(scenario, rep, config)
    return write_passengers(path, passengers, metadata, config.sheet_rows)
