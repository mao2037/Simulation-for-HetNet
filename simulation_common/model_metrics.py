from __future__ import annotations

from pathlib import Path
from statistics import mean, stdev

import numpy as np

from .core import DEMAND_TAG, Network, Scenario, SimulationConfig, model_metrics_folder
from .excel_io import ExcelWriter
from .route_filter import filter_operating_network

VEHICLE_KM_COST = 2.0
VEHICLE_HOUR_COST = 40.0
STRATEGIES = ("earliest", "shortest")
COMPONENT_FIELDS = ("N_k", "N_h", "T_a", "T_w", "T_t", "T_r")
METRIC_FIELDS = (*COMPONENT_FIELDS, "objective_total")


def _objective_row(components: tuple) -> tuple:
    if len(components) != len(COMPONENT_FIELDS):
        raise ValueError("Expected six objective components")
    total = None if None in components else sum(components)
    return (*components, total)


def _write_metrics(
    path: Path,
    metrics: dict[str, tuple],
    config: SimulationConfig,
    deviations: dict[str, tuple] | None = None,
    replications: int | None = None,
) -> None:
    with ExcelWriter(path) as writer:
        writer.table(
            "metrics",
            ("strategy", *METRIC_FIELDS),
            ((strategy, *metrics[strategy]) for strategy in STRATEGIES),
        )
        if deviations is not None:
            writer.table(
                "metrics_std",
                ("strategy", *METRIC_FIELDS),
                ((strategy, *deviations[strategy]) for strategy in STRATEGIES),
            )
        metadata = {
            "first_boarding_mode": "axis_earliest_arrival",
            "scheduling_mode": "route_periodic_coordinated",
            "waiting_time_mode": "departure",
            "demand_mode": DEMAND_TAG,
        }
        if replications is not None:
            metadata.update(replications=replications, std_ddof=1)
        writer.metadata(metadata)


def write_model_metrics_table(
    network: Network, rep: int, times_by_strategy: dict, config: SimulationConfig
) -> dict[str, tuple]:
    operating = filter_operating_network(network, config)
    vehicle_km_h = sum(route.frequency_veh_h * route.length_km for route in operating.routes)
    vehicle_hours_h = sum(
        route.frequency_veh_h
        * (route.length_km / config.bus_speed_kmh + len(route.stop_ids) * config.dwell_s / 3600)
        for route in operating.routes
    )
    if (
        not np.isfinite([VEHICLE_KM_COST, VEHICLE_HOUR_COST]).all()
        or min(VEHICLE_KM_COST, VEHICLE_HOUR_COST) < 0
    ):
        raise ValueError("Vehicle costs must be finite and nonnegative")
    scenario = operating.scenario
    operator_k = 60 * (VEHICLE_KM_COST / scenario.mu) * vehicle_km_h / scenario.demand
    operator_h = 60 * (VEHICLE_HOUR_COST / scenario.mu) * vehicle_hours_h / scenario.demand
    metrics = {
        strategy: _objective_row((operator_k, operator_h, *times_by_strategy[strategy]))
        for strategy in STRATEGIES
    }
    path = model_metrics_folder(config) / f"model_metrics_{scenario.key}_{DEMAND_TAG}_rep_{rep:03d}.xlsx"
    _write_metrics(path, metrics, config)
    return metrics


def write_model_metrics_summary(
    scenario: Scenario,
    replications: list[dict[str, tuple]],
    *,
    filename_tag: str = DEMAND_TAG,
    config: SimulationConfig | None = None,
) -> Path:
    config = SimulationConfig() if config is None else config
    if len(replications) < 2:
        raise ValueError("A scenario mean requires at least two replications")
    averages = {}
    deviations = {}
    for strategy in STRATEGIES:
        rows = [replication[strategy] for replication in replications]
        if any(len(row) != len(METRIC_FIELDS) for row in rows):
            raise ValueError("Each replication must contain six components and objective_total")
        columns = tuple(zip(*rows))
        averages[strategy] = tuple(mean(values) if None not in values else None for values in columns)
        deviations[strategy] = tuple(stdev(values) if None not in values else None for values in columns)
    path = (
        model_metrics_folder(config)
        / f"model_metrics_summary_{scenario.key}_{filename_tag}_reps_{len(replications):03d}.xlsx"
    )
    _write_metrics(path, averages, config, deviations, len(replications))
    return path
