# FAST_LIO Integration

JR扫描仪的雷达建图模式使用 `mkygogo/FAST_LIO`。

小主机工作区：

```bash
cd ~/fast_livo2_ws/src
git clone https://github.com/mkygogo/FAST_LIO.git FAST_LIO
cd FAST_LIO
git submodule update --init --recursive
```

当前 Mid360 驱动使用 `livox_ros_driver2`，所以需要应用：

```bash
git apply ~/fast_livo2_deploy/console/patches/fast_lio_livox_ros_driver2.patch
```

该补丁同时会把 `config/mid360.yaml` 里的 `publish.path_en` 打开，用于浏览器预览里的轨迹线、当前位置标记和俯视跟随。

编译：

```bash
cd ~/fast_livo2_deploy
docker compose run --rm fast-livo2 bash -lc \
  'source /opt/ros/noetic/setup.bash; cd /home/jr/fast_livo2_ws; catkin_make -DROS_EDITION=ROS1 -DCMAKE_BUILD_TYPE=Release'
```

雷达建图启动命令：

```bash
roslaunch fast_lio mapping_mid360.launch rviz:=false
```

控制台接口：

- `POST /api/lio/start_all`：启动 Mid360 驱动和 FAST_LIO 雷达建图
- `POST /api/lio/start`：仅启动 FAST_LIO 雷达建图
- `POST /api/lio/stop`：停止雷达建图
- 预览页选择“雷达建图”后读取 `/cloud_registered` 和 `/path`
