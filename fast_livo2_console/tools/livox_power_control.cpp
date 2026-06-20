#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>

#include "livox_lidar_api.h"
#include "livox_lidar_def.h"

namespace {

std::mutex g_mutex;
std::condition_variable g_cv;
std::atomic<bool> g_found{false};
std::atomic<int> g_callbacks{0};
uint32_t g_handle = 0;

void InfoChangeCallback(const uint32_t handle, const LivoxLidarInfo *info, void *) {
  if (!info) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_mutex);
  g_handle = handle;
  g_found = true;
  std::cout << "found lidar handle=" << handle
            << " type=" << static_cast<int>(info->dev_type)
            << " sn=" << info->sn
            << " ip=" << info->lidar_ip << std::endl;
  g_cv.notify_all();
}

void ControlCallback(livox_status status, uint32_t handle,
                     LivoxLidarAsyncControlResponse *response, void *client_data) {
  const char *name = static_cast<const char *>(client_data);
  int ret_code = response ? response->ret_code : -1;
  std::cout << name << " callback handle=" << handle
            << " status=" << status
            << " ret_code=" << ret_code << std::endl;
  ++g_callbacks;
  g_cv.notify_all();
}

bool WaitFound(int seconds) {
  std::unique_lock<std::mutex> lock(g_mutex);
  return g_cv.wait_for(lock, std::chrono::seconds(seconds), [] { return g_found.load(); });
}

void WaitCallbacks(int target, int seconds) {
  std::unique_lock<std::mutex> lock(g_mutex);
  g_cv.wait_for(lock, std::chrono::seconds(seconds), [target] {
    return g_callbacks.load() >= target;
  });
}

}  // namespace

int main(int argc, char **argv) {
  std::string mode = argc > 1 ? argv[1] : "sleep";
  std::string config = argc > 2 ? argv[2] : "/home/jr/fast_livo2_ws/src/livox_ros_driver2/config/MID360_config.json";
  std::string host_ip = argc > 3 ? argv[3] : "192.168.1.5";

  if (mode != "sleep" && mode != "idle" && mode != "normal" && mode != "disable-send") {
    std::cerr << "usage: livox_power_control [sleep|idle|normal|disable-send] [config] [host_ip]" << std::endl;
    return 2;
  }

  DisableLivoxSdkConsoleLogger();
  if (!LivoxLidarSdkInit(config.c_str(), host_ip.c_str(), nullptr)) {
    std::cerr << "LivoxLidarSdkInit failed" << std::endl;
    return 1;
  }

  SetLivoxLidarInfoChangeCallback(InfoChangeCallback, nullptr);
  if (!LivoxLidarSdkStart()) {
    std::cerr << "LivoxLidarSdkStart failed" << std::endl;
    LivoxLidarSdkUninit();
    return 1;
  }

  if (!WaitFound(8)) {
    std::cerr << "No Livox lidar discovered" << std::endl;
    LivoxLidarSdkUninit();
    return 1;
  }

  uint32_t handle = g_handle;
  int expected_callbacks = 0;

  if (mode == "normal") {
    std::cout << "set work mode normal" << std::endl;
    SetLivoxLidarWorkMode(handle, kLivoxLidarNormal, ControlCallback, const_cast<char *>("normal"));
    expected_callbacks += 1;
  } else {
    std::cout << "disable point send" << std::endl;
    DisableLivoxLidarPointSend(handle, ControlCallback, const_cast<char *>("disable-point-send"));
    expected_callbacks += 1;

    std::cout << "disable imu data" << std::endl;
    DisableLivoxLidarImuData(handle, ControlCallback, const_cast<char *>("disable-imu"));
    expected_callbacks += 1;

    if (mode == "sleep" || mode == "idle") {
      std::cout << "set work mode idle" << std::endl;
      SetLivoxLidarWorkMode(handle, kLivoxLidarWakeUp, ControlCallback, const_cast<char *>("idle"));
      expected_callbacks += 1;
    }
  }

  WaitCallbacks(expected_callbacks, 8);
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  LivoxLidarSdkUninit();

  std::cout << "callbacks=" << g_callbacks.load() << "/" << expected_callbacks << std::endl;
  return g_callbacks.load() > 0 ? 0 : 1;
}
