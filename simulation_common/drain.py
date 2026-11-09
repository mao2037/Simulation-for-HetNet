from __future__ import annotations

from math import floor

import numpy as np

from .core import Network


def next_boarding_time(schedule, network: Network, groups: list[dict], now: float) -> float:


    if not np.isfinite(now):
        raise ValueError("搜索起点 now 必须是有限秒数")

    needed: dict[int, set[int]] = {}

    exclusions = {}
    for station_groups in groups:
        for group in station_groups.values():
            if not group.passengers:
                continue

            count = 0
            for (route_index, sequence), alight in group.plan.options.items():
                if not 0 <= route_index < len(network.routes):
                    raise ValueError("候车组选项引用了不存在的完整线路")

                route = network.routes[route_index]
                if route.frequency_veh_h <= 0 or not 0 <= sequence < alight < len(route.stop_ids):
                    raise ValueError("候车组选项没有正频率服务或有效下游站")
                needed.setdefault(int(route_index), set()).add(int(sequence))
                exclusions.setdefault((int(route_index), int(sequence)), set()).add(getattr(group, "excluded_trip_id", None))
                count += 1
            if count == 0:
                raise ValueError("仍有乘客候车，但其路由计划没有任何可乘车辆")
    if not needed:
        raise ValueError("没有需要搜索车辆服务的活跃候车组")

    route_indices = set(needed)

    maximum_duration = max(float(schedule.route_durations[index]) for index in route_indices)
    if not np.isfinite(maximum_duration) or maximum_duration < 0:
        raise ValueError("车辆运行时长必须是有限非负秒数")

    hour = floor((now - maximum_duration) / 3600)
    best = np.inf


    while True:


        if hour * 3600 > best:
            return float(best)
        for trip in schedule.hour_trips_for_routes(hour, route_indices):

            route_index = trip.route_index
            if route_index not in needed:
                continue
            for sequence in needed[route_index]:
                if exclusions[(route_index, sequence)] == {trip.trip_id}:
                    continue

                arrival = float(trip.departure_s + schedule.arrival_offsets[route_index][sequence])
                departure = float(trip.departure_s + schedule.departure_offsets[route_index][sequence])
                if departure >= now:
                    best = min(best, max(float(now), arrival))
        hour += 1
