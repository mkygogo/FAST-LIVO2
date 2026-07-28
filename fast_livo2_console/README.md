# JR扫描仪 Touch Console

本机触摸屏控制台，默认监听 `127.0.0.1:8090`。

功能：
- Mid360 启停、检查、ROS topic 状态
- JR扫描仪雷达建图启停
- JR扫描仪融合算法预留启停
- Mid360 rosbag 录制启停
- `/livox/lidar` 和 `/cloud_registered` 降采样点云预览
- 日志查看和性能快照

服务文件：
- `/home/jr/fast_livo2_deploy/console/server.py`
- `/home/jr/fast_livo2_data/tools/ros_point_stream.py`
- `/etc/systemd/system/fast-livo2-console.service`

常用命令：

```bash
sudo systemctl status fast-livo2-console.service
sudo systemctl restart fast-livo2-console.service
xdg-open http://localhost:8090
```

## 相机内参标定

仓库内提供两种 OpenCV 棋盘格标定入口，默认棋盘格为 `11x8` 内角点、
方格边长 `0.025 m`：

```bash
# 海康 MVS 工业相机（推荐）
fast_livo2_console/scripts/start_dev_mvs_camera_calibration.sh

# 已采集图片离线重算
fast_livo2_console/scripts/solve_dev_mvs_camera_calibration.sh \
  --calib-dir ~/fast_livo2_data/calib/camera_intrinsics/jr_mvs/<session>
```

标定结果默认写入 `~/fast_livo2_data/calib/camera_intrinsics/`，不写入源码仓库。
输出同时包含 ROS `camera_intrinsics.yaml` 和供 FAST-Calib2 使用的
`fast_calib2_intrinsics.yaml`。FAST-Calib2 是独立的雷达—相机外参标定项目，
不包含在本仓库中。

## FAST-Calib2 雷达—相机外参标定

小主机桌面入口 `FAST-Calib2雷达相机标定` 提供完整菜单：启动/停止标定
设备、状态检查、录制场景、单组求解、多组联合求解和结果查看。设备入口为：

```bash
fast_livo2_console/scripts/fast_calib2_devices.sh start
fast_livo2_console/scripts/fast_calib2_devices.sh status
fast_livo2_console/scripts/fast_calib2_devices.sh stop
```

它只启动 Mid360 驱动和海康 `Line0/RisingEdge` 外触发相机，不启动
FAST-LIVO2 或 RViz；检测到正在建图时会拒绝切换设备。新相机数据使用
`~/fast_livo2_data/calib/fast_calib2_mv_cs050/`，与旧相机数据隔离。

Mid360 在部分标定板角度下，四个反光圆环的回波强度可能不一致。部署
FAST-Calib2 源码后还需应用
`patches/fast_calib2_mid360_annulus_threshold.patch`；该补丁仅在相对高强度
阈值被少量饱和点抬高时回退到稳定前景阈值，不改变圆簇点数、半径、拟合
残差和四圆几何验收条件。补丁同时避免粗 ROI 在阈值比例仅约 1.58 时过早
回退到 p92，否则偏右场景会把背景反光扩大成 17 个候选簇。

“录制一组标定数据”会打开 1024×600 全屏实时取景：左侧持续显示相机
画面并标出 ArUco，右侧显示 1/2/3/4 标记数量、标定板是否离边缘过近、
亮度和相机信号。推荐在 `4/4 + SAFE + Brightness OK` 时开始；至少检测到
3 个标记才允许录制。3 秒录制期间画面和倒计时持续更新，完成后显示
bag 大小并保持实时预览。bag 索引完成后，界面会自动调用维护版
`lidar_center_test` 检查 Mid360 是否提取到四个满足几何约束的反光圆环。
只有显示 `LiDAR circles 4/4` 才将该组标记为有效；若只找到例如 `2/4`，
界面会显示无效并允许调整标定板角度后直接重录，无需退出后运行单组标定
才发现错误。

标定板和扫描仪在这 3 秒内必须完全静止。实机验证中，12 秒内约 15 cm
的支架移动会把上下圆环轨迹连接成两列并导致自动 ROI 失败；同一数据的
静止开头 3 秒可稳定提取 4 圆并得到约 1.5 mm 单组 RMSE。

“单组标定（最新数据）”按目录实际修改时间选择数据，不能按目录名排序，
因为主机与容器可能分别使用北京时间和 UTC。求解节点写出结果后会被单独
结束，正常约 5–10 秒返回，45 秒为硬超时。雷达未找到 4 个反光圆或结果
矩阵接近全零时必须返回失败，不能把文件存在误判为标定成功。

“多组联合标定”同样按实际修改时间只选择最新三组完整数据（通常为正面、
偏左、偏右），并先调用一次性单组求解逐组验证四圆。任何一组失败都会明确
显示数据集名称并停止，不会继续扫描历史失败数据或用旧数据顶替。三组均通过
后，脚本在现有相机容器内直接执行一次性的 `multi_fast_calib`。带有录后
质检元数据的新数据必须同时满足 `complete + passed + 4/4` 才属于完整数据；
点击 Retry 留下的 `invalid_lidar` 诊断 bag 会保留，但不会占用最新三组名额。

硬件调整后，2026-07-27 的偏左、正面、偏右三组有效数据单组 RMSE 分别为
1.1、1.9、2.1 mm，联合 RMSE 为 `0.0024 m`，结果目录为
`20260727-140300-multi`。外参 `Rcl/Pcl` 已写入 FAST-LIVO2 的
`config/mid360.yaml`；`config/camera_pinhole.yaml` 只保存新相机的内参。相机、
镜头或雷达安装位置发生变化后必须重新标定。
