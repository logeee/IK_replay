# RGB-D calibration

Production camera input comes from the external teleimager ZMQ stream. The
stream service is read-only from this project.

Generate the local calibration file once, while the camera is available for
exclusive SDK access:

```bash
python tools/export_orbbec_rgbd_calibration.py \
  --serial CP0BB53000FS \
  --color-width 1920 --color-height 1080 \
  --depth-width 1280 --depth-height 800 \
  --output config/camera/orbbec_rgbd_calibration.json
```

Temporarily stop the process that owns the camera before exporting, then
restart it afterwards. The generated JSON is machine/device-specific and is
not committed. `reach_server.py` validates every incoming stream shape against
this file before using depth for 3D calculations.

This project never starts the teleimager service and never edits that
repository. After exporting, only restart the existing camera service if you
stopped it:

```bash
sudo systemctl start teleimager-camera-capture.service
```
