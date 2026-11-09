# 公交网络离散化与乘客仿真

项目沿用网络离散化、正式 OD 抽样、车辆排班和乘客离散事件仿真四个阶段。两种策略 `earliest` 和 `shortest` 使用同一份 OD、预热需求和公交班次，分别计算最先可乘车辆和最短里程策略。

## 运行环境

使用 Python 3.9 或以上版本。在项目目录安装依赖。

```sh
python -m pip install -r requirements.txt
```

重新导出网络时需要将 MATLAB 加入 `PATH`。其余阶段由 Python 执行。

## 运行方式

`run_pipeline.py` 默认依次生成排班和执行乘客仿真，处理六个内置场景，每个场景重复 10 次，最多使用 2 个工作进程。默认场景均为一小时 100,000 人、`mu=25`、`alpha=0.5`、城市边长 10 公里。首次运行时，可以先生成网络和 OD，再使用默认阶段。程序在控制台显示实验总数，以及每个启动任务的序号、场景和重复号。

```sh
python run_pipeline.py --stages network,od
python run_pipeline.py
python run_pipeline.py --mode single --city Monocentric --rep 1 --stages network,od,scene,simulation
python run_pipeline.py --mode single --city Monocentric --rep 1 --stages simulation
```

`--mode single` 选择单个场景，可通过 `--city`、`--demand`、`--mu`、`--alpha` 和 `--citysize` 指定五参数。`--mode selected` 运行脚本顶部 `SELECTED_SCENARIOS` 中的批量场景。`--rep N` 只处理第 N 次重复，`--replications N` 处理第 1 至 N 次。`--stages` 接受逗号分隔的阶段名，按 `network`、`od`、`scene`、`simulation` 的顺序执行所选阶段。

发布版固定采用 `cell_conservative`。正式和预热 OD 均按原网格节点联合概率选取起终点单元，再按四维插值密度在单元内连续抽样。乘客行为沿用曼哈顿步行、最近站对不可达修复、一次换乘约束，以及各候选站首辆合规末程车的终点到达时间比较。线路按原始车头时距严格小于 120 分钟筛选，删除线路的频率按方向回分给保留线路。单车容量为 100,000 人。数值参数可在 `simulation_common/core.py` 的 `SimulationConfig` 中修改。更换需求 MAT 后需重新生成 `od`。更换种子后需重新生成 `od` 和 `scene`。

## 输入与输出

`inputs/` 随项目提供 Monocentric、Commuter、checkerboard1、checkerboard2、checkerboard3 和 checkerboard4 的输入。四个棋盘场景沿用改名前的 OD 与排班随机流。

| 目录 | 文件 |
| --- | --- |
| `inputs/model/` | 含 `delta_EW`、`delta_NS`、`h_E`、`h_W`、`h_N`、`h_S` 的连续模型解 |
| `inputs/demand/<city>/` | 含 `lambda_OD` 和 `D` 的需求 MAT，供正式及预热 OD 抽样 |

所有数据路径均相对于项目目录。`scene` 从 `outputs/` 读取对应场景的 `network`。`simulation` 从 `outputs/` 读取该场景的 `network`，以及对应重复号的 `od` 和 `scene`。前置文件缺失时，程序报出缺失路径并提示需要补跑的阶段。

增加场景时，将匹配的模型和需求 MAT 放入对应 `inputs/` 目录，并运行所需阶段。模型文件使用 `<city>_demand_<demand>_mu_<mu>_alpha_<alpha>_citysize_<citysize>.mat`。四个内置棋盘场景的需求文件使用 `<city>_lambda_<demand>_citysize_<citysize>.mat`，其余场景使用 `lambda_<demand>_citysize_<citysize>.mat`。

| 输出目录 | 工作簿内容 |
| --- | --- |
| `outputs/network/` | 离散线路、几何线段、站点及其关联 |
| `outputs/od/` | 正式需求时段每位乘客的出发时刻和起终点坐标，工作簿直接保存在该目录 |
| `outputs/scene/` | 运营线路、排班和逐站到发时刻，工作簿直接保存在该目录 |
| `outputs/model_metrics/` | 逐次结果和场景均值工作簿，每策略一行 |

`metrics` 工作表仅含策略标识和六项目标函数分项。`N_k`、`N_h` 是运营里程与运营时间成本，按时间价值折算为每位正式需求乘客的等价分钟。`T_a`、`T_w`、`T_t`、`T_r` 分别为加权步行、普通候车、换乘衔接和车内时间，按正式需求人数归一。没有完成者时四项乘客时间留空。

每个场景在本次选中的重复全部成功后，自动写出 `model_metrics_summary_<场景键>_cell_conservative_reps_<次数>.xlsx`。默认汇总 10 次。每个分项在同一策略内取各次结果的算术平均值。某分项在任一次重复中为空时，其均值留空。单独运行一个 `--rep` 时只写逐次指标。

逐次指标文件名含 `cell_conservative`。再次执行同一场景及重复号时会替换相应工作簿。已有 `network` 和 `scene` 时，切换到这一版后可用 `--stages od,simulation` 生成新口径的 OD 和指标。
