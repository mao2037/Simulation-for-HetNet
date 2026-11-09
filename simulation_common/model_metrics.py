from __future__ import annotations

from pathlib import Path
from statistics import mean

import numpy as np

from .core import DEMAND_TAG, Network, Scenario, SimulationConfig, project_path
from .excel_io import ExcelWriter
from .route_filter import filter_operating_network

VEHICLE_KM_COST = 2.0
VEHICLE_HOUR_COST = 40.0
STRATEGIES = ("earliest", "shortest")
METRIC_FIELDS = ("N_k", "N_h", "T_a", "T_w", "T_t", "T_r")


def _write_metrics(path: Path, metrics: dict[str, tuple]) -> None:
    with ExcelWriter(path) as writer:
        writer.table("metrics", ("strategy", *METRIC_FIELDS),
                     ((strategy, *metrics[strategy]) for strategy in STRATEGIES))


def write_model_metrics_table(network: Network, rep: int, times_by_strategy: dict,
                              config: SimulationConfig) -> dict[str, tuple]:
    operating = filter_operating_network(network, config)
    vehicle_km_h = sum(route.frequency_veh_h * route.length_km for route in operating.routes)
    vehicle_hours_h = sum(route.frequency_veh_h * (route.length_km / config.bus_speed_kmh
                          + len(route.stop_ids) * config.dwell_s / 3600) for route in operating.routes)
    if not np.isfinite([VEHICLE_KM_COST, VEHICLE_HOUR_COST]).all() or min(VEHICLE_KM_COST, VEHICLE_HOUR_COST) < 0:
        raise ValueError("Vehicle costs must be finite and nonnegative")
    scenario = operating.scenario
    operator_k = 60 * (VEHICLE_KM_COST / scenario.mu) * vehicle_km_h / scenario.demand
    operator_h = 60 * (VEHICLE_HOUR_COST / scenario.mu) * vehicle_hours_h / scenario.demand
    metrics = {strategy: (operator_k, operator_h, *times) for strategy, times in times_by_strategy.items()}
    path = project_path("outputs", "model_metrics", f"model_metrics_{scenario.key}_{DEMAND_TAG}_rep_{rep:03d}.xlsx")
    _write_metrics(path, metrics)
    return metrics


def write_model_metrics_summary(scenario: Scenario, replications: list[dict[str, tuple]],
                                *, filename_tag: str = DEMAND_TAG) -> Path:
    if len(replications) < 2:
        raise ValueError("A scenario mean requires at least two replications")
    averages = {}
    for strategy in STRATEGIES:
        columns = zip(*(replication[strategy] for replication in replications))
        averages[strategy] = tuple(mean(values) if None not in values else None for values in columns)
    path = project_path("outputs", "model_metrics",
                        f"model_metrics_summary_{scenario.key}_{filename_tag}_reps_{len(replications):03d}.xlsx")
    _write_metrics(path, averages)
    return path
