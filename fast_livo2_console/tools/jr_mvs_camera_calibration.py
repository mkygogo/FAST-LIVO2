#!/usr/bin/env python3
import argparse
from ctypes import *
import os
import pathlib
import sys
import time

import cv2
import numpy as np

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from jr_usb_camera_calibration import UsbCalibrationApp  # noqa: E402


MVS_ROOT = pathlib.Path(os.environ.get("MVCAM_SDK_PATH", "/opt/MVS"))
MVS_IMPORT_DIR = MVS_ROOT / "Samples" / "64" / "Python" / "MvImport"
if str(MVS_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(MVS_IMPORT_DIR))

os.environ.setdefault("MVCAM_SDK_PATH", str(MVS_ROOT))
os.environ.setdefault("MVCAM_COMMON_RUNENV", str(MVS_ROOT / "lib"))
os.environ.setdefault("MVCAM_GENICAM_CLPROTOCOL", str(MVS_ROOT / "lib" / "CLProtocol"))
os.environ.setdefault("ALLUSERSPROFILE", str(MVS_ROOT / "MVFG"))

from MvCameraControl_class import *  # noqa: E402,F403


HB_PIXEL_FORMATS = {
    PixelType_Gvsp_HB_Mono8,
    PixelType_Gvsp_HB_Mono10,
    PixelType_Gvsp_HB_Mono10_Packed,
    PixelType_Gvsp_HB_Mono12,
    PixelType_Gvsp_HB_Mono12_Packed,
    PixelType_Gvsp_HB_Mono16,
    PixelType_Gvsp_HB_RGB8_Packed,
    PixelType_Gvsp_HB_BGR8_Packed,
    PixelType_Gvsp_HB_RGBA8_Packed,
    PixelType_Gvsp_HB_BGRA8_Packed,
    PixelType_Gvsp_HB_RGB16_Packed,
    PixelType_Gvsp_HB_BGR16_Packed,
    PixelType_Gvsp_HB_RGBA16_Packed,
    PixelType_Gvsp_HB_BGRA16_Packed,
    PixelType_Gvsp_HB_YUV422_Packed,
    PixelType_Gvsp_HB_YUV422_YUYV_Packed,
    PixelType_Gvsp_HB_BayerGR8,
    PixelType_Gvsp_HB_BayerRG8,
    PixelType_Gvsp_HB_BayerGB8,
    PixelType_Gvsp_HB_BayerBG8,
    PixelType_Gvsp_HB_BayerRBGG8,
    PixelType_Gvsp_HB_BayerGB10,
    PixelType_Gvsp_HB_BayerGB10_Packed,
    PixelType_Gvsp_HB_BayerBG10,
    PixelType_Gvsp_HB_BayerBG10_Packed,
    PixelType_Gvsp_HB_BayerRG10,
    PixelType_Gvsp_HB_BayerRG10_Packed,
    PixelType_Gvsp_HB_BayerGR10,
    PixelType_Gvsp_HB_BayerGR10_Packed,
    PixelType_Gvsp_HB_BayerGB12,
    PixelType_Gvsp_HB_BayerGB12_Packed,
    PixelType_Gvsp_HB_BayerBG12,
    PixelType_Gvsp_HB_BayerBG12_Packed,
    PixelType_Gvsp_HB_BayerRG12,
    PixelType_Gvsp_HB_BayerRG12_Packed,
    PixelType_Gvsp_HB_BayerGR12,
    PixelType_Gvsp_HB_BayerGR12_Packed,
}

MONO_PIXEL_FORMATS = {
    PixelType_Gvsp_Mono8,
    PixelType_Gvsp_Mono10,
    PixelType_Gvsp_Mono10_Packed,
    PixelType_Gvsp_Mono12,
    PixelType_Gvsp_Mono12_Packed,
    PixelType_Gvsp_Mono14,
    PixelType_Gvsp_Mono16,
}


def c_string(chars):
    return "".join(chr(c) for c in chars if c != 0)


def enumerate_devices():
    device_list = MV_CC_DEVICE_INFO_LIST()
    tlayer_type = (
        MV_GIGE_DEVICE
        | MV_USB_DEVICE
        | MV_GENTL_CAMERALINK_DEVICE
        | MV_GENTL_CXP_DEVICE
        | MV_GENTL_XOF_DEVICE
    )
    ret = MvCamera.MV_CC_EnumDevices(tlayer_type, device_list)
    if ret != 0:
        raise RuntimeError(f"MVS enum devices failed: 0x{ret:x}")
    devices = []
    for index in range(device_list.nDeviceNum):
        info = cast(device_list.pDeviceInfo[index], POINTER(MV_CC_DEVICE_INFO)).contents
        item = {"index": index, "layer": int(info.nTLayerType)}
        if info.nTLayerType == MV_USB_DEVICE:
            item.update(
                {
                    "type": "USB",
                    "model": c_string(info.SpecialInfo.stUsb3VInfo.chModelName),
                    "serial": c_string(info.SpecialInfo.stUsb3VInfo.chSerialNumber),
                }
            )
        elif info.nTLayerType == MV_GIGE_DEVICE or info.nTLayerType == MV_GENTL_GIGE_DEVICE:
            item.update(
                {
                    "type": "GigE",
                    "model": c_string(info.SpecialInfo.stGigEInfo.chModelName),
                    "serial": c_string(info.SpecialInfo.stGigEInfo.chSerialNumber),
                }
            )
        else:
            item.update({"type": "Other", "model": "", "serial": ""})
        devices.append(item)
    return device_list, devices


def warn_if_failed(label, ret):
    if ret != 0:
        print(f"Warning: {label} failed: 0x{ret:x}", flush=True)


class MvsCameraSource:
    def __init__(
        self,
        device_index=None,
        serial=None,
        exposure_us=0.0,
        exposure_auto="Continuous",
        gain=0.0,
        gain_auto="Continuous",
        gamma=1.0,
        saturation=128,
        sharpness=0,
        balance_white_auto="Continuous",
        timeout_ms=1000,
    ):
        self.device_index = device_index
        self.serial = serial
        self.exposure_us = exposure_us
        self.exposure_auto = exposure_auto
        self.gain = gain
        self.gain_auto = gain_auto
        self.gamma = gamma
        self.saturation = saturation
        self.sharpness = sharpness
        self.balance_white_auto = balance_white_auto
        self.timeout_ms = int(timeout_ms)
        self.device_list = None
        self.cam = None
        self.started = False

    def open(self):
        MvCamera.MV_CC_Initialize()
        self.device_list, devices = enumerate_devices()
        if not devices:
            raise RuntimeError("No MVS camera found")
        for item in devices:
            print(
                f"MVS device[{item['index']}] {item['type']} model={item.get('model', '')} serial={item.get('serial', '')}",
                flush=True,
            )

        index = self._select_device(devices)
        info = cast(self.device_list.pDeviceInfo[index], POINTER(MV_CC_DEVICE_INFO)).contents
        self.cam = MvCamera()
        ret = self.cam.MV_CC_CreateHandle(info)
        if ret != 0:
            raise RuntimeError(f"MVS create handle failed: 0x{ret:x}")
        ret = self.cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if ret != 0:
            raise RuntimeError(f"MVS open device failed: 0x{ret:x}")

        if info.nTLayerType == MV_GIGE_DEVICE or info.nTLayerType == MV_GENTL_GIGE_DEVICE:
            packet_size = self.cam.MV_CC_GetOptimalPacketSize()
            if int(packet_size) > 0:
                self.cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)

        warn_if_failed("set TriggerMode Off", self.cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF))
        if self.exposure_us > 0:
            warn_if_failed("set ExposureAuto Off", self.cam.MV_CC_SetEnumValueByString("ExposureAuto", "Off"))
            warn_if_failed("set ExposureTime", self.cam.MV_CC_SetFloatValue("ExposureTime", float(self.exposure_us)))
        elif self.exposure_auto.lower() != "keep":
            warn_if_failed(
                f"set ExposureAuto {self.exposure_auto}",
                self.cam.MV_CC_SetEnumValueByString("ExposureAuto", self.exposure_auto),
            )
        if self.gain_auto.lower() == "off":
            warn_if_failed("set GainAuto Off", self.cam.MV_CC_SetEnumValueByString("GainAuto", "Off"))
            warn_if_failed("set Gain", self.cam.MV_CC_SetFloatValue("Gain", float(self.gain)))
        elif self.gain > 0:
            warn_if_failed("set GainAuto Off", self.cam.MV_CC_SetEnumValueByString("GainAuto", "Off"))
            warn_if_failed("set Gain", self.cam.MV_CC_SetFloatValue("Gain", float(self.gain)))
        elif self.gain_auto.lower() != "keep":
            warn_if_failed(f"set GainAuto {self.gain_auto}", self.cam.MV_CC_SetEnumValueByString("GainAuto", self.gain_auto))
        if self.balance_white_auto.lower() != "keep":
            warn_if_failed(
                f"set BalanceWhiteAuto {self.balance_white_auto}",
                self.cam.MV_CC_SetEnumValueByString("BalanceWhiteAuto", self.balance_white_auto),
            )
        if self.gamma > 0:
            self._set_bool("GammaEnable", True, quiet=True)
            self._set_float("Gamma", float(self.gamma), quiet=True)
        if self.saturation >= 0:
            self._set_bool("SaturationEnable", True, quiet=True)
            self._set_int("Saturation", int(self.saturation), quiet=True)
        if self.sharpness >= 0:
            self._set_bool("SharpnessEnable", True, quiet=True)
            self._set_int("Sharpness", int(self.sharpness), quiet=True)

        self._print_float("ExposureTime")
        self._print_float("Gain")

        ret = self.cam.MV_CC_StartGrabbing()
        if ret != 0:
            raise RuntimeError(f"MVS start grabbing failed: 0x{ret:x}")
        self.started = True
        return self

    def _print_float(self, key):
        try:
            value = MVCC_FLOATVALUE()
            memset(byref(value), 0, sizeof(value))
            ret = self.cam.MV_CC_GetFloatValue(key, value)
            if ret == 0:
                print(f"MVS {key}: {value.fCurValue:.3f}", flush=True)
        except Exception:
            pass

    def _set_float(self, key, value, quiet=False):
        ret = self.cam.MV_CC_SetFloatValue(key, float(value))
        if ret != 0 and not quiet:
            warn_if_failed(f"set {key}", ret)
        return ret == 0

    def _set_int(self, key, value, quiet=False):
        ret = self.cam.MV_CC_SetIntValueEx(key, int(value))
        if ret != 0 and not quiet:
            warn_if_failed(f"set {key}", ret)
        return ret == 0

    def _set_bool(self, key, value, quiet=False):
        ret = self.cam.MV_CC_SetBoolValue(key, bool(value))
        if ret != 0 and not quiet:
            warn_if_failed(f"set {key}", ret)
        return ret == 0

    def set_tuning(self, name, value):
        if name == "exposure_us":
            ok1 = self.cam.MV_CC_SetEnumValueByString("ExposureAuto", "Off") == 0
            ok2 = self._set_float("ExposureTime", float(value))
            return ok1 and ok2
        if name == "gain":
            ok1 = self.cam.MV_CC_SetEnumValueByString("GainAuto", "Off") == 0
            ok2 = self._set_float("Gain", float(value))
            return ok1 and ok2
        if name == "gamma":
            self._set_bool("GammaEnable", True, quiet=True)
            return self._set_float("Gamma", float(value))
        if name == "saturation":
            self._set_bool("SaturationEnable", True, quiet=True)
            return self._set_int("Saturation", int(value))
        if name == "sharpness":
            self._set_bool("SharpnessEnable", True, quiet=True)
            return self._set_int("Sharpness", int(value))
        return False

    def _select_device(self, devices):
        if self.serial:
            for item in devices:
                if item.get("serial") == self.serial:
                    return item["index"]
            raise RuntimeError(f"MVS camera serial not found: {self.serial}")
        if self.device_index is not None:
            if self.device_index < 0 or self.device_index >= len(devices):
                raise RuntimeError(f"MVS device index out of range: {self.device_index}")
            return self.device_index
        for item in devices:
            if item.get("type") == "USB":
                return item["index"]
        return devices[0]["index"]

    def read(self):
        out = MV_FRAME_OUT()
        memset(byref(out), 0, sizeof(out))
        ret = self.cam.MV_CC_GetImageBuffer(out, self.timeout_ms)
        if ret != 0 or not out.pBufAddr:
            return False, None
        try:
            return True, self._convert_frame(out)
        finally:
            self.cam.MV_CC_FreeImageBuffer(out)

    def _convert_frame(self, out):
        src_ptr = out.pBufAddr
        src_len = out.stFrameInfo.nFrameLen
        src_type = out.stFrameInfo.enPixelType
        width = out.stFrameInfo.nWidth
        height = out.stFrameInfo.nHeight
        decode_buffer = None

        if src_type in HB_PIXEL_FORMATS:
            decode = MV_CC_HB_DECODE_PARAM()
            memset(byref(decode), 0, sizeof(decode))
            decode_len = width * height * 3
            decode_buffer = (c_ubyte * decode_len)()
            decode.pSrcBuf = src_ptr
            decode.nSrcLen = src_len
            decode.pDstBuf = decode_buffer
            decode.nDstBufSize = decode_len
            ret = self.cam.MV_CC_HBDecode(decode)
            if ret != 0:
                raise RuntimeError(f"MVS HB decode failed: 0x{ret:x}")
            src_ptr = decode.pDstBuf
            src_len = decode.nDstBufLen
            src_type = decode.enDstPixelType

        mono = src_type in MONO_PIXEL_FORMATS
        channels = 1 if mono else 3
        dst_type = PixelType_Gvsp_Mono8 if mono else PixelType_Gvsp_RGB8_Packed
        dst_len = width * height * channels
        dst = (c_ubyte * dst_len)()

        convert = MV_CC_PIXEL_CONVERT_PARAM_EX()
        memset(byref(convert), 0, sizeof(convert))
        convert.nWidth = width
        convert.nHeight = height
        convert.pSrcData = src_ptr
        convert.nSrcDataLen = src_len
        convert.enSrcPixelType = src_type
        convert.enDstPixelType = dst_type
        convert.pDstBuffer = dst
        convert.nDstBufferSize = dst_len
        ret = self.cam.MV_CC_ConvertPixelTypeEx(convert)
        if ret != 0:
            raise RuntimeError(f"MVS convert pixel failed: 0x{ret:x}")

        if mono:
            return np.frombuffer(dst, dtype=np.uint8, count=dst_len).reshape(height, width)
        rgb = np.frombuffer(dst, dtype=np.uint8, count=dst_len).reshape(height, width, 3)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    def release(self):
        if self.cam is not None:
            if self.started:
                self.cam.MV_CC_StopGrabbing()
            self.cam.MV_CC_CloseDevice()
            self.cam.MV_CC_DestroyHandle()
        MvCamera.MV_CC_Finalize()


class MvsCalibrationApp(UsbCalibrationApp):
    def open_camera(self):
        self.status_message = "Opening MVS camera..."
        source = MvsCameraSource(
            device_index=self.args.device_index,
            serial=self.args.serial,
            exposure_us=self.args.exposure_us,
            exposure_auto=self.args.exposure_auto,
            gain=self.args.gain,
            gain_auto=self.args.gain_auto,
            gamma=self.args.gamma,
            saturation=self.args.saturation,
            sharpness=self.args.sharpness,
            balance_white_auto=self.args.balance_white_auto,
            timeout_ms=self.args.frame_timeout_ms,
        )
        return source.open()


def probe(args):
    source = MvsCameraSource(
        device_index=args.device_index,
        serial=args.serial,
        exposure_us=args.exposure_us,
        exposure_auto=args.exposure_auto,
        gain=args.gain,
        gain_auto=args.gain_auto,
        gamma=args.gamma,
        saturation=args.saturation,
        sharpness=args.sharpness,
        balance_white_auto=args.balance_white_auto,
        timeout_ms=args.frame_timeout_ms,
    ).open()
    try:
        ok, frame = source.read()
        if not ok or frame is None:
            raise RuntimeError("MVS probe could not get a frame")
        output = pathlib.Path(args.probe_output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        print(
            f"MVS probe OK: frame={frame.shape[1]}x{frame.shape[0]} mean={float(frame.mean()):.1f} saved={output}",
            flush=True,
        )
    finally:
        source.release()


def main():
    default_output = pathlib.Path.home() / "fast_livo2_data" / "calib" / "camera_intrinsics" / "jr_mvs" / time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-index", type=int, default=None)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--exposure-us", type=float, default=0.0)
    parser.add_argument("--exposure-auto", default="Continuous", choices=["Continuous", "Once", "Off", "Keep"])
    parser.add_argument("--gain", type=float, default=0.0)
    parser.add_argument("--gain-auto", default="Continuous", choices=["Continuous", "Once", "Off", "Keep"])
    parser.add_argument("--gamma", type=float, default=1.0)
    parser.add_argument("--saturation", type=int, default=128)
    parser.add_argument("--sharpness", type=int, default=0)
    parser.add_argument("--balance-white-auto", default="Continuous", choices=["Continuous", "Once", "Off", "Keep"])
    parser.add_argument("--frame-timeout-ms", type=int, default=1000)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--bright", action="store_true", help="Use fixed bright exposure/gain for dark scenes.")
    parser.add_argument("--low-noise", action="store_true", help="Use fixed low-gain settings for calibration with strong lighting.")
    parser.add_argument("--probe-output", default="/tmp/jr_mvs_probe.jpg")
    parser.add_argument("--output-dir", default=str(default_output))
    parser.add_argument("--inner-corners", default="11x8")
    parser.add_argument("--square-size", type=float, default=0.025)
    parser.add_argument("--target-count", type=int, default=40)
    parser.add_argument("--min-count", type=int, default=25)
    parser.add_argument("--preview-hz", type=float, default=5)
    parser.add_argument("--preview-width", type=int, default=960)
    parser.add_argument("--preview-height", type=int, default=700)
    parser.add_argument("--detect-hz", type=float, default=2)
    parser.add_argument("--detect-width", type=int, default=900)
    parser.add_argument("--min-sharpness", type=float, default=75.0)
    parser.add_argument("--min-sharpness-floor", type=float, default=45.0)
    parser.add_argument("--window-name", default="JR MVS Camera Calibration")
    parser.add_argument("--focus-view", action="store_true")
    parser.add_argument("--duplicate-center", type=float, default=0.06)
    parser.add_argument("--duplicate-scale", type=float, default=0.025)
    parser.add_argument("--duplicate-tilt", type=float, default=0.08)
    args = parser.parse_args()
    if args.bright:
        args.exposure_us = max(args.exposure_us, 20000.0)
        args.gain = max(args.gain, 12.0)
        args.exposure_auto = "Off"
        args.gain_auto = "Off"
    if args.low_noise:
        args.exposure_us = max(args.exposure_us, 40000.0)
        args.gain = 0.0
        args.exposure_auto = "Off"
        args.gain_auto = "Off"
    if args.probe:
        probe(args)
    else:
        MvsCalibrationApp(args).run()


if __name__ == "__main__":
    main()
