from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
import sys
import traceback

from simulation_common.core import Scenario, SimulationConfig
from simulation_common.model_metrics import write_model_metrics_summary
from simulation_common.parallel import ReplicationTask, run_parallel_tasks
from simulation_common.pipeline import export_network, missing_prerequisites

RUN_MODE = "selected"
STAGES = "scene,simulation"
STAGE_ORDER = ("network", "od", "scene", "simulation")
SINGLE_SCENARIO = dict(city="Monocentric", demand=100000, mu=25, alpha=0.5, citysize=10)
SELECTED_SCENARIOS = [
    dict(SINGLE_SCENARIO, city=city)
    for city in ("Monocentric", "Commuter", "checkerboard1", "checkerboard2", "checkerboard3", "checkerboard4")
]
REPLICATIONS = 10
MAX_WORKERS = 2
SIMULATION = SimulationConfig()


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bus network and passenger simulation pipeline.")
    parser.add_argument("--mode", choices=("single", "selected"), default=RUN_MODE)
    parser.add_argument("--stages", default=STAGES, help="Comma-separated stages: network,od,scene,simulation")
    parser.add_argument("--city")
    parser.add_argument("--demand", type=positive_int)
    parser.add_argument("--mu", type=float)
    parser.add_argument("--alpha", type=float)
    parser.add_argument("--citysize", type=float)
    repetitions = parser.add_mutually_exclusive_group()
    repetitions.add_argument("--replications", type=positive_int, default=REPLICATIONS)
    repetitions.add_argument("--rep", type=positive_int)
    args = parser.parse_args()

    stages = frozenset(stage.strip() for stage in args.stages.split(","))
    if not stages.issubset(STAGE_ORDER):
        parser.error("--stages accepts only network,od,scene,simulation")
    if type(MAX_WORKERS) is not int or MAX_WORKERS < 1:
        parser.error("MAX_WORKERS must be a positive integer")
    if type(args.replications) is not int or args.replications < 1:
        parser.error("REPLICATIONS must be a positive integer")

    overrides = {key: getattr(args, key) for key in SINGLE_SCENARIO if getattr(args, key) is not None}
    try:
        if args.mode == "single":
            scenarios = [Scenario.from_dict({**SINGLE_SCENARIO, **overrides})]
        else:
            if overrides:
                parser.error("Scenario arguments require --mode single")
            scenarios = list(dict.fromkeys(Scenario.from_dict(entry) for entry in SELECTED_SCENARIOS))
    except (TypeError, ValueError, KeyError) as failure:
        parser.error(str(failure))
    if not scenarios:
        parser.error("SELECTED_SCENARIOS must contain at least one scenario")

    reps = (args.rep,) if args.rep is not None else range(1, args.replications + 1)
    tasks = []
    failed = False
    repetition_stages = stages - {"network"}
    if not repetition_stages:
        print(f"实验总数 {len(scenarios)}", flush=True)
    for number, scenario in enumerate(scenarios, 1):
        missing = missing_prerequisites(scenario, reps, stages)
        if missing:
            needed_stages = ",".join(stage for stage in STAGE_ORDER if stage in stages or stage in missing)
            files = "\n".join(f"  {stage} 工作簿 {path}" for stage, path in missing.items())
            print(f"{scenario.key} 缺少前置文件（每个阶段显示第一项）。\n{files}\n"
                  f"请先生成这些文件，或保留场景和重复号参数并使用 --stages {needed_stages}。",
                  file=sys.stderr)
            failed = True
            continue
        if "network" in stages:
            label = "准备网络" if repetition_stages else "开始实验"
            print(f"{label} {number}/{len(scenarios)} | {scenario.key}", flush=True)
            try:
                export_network(scenario)
            except Exception:
                print(f"{scenario.key} network export failed\n{traceback.format_exc()}", file=sys.stderr)
                failed = True
                continue
        if repetition_stages:
            tasks.extend(ReplicationTask(scenario, rep, SIMULATION, repetition_stages) for rep in reps)
    completed = defaultdict(dict)
    with closing(run_parallel_tasks(tasks, MAX_WORKERS)) as outcomes:
        for outcome in outcomes:
            if outcome.error:
                print(f"{outcome.scenario.key} rep {outcome.rep} failed\n{outcome.error}", file=sys.stderr)
                failed = True
            elif outcome.metrics is not None and len(reps) > 1:
                replications = completed[outcome.scenario]
                replications[outcome.rep] = outcome.metrics
                if len(replications) == len(reps):
                    try:
                        path = write_model_metrics_summary(outcome.scenario, [replications[rep] for rep in reps])
                        print(f"已生成场景均值汇总 {path}", flush=True)
                    except Exception:
                        print(f"{outcome.scenario.key} 汇总失败\n{traceback.format_exc()}", file=sys.stderr)
                        failed = True
                    del completed[outcome.scenario]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
