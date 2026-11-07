import json
import subprocess
import sys

from compute.engine.node.engine import reap_orphans


def test_reaper_only_kills_llama_processes(tmp_path):
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])   # works on Windows too
    try:
        pid_file = tmp_path / 'node.pids'
        pid_file.write_text(json.dumps([other.pid, 999999]))
        assert reap_orphans(pid_file) == []      # not llama-server / rpc-server: left alone
        assert other.poll() is None
        assert not pid_file.exists()
    finally:
        other.kill()
