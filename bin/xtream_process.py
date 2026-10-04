"""Stop an Xtream media child before its account lease becomes reusable."""
from __future__ import annotations

import subprocess


def close_media_process(process):
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                # The child may have exited between poll and terminate. If it
                # did not, make one stronger attempt before releasing a lease.
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        for pipe in (process.stdin, process.stdout):
            if pipe and not pipe.closed:
                pipe.close()
