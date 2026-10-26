#!/usr/bin/env python3
"""Watchdog: kills SOFTWARE transcodes of on-demand catalog items.

Why: when PlaybackInfo's probe fails, Jellyfin plays the .strm "blind" — without knowing
the codec — and transcodes everything on the CPU (hevc -> libx264). On a 4K title on a
small CPU that was 0.3 frames/s, an ffmpeg with 2 GB of RAM, swapping, and Jellyfin
re-creates the process on every retry of the player. The gateway already refuses that
case; this watchdog is the server's safety net. Optional — useful on weak hardware.

Rule: an ffmpeg whose input is the gateway (<host:port>/v/) AND that encodes video in
software (libx264/libx265) -> kill -9. Hardware transcodes (vaapi, qsv, nvenc...) are
never touched.

Runs on the HOST (it must see Jellyfin's processes), from cron, every minute (a ~55 s
loop that checks every 5 s):

    * * * * * /usr/bin/python3 /opt/jellyfin-ondemand/tools/transcode_watchdog.py \
                  --gateway ondemand:8097 --data /opt/jellyfin-ondemand/data

Metrics: <data>/transcode-watchdog.prom, which the service's /metrics passes along — the
total of kills and the time of the last run (a stopped cron is an alert too).
"""

import argparse
import fcntl
import os
import signal
import sys
import time

SOFTWARE = ("libx264", "libx265")
MAX_LINES = 1000


class Watchdog:
    def __init__(self, gateway, data):
        self.gateway = gateway.rstrip("/").split("://")[-1] + "/v/"
        self.log = os.path.join(data, "transcode-watchdog.log")
        self.prom = os.path.join(data, "transcode-watchdog.prom")

    def read_kills(self):
        try:
            with open(self.prom) as f:
                for line in f:
                    if line.startswith("ondemand_transcode_watchdog_kills_total "):
                        return int(float(line.split()[1]))
        except (OSError, ValueError):
            pass
        return 0

    def write_prom(self, kills):
        tmp = self.prom + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"ondemand_transcode_watchdog_kills_total {kills}\n"
                    f"ondemand_transcode_watchdog_run_unix {int(time.time())}\n")
        os.replace(tmp, self.prom)

    def note(self, msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        try:
            with open(self.log, "a") as f:
                f.write(line)
            with open(self.log) as f:
                lines = f.readlines()
            if len(lines) > MAX_LINES:
                with open(self.log, "w") as f:
                    f.writelines(lines[-MAX_LINES:])
        except OSError:
            sys.stderr.write(line)

    @staticmethod
    def ffmpegs():
        """PIDs of processes named ffmpeg, from /proc (no fork of pgrep every 5 s)."""
        pids = []
        for p in os.listdir("/proc"):
            if p.isdigit():
                try:
                    with open(f"/proc/{p}/comm") as f:
                        if f.read().strip() == "ffmpeg":
                            pids.append(int(p))
                except OSError:
                    continue
        return pids

    def check(self):
        """How many ffmpeg processes this pass killed."""
        killed = 0
        for pid in self.ffmpegs():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if self.gateway not in cmd or not any(s in cmd for s in SOFTWARE):
                continue
            release = cmd.split(self.gateway, 1)[1].split()[0].strip('"')
            try:
                os.kill(pid, signal.SIGKILL)
                self.note(f"KILLED ffmpeg {pid}: software transcode of a catalog item (release {release})")
                killed += 1
            except ProcessLookupError:
                pass
            except PermissionError as e:
                self.note(f"no permission to kill ffmpeg {pid} ({release}): {e}")
        return killed

    def run(self, seconds=55, every=5):
        kills = self.read_kills()
        self.write_prom(kills)          # heartbeat: one per minute
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            n = self.check()
            if n:
                kills += n
                self.write_prom(kills)
            time.sleep(every)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--gateway", required=True, help="host:port of the gateway as written in the .strm files")
    p.add_argument("--data", required=True, help="the service's data folder (log and .prom go there)")
    args = p.parse_args()
    lock = open(os.path.join("/tmp", "ondemand-transcode-watchdog.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return                      # another run is still in its loop
    Watchdog(args.gateway, args.data).run()


if __name__ == "__main__":
    main()
