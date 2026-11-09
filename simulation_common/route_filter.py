from __future__ import annotations

from dataclasses import replace

import numpy as np

from .core import Network, SimulationConfig


def route_filter_settings(config: SimulationConfig) -> dict:
    return {"version": "direction_frequency_tail_v1_strict_max_headway_v2",
            "max_headway_min": float(config.route_max_headway_min)}


def filter_operating_network(network: Network, config: SimulationConfig) -> Network:
    settings = route_filter_settings(config)
    previous = (network.metadata or {}).get("route_filter_settings")
    if previous is not None:
        if previous != settings:
            raise ValueError("Reload the original network to change the headway threshold")
        return network
    network.validate()
    keep = np.zeros(len(network.routes), dtype=bool)
    scales = np.ones(len(network.routes), dtype=float)
    for direction in ("E", "W", "N", "S"):
        indices = np.array([index for index, route in enumerate(network.routes) if route.direction == direction],
                           dtype=np.int64)
        if not len(indices):
            continue
        frequencies = np.array([network.routes[index].frequency_veh_h for index in indices])
        selected = 60.0 / frequencies < config.route_max_headway_min
        if not selected.any():
            raise ValueError(f"Headway threshold removed every route in direction {direction}")
        keep[indices[selected]] = True
        scales[indices] = float(frequencies.sum()) / float(frequencies[selected].sum())
    routes = [replace(route, frequency_veh_h=route.frequency_veh_h * scales[index])
              for index, route in enumerate(network.routes) if keep[index]]
    metadata = {**(network.metadata or {}), "route_filter_settings": settings}
    operating = Network(network.scenario, network.stations, routes, metadata)
    operating.validate()
    return operating
