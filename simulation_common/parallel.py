from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import gc
import multiprocessing as mp
from multiprocessing.connection import wait
import os
import signal
import sys
import traceback
from typing import Any, Iterator

from .core import Network, Scenario, SimulationConfig, data_path, require_output
from .demand import generate_od
from .excel_io import load_network
from .pipeline import simulate_scenario
from .schedule import build_scene

_NETWORK_CACHE_KEY = None
_NETWORK_CACHE = None


@dataclass(frozen=True)
class ReplicationTask:
    scenario: Scenario
    rep: int
    config: SimulationConfig
    stages: frozenset[str]


@dataclass(frozen=True)
class ReplicationOutcome:
    scenario: Scenario
    rep: int
    error: str | None = None
    metrics: dict[str, tuple] | None = None


@dataclass
class _Worker:
    process: Any
    connection: Any
    task: ReplicationTask | None = None


def _cached_network(scenario: Scenario) -> Network:
    global _NETWORK_CACHE_KEY, _NETWORK_CACHE
    path = require_output(data_path("network", scenario), "network")
    stamp = path.stat()
    key = (path, stamp.st_mtime_ns, stamp.st_size)
    if key != _NETWORK_CACHE_KEY:
        _NETWORK_CACHE, _NETWORK_CACHE_KEY = None, None
        gc.collect()
        _NETWORK_CACHE = load_network(path, expected_scenario=scenario)
        _NETWORK_CACHE_KEY = key
    return _NETWORK_CACHE


def execute_replication(task: ReplicationTask) -> ReplicationOutcome:
    try:
        metrics = None
        network = _cached_network(task.scenario) if task.stages & {"scene", "simulation"} else None
        if "od" in task.stages:
            generate_od(task.scenario, task.rep, task.config)
        if "scene" in task.stages:
            build_scene(network, task.rep, task.config)
        if "simulation" in task.stages:
            metrics = simulate_scenario(network, task.rep, task.config)
    except Exception:
        return ReplicationOutcome(task.scenario, task.rep, traceback.format_exc())
    return ReplicationOutcome(task.scenario, task.rep, metrics=metrics)


def _worker_loop(connection) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        while True:

            task = connection.recv()
            if task is None:
                return
            connection.send(execute_replication(task))
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


@contextmanager
def _child_thread_limit():


    names = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "1"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _start_worker(context, slot: int) -> _Worker:


    parent, child = context.Pipe()
    process = context.Process(target=_worker_loop, args=(child,), name=f"simulation_worker_{slot}")
    try:
        with _child_thread_limit():
            process.start()
    except BaseException:

        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        parent.close()
        child.close()
        process.close()
        raise
    child.close()
    return _Worker(process, parent)


def _stop_worker(worker: _Worker, terminate: bool = False) -> None:
    if worker.process.is_alive():
        if terminate:
            worker.process.terminate()
        else:
            try:
                worker.connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                worker.process.terminate()
    worker.process.join(timeout=5)
    if worker.process.is_alive():
        worker.process.kill()
        worker.process.join(timeout=5)
    worker.connection.close()
    worker.process.close()


def _print_replication_progress(task: ReplicationTask, number: int, total: int) -> None:
    print(f"开始实验 {number}/{total} | {task.scenario.key} | 重复 {task.rep}", flush=True)


def run_parallel_tasks(tasks: list[ReplicationTask], workers: int) -> Iterator[ReplicationOutcome]:


    total = len(tasks)
    if not total:
        return
    print(f"实验总数 {total}", flush=True)
    count = min(workers, total)
    if count == 1:
        for number, task in enumerate(tasks, 1):
            _print_replication_progress(task, number, total)
            yield execute_replication(task)
        return

    context, active, cursor = mp.get_context("spawn"), [None] * count, 0

    outcomes = deque()

    try:
        while cursor < total or outcomes or any(worker is not None and worker.task is not None for worker in active):
            for slot, worker in enumerate(active):
                if cursor >= total:
                    break
                if worker is not None and worker.task is not None:
                    continue
                if worker is None:
                    worker = _start_worker(context, slot)
                    active[slot] = worker

                task = tasks[cursor]
                cursor += 1
                worker.task = task
                _print_replication_progress(task, cursor, total)
                try:
                    worker.connection.send(task)
                except (BrokenPipeError, EOFError, OSError) as failure:

                    outcomes.append(ReplicationOutcome(task.scenario, task.rep, str(failure)))
                    _stop_worker(worker, terminate=True)
                    active[slot] = None
            if outcomes:
                yield outcomes.popleft()
                continue

            watched = [handle for worker in active if worker is not None and worker.task is not None
                       for handle in (worker.connection, worker.process.sentinel)]
            ready = wait(watched, timeout=1)
            for slot, worker in enumerate(active):
                if worker is None or worker.task is None:
                    continue
                if worker.connection not in ready and worker.process.sentinel not in ready:
                    continue

                task, outcome, dead = worker.task, None, False
                if worker.connection in ready:
                    try:
                        outcome = worker.connection.recv()
                    except (EOFError, OSError):
                        dead = True
                else:
                    dead = True
                if dead:

                    message = f"Worker {worker.process.pid} exited with code {worker.process.exitcode}"
                    outcome = ReplicationOutcome(task.scenario, task.rep, message)
                    _stop_worker(worker, terminate=True)
                    active[slot] = None
                elif not isinstance(outcome, ReplicationOutcome) or (outcome.scenario, outcome.rep) != (task.scenario, task.rep):
                    raise RuntimeError("子进程返回的实验身份与当前任务不一致")
                else:
                    worker.task = None
                outcomes.append(outcome)
    finally:


        cleanup_errors, unwinding = [], sys.exc_info()[0] is not None
        for worker in active:
            if worker is not None:
                try:
                    _stop_worker(worker, terminate=worker.task is not None)
                except Exception as failure:
                    cleanup_errors.append(str(failure))
        if cleanup_errors:
            print("Worker cleanup failed: " + ", ".join(cleanup_errors), file=sys.stderr, flush=True)
            if not unwinding:
                raise RuntimeError("工作进程清理未完成：" + "; ".join(cleanup_errors))
