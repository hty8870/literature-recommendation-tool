"""Project-only temporary OpenAI egress with ownership-bound orphan recovery."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time
from contextlib import contextmanager

from tools.deploy_citation import ROOT, remote_python, ssh_command

LEASE = ROOT / "data/runtime/luna-egress-owner.json"
LOCK = ROOT / "data/runtime/luna-egress.lock"
OWNER_PREFIX = b"CITATION_EGRESS_PID="

OWNER_SCRIPT = """
import json,os,pwd,signal,subprocess,sys
from pathlib import Path
request=json.loads(sys.argv[1])
pid=int(request['pid'])
assert pid > 1
proc=Path('/proc')/str(pid)
if not proc.exists():
    print(json.dumps({'gone':True}))
else:
    owner=proc.stat().st_uid
    command=(proc/'cmdline').read_bytes().replace(bytes([0]),b' ').decode(errors='replace')
    ticks=(proc/'stat').read_text().rsplit(') ',1)[1].split()[19]
    assert owner==pwd.getpwnam('ubuntu').pw_uid and command.startswith('sshd: ubuntu')
    listener=subprocess.check_output(['ss','-Hltnp','( sport = :18768 )'],text=True)
    if f'pid={pid},' not in listener or '127.0.0.1:18768' not in listener:
        assert request['action']=='close', 'forward owner differs'
        print(json.dumps({'gone':True}))
    else:
        if request['action']=='close':
            assert ticks==str(request['start_ticks']), 'SSH process creation identity changed'
            descriptor=os.pidfd_open(pid)
            try:
                current=(proc/'stat').read_text().rsplit(') ',1)[1].split()[19]
                assert current==ticks, 'SSH identity changed before termination'
                signal.pidfd_send_signal(descriptor,signal.SIGTERM)
            finally:
                os.close(descriptor)
        print(json.dumps({'pid':pid,'start_ticks':ticks,'purpose':'citation-openai-egress',
            'port':18768,'closed':request['action']=='close'}))
"""


REMOTE_SESSION = """
import os,time
from pathlib import Path
parent=os.getppid()
stat=Path('/proc')/str(parent)/'stat'
def identity():
    return stat.read_text().rsplit(') ',1)[1].split()[19]
birth=identity()
print('CITATION_EGRESS_PID='+str(parent),flush=True)
while True:
    time.sleep(10)
    try:
        if identity()!=birth: break
    except OSError:
        break
"""


def tunnel_command() -> list[str]:
    encoded = base64.b64encode(REMOTE_SESSION.encode()).decode()
    bootstrap = f"import base64;exec(base64.b64decode({encoded!r}))"
    remote_command = "exec python3 -u -c " + shlex.quote(bootstrap)
    ssh = ssh_command()
    return [
        *ssh[:-1],
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        "127.0.0.1:18768:127.0.0.1:7897",
        ssh[-1],
        remote_command,
    ]


def permanent_failure(stderr: str) -> bool:
    lowered = stderr.casefold()
    return any(
        marker in lowered
        for marker in (
            "permission denied",
            "host key verification failed",
            "remote port forwarding failed",
        )
    )


def inspect_owner(pid: int) -> dict:
    result = json.loads(remote_python(OWNER_SCRIPT, json.dumps({"action": "inspect", "pid": pid})))
    if result.get("gone"):
        raise RuntimeError("new project forward disappeared before ownership verification")
    return result


def cleanup_owned() -> None:
    if not LEASE.exists():
        return
    lease = json.loads(LEASE.read_text(encoding="utf-8"))
    if lease.get("purpose") != "citation-openai-egress" or lease.get("port") != 18768:
        raise ValueError("project egress ownership receipt is invalid")
    result = json.loads(remote_python(OWNER_SCRIPT, json.dumps({**lease, "action": "close"})))
    if not result.get("gone") and not result.get("closed"):
        raise RuntimeError("project egress ownership could not be verified")
    LEASE.unlink()


@contextmanager
def single_owner():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run() -> int:
    delay = 30
    process = None
    try:
        while True:
            cleanup_owned()
            started = time.monotonic()
            print("project OpenAI tunnel starting", flush=True)
            process = subprocess.Popen(
                tunnel_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            marker = process.stdout.readline()
            if marker.startswith(OWNER_PREFIX):
                owner = inspect_owner(int(marker[len(OWNER_PREFIX) :].strip()))
                LEASE.write_text(json.dumps(owner) + "\n", encoding="utf-8")
                print("project tunnel forward ownership verified", flush=True)
            _, stderr = process.communicate()
            if permanent_failure(stderr.decode(errors="replace")):
                print(
                    "tunnel stopped: review authentication, host identity or port ownership",
                    flush=True,
                )
                return 2
            cleanup_owned()
            print(f"tunnel ended; reconnecting in {delay}s", flush=True)
            time.sleep(delay)
            delay = 30 if time.monotonic() - started > 300 else min(delay * 2, 300)
    except KeyboardInterrupt:
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


def main() -> int:
    try:
        with single_owner():
            return run()
    except Exception as exc:
        print(f"project tunnel stopped safely: {type(exc).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
