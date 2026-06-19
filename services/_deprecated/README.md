# Deprecated systemd units — DO NOT REINSTALL

These two units (the picam-watchdog service + timer) were disabled on
2026-05-31 after they caused an 11-hour camera freeze. Kept here for
historical reference only.

## What happened

`picam-watchdog.timer` fired `picam-watchdog.service` every 5 minutes,
which ran `/home/pi/picam/scripts/watchdog.sh` (the OLD watchdog).
That script's `check_stream()` curls mediamtx API on
`http://127.0.0.1:9997/...`, but the mediamtx instance running inside
`picam-stream.service` has `api: no` in its config, so the probe always
failed. The watchdog reacted by running `systemctl restart picam-stream`
every 5 min, which spun up `mediamtx + rpicam-vid` simultaneously with
`picamera2` inside `motion_detector.py`. They fought for camera access
and the capture loop hung — snapshots stopped, live stream went dark
for ~11h before anyone noticed.

## The replacement

A cron-driven freeze-watchdog at `/etc/cron.d/picam-freeze-watchdog`
runs `/home/pi/picam/watchdog.sh` (the NEW watchdog) every 3 min.
It checks `/home/pi/picam-logs/motion.log` mtime > 150s and runs
`systemctl restart picam-motion` if stale. No port probing, no
camera contention. The cron file and watchdog.sh are kept in
`services/` (one level up) as canonical assets.

## What about picam-stream.service?

Still active in the architecture (one level up in `services/`), but
serves a different role now: `picam-motion.service` owns the camera
when it's running, and `picam-stream.service` is the swap-target that
takes over (via `picam-motion.service`'s `ExecStopPost=`) so the live
stream still works during a motion-detector restart. The two are no
longer running simultaneously — that's what broke things.
