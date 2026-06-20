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
