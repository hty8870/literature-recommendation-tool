# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Prepare, start, and update the loopback literature demo without taking over other services."""

from __future__ import annotations

import argparse
import asyncio
import errno
import getpass
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_COUNT = 11
STOP_TIMEOUT = 25
STATE_DIR = "data/runtime/linux-updater"
HEALTH_LIMIT = 16_384
UPDATER_MODEL_ENV = "CITATION_UPDATER_MODEL_CONFIG"
ACCOUNT_KEY_ENV = "CITATION_ACCOUNT_MODEL_KEY_FILE"
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
STABLE_PATHS = {
    "CITATION_DB_PATH": "data/runtime/app.db",
    "CITATION_AUTH_DB_PATH": "data/runtime/auth.db",
    "CITATION_MODEL_CONFIG": "data/runtime/deepseek.local.json",
    "CITATION_EMBEDDING_CACHE_DIR": "data/runtime/embeddings",
}


class UpdateError(RuntimeError):
    pass


class LockBusy(UpdateError):
    pass


@contextmanager
def lock(path: Path) -> Iterator[None]:
    # Closing the descriptor releases the kernel lock even after an unclean process exit.
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                raise LockBusy(path.name) from exc
            raise
        yield


def locked(path: Path) -> bool:
    try:
        with lock(path):
            return False
    except LockBusy:
        return True


@dataclass(frozen=True, slots=True)
class Instance:
    revision: str
    port: int
    generation: str
    profile: str

    @classmethod
    def read(cls, path: Path) -> Instance | None:
        try:
            item = cls(**json.loads(path.read_bytes()))
        except FileNotFoundError:
            return None
        except (TypeError, ValueError) as exc:
            raise UpdateError(f"状态文件无法识别：{path}；没有自动停止任何进程。") from exc
        if (
            not isinstance(item.revision, str)
            or not REVISION.fullmatch(item.revision)
            or type(item.port) is not int
            or not 1024 <= item.port <= 65535
            or not isinstance(item.generation, str)
            or not re.fullmatch(r"[0-9a-f]{32}", item.generation)
            or not isinstance(item.profile, str)
            or not re.fullmatch(r"[0-9a-f]{64}", item.profile)
        ):
            raise UpdateError(f"状态文件内容不合法：{path}；没有自动停止任何进程。")
        return item

    def save(self, path: Path) -> None:
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(asdict(self)), encoding="utf-8")
        pending.replace(path)


def listening_socket(ports: Iterable[int]) -> socket.socket:
    ports = tuple(ports)
    for port in ports:
        try:
            return socket.create_server((HOST, port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", 0) not in (10013, 10048):
                raise
    scope = str(ports[0]) if len(ports) == 1 else f"{ports[0]}–{ports[-1]}"
    raise UpdateError(f"回环端口 {scope} 已被占用。可指定 --port；没有停止占用进程。")


def healthy(port: int) -> bool:
    connection = http.client.HTTPConnection(HOST, port, timeout=0.6)
    try:
        connection.request("GET", "/healthz", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        raw = response.read(HEALTH_LIMIT + 1)
        if len(raw) > HEALTH_LIMIT:
            return False
        data = json.loads(raw)
        return (
            isinstance(data, dict)
            and data.get("status") == "ok"
            and data.get("storage") == "sqlite_local"
            and data.get("worker_mode") == "in_process_asyncio"
        )
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


def is_server_command(arguments: list[str]) -> bool:
    return not {"--check", "--check-live"}.intersection(arguments) and (
        "citation_tool.api:create_app" in arguments
        or any(Path(argument).name == "citation-tool" for argument in arguments[:6])
    )


def manual_instance(repo: Path) -> bool:
    if sys.platform != "linux":
        return False
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if (process / "cwd").readlink() != repo:
                continue
            argv = (process / "cmdline").read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if is_server_command(argv.rstrip("\0").split("\0")):
            return True
    return False


def service_environment(repo: Path, releases: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "UV_PROJECT_ENVIRONMENT",
        UPDATER_MODEL_ENV,
    ):
        env.pop(key, None)
    stable = STABLE_PATHS | (
        {ACCOUNT_KEY_ENV: env[ACCOUNT_KEY_ENV]} if env.get(ACCOUNT_KEY_ENV) else {}
    )
    for key, default in stable.items():
        path = (repo / env.get(key, default)).resolve()
        if path.is_relative_to(releases):
            raise UpdateError(f"{key} 必须指向独立数据目录，不能放在版本目录内。")
        env[key] = str(path)
    for key in ("CITATION_CORPUS_PATH", "CITATION_SAMPLE_CORPUS_PATH"):
        if key in env:
            env[key] = str((repo / env[key]).resolve())
    return env


def profile_id(env: dict[str, str]) -> str:
    fields = {
        name: (
            bool(value)
            if name != ACCOUNT_KEY_ENV
            and any(key in name for key in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
            else value
        )
        for name, value in env.items()
        if name.startswith("CITATION_") and not name.startswith("CITATION_DEPLOY_")
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def interpreter(release: Path) -> Path:
    bin_dir, name = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return release / ".venv" / bin_dir / name


def stop_marker(state: Path, generation: str) -> Path:
    return state / f"stop-{generation}"


async def serve_until_stopped(state: Path, instance: Instance, listener: socket.socket) -> None:
    import uvicorn

    config = uvicorn.Config(
        "citation_tool.api:create_app",
        factory=True,
        host=HOST,
        port=instance.port,
        workers=1,
        timeout_graceful_shutdown=10,
        access_log=False,
    )
    server = uvicorn.Server(config)
    request = stop_marker(state, instance.generation)
    await_job = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not await_job.done():
            if request.exists():
                server.should_exit = True
                break
            await asyncio.sleep(0.25)
        await await_job
    finally:
        request.unlink(missing_ok=True)


def serve_child(state: Path, instance: Instance, fallback: bool) -> None:
    ports = (
        range(instance.port, min(instance.port + PORT_COUNT, 65536))
        if fallback
        else (instance.port,)
    )
    with lock(state / "service.lock"), listening_socket(ports) as listener:
        bound = Instance(
            instance.revision, listener.getsockname()[1], instance.generation, instance.profile
        )
        bound.save(state / "running.json")
        asyncio.run(serve_until_stopped(state, bound, listener))


def git_text(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        raise UpdateError("找不到 git。")
    result = subprocess.run(
        [git, "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=20,
    )
    if result.returncode:
        raise UpdateError("无法读取 Git 状态。请在已克隆的仓库内使用追踪上游的本地分支。")
    return result.stdout.strip()


class Deployment:
    def __init__(
        self, repo: Path, *, wait: int, check_live: bool = False, set_deepseek: bool = False
    ) -> None:
        self.repo = repo
        self.check_live = check_live
        self.set_deepseek = set_deepseek
        self.state = repo / STATE_DIR
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state.is_symlink() or (os.name == "posix" and self.state.stat().st_mode & 0o077):
            raise UpdateError(f"请将脚本状态目录设为仅本账号可访问：{self.state}")
        self.releases = self.state / "releases"
        self.releases.mkdir(exist_ok=True)
        if self.releases.is_symlink():
            raise UpdateError("版本目录不能是符号链接。")
        self.log = self.state / "update.log"
        self.wait = wait
        self.env = service_environment(repo, self.releases)
        self.profile = profile_id(self.env)
        self.default_model = self.state / "default-model.local.json"
        self.pending_model = self.state / "pending-model.local.json"
        if self.default_model.is_file():
            self.env[UPDATER_MODEL_ENV] = str(self.default_model)

    def running(self) -> Instance | None:
        if not locked(self.state / "service.lock"):
            return None
        current = Instance.read(self.state / "running.json")
        if current is None and locked(self.state / "service.lock"):
            raise UpdateError("实例正在启动或缺少运行回执；请稍后运行 status 检查。")
        return current

    def command(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        timeout: int,
        env: dict[str, str] | None = None,
        input_data: bytes | None = None,
    ) -> None:
        with self.log.open("ab") as log:
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {name}\n".encode())
            log.flush()
            try:
                result = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL if input_data is None else None,
                    input=input_data,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise UpdateError(f"{name}无法完成（{type(exc).__name__}），见 {self.log}") from exc
        if result.returncode:
            raise UpdateError(f"{name}失败（退出码 {result.returncode}），见 {self.log}")

    def target(self, baseline: Instance | None) -> str:
        if git_text(self.repo, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("仓库有未提交的跟踪文件修改。脚本不会覆盖，请先保留或提交修改。")
        branch = git_text(self.repo, "symbolic-ref", "--short", "HEAD")
        remote = git_text(self.repo, "config", "--get", f"branch.{branch}.remote")
        self.command(
            "获取上游版本",
            ["git", "fetch", "--no-tags", "--no-recurse-submodules", "--", remote],
            cwd=self.repo,
            timeout=180,
        )
        target = git_text(self.repo, "rev-parse", "--verify", "@{upstream}^{commit}")
        if not REVISION.fullmatch(target):
            raise UpdateError("无法确认上游提交。")
        bases = ("HEAD", baseline.revision) if baseline else ("HEAD",)
        for current in bases:
            self.command(
                "检查是否可快进",
                ["git", "merge-base", "--is-ancestor", current, target],
                cwd=self.repo,
                timeout=20,
            )
        return target

    def prepare(self, revision: str) -> None:
        release = self.releases / revision
        if release.is_symlink():
            raise UpdateError("版本路径不能是符号链接。")
        if not (release / ".prepared").is_file():
            if release.exists():
                raise UpdateError(f"发现未完成的版本目录 {release}；请确认停机后移走再试。")
            env = {key: value for key, value in self.env.items() if not key.startswith("CITATION_")}
            env["UV_PROJECT_ENVIRONMENT"] = str(release / ".venv")
            try:
                with TemporaryDirectory(prefix="stage-", dir=self.state) as temp:
                    archive = Path(temp) / "code.tar"
                    self.command(
                        "导出已提交的源码",
                        ["git", "archive", "-o", str(archive), revision],
                        cwd=self.repo,
                        timeout=90,
                    )
                    release.mkdir()
                    with tarfile.open(archive, mode="r:") as stream:
                        stream.extractall(release, filter="data")
                self.command(
                    "安装锁定依赖",
                    ["uv", "sync", "--frozen", "--no-dev", "--python", "3.12"],
                    cwd=release,
                    timeout=900,
                    env=env,
                )
                self.check(release)
                (release / ".prepared").touch()
            except (OSError, UpdateError, KeyboardInterrupt, tarfile.TarError):
                self.remove(release)
                raise
        else:
            self.check(release)

    def staged_environment(self) -> dict[str, str]:
        if self.set_deepseek:
            return {**self.env, UPDATER_MODEL_ENV: str(self.pending_model)}
        return self.env

    def stage_deepseek(self, release: Path) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("DeepSeek API Key（输入不显示）：")
        except (EOFError, getpass.GetPassWarning):
            raise UpdateError("需要交互式终端安全输入 Key。") from None
        self.command(
            "保存候选默认模型",
            [
                str(interpreter(release)),
                "-I",
                "-c",
                "import sys; from pathlib import Path; from citation_tool.reasons import "
                "save_demo_model_config; "
                "save_demo_model_config(Path(sys.argv[1]), sys.stdin.read(513))",
                str(self.pending_model),
            ],
            input_data=key.encode("utf-8"),
            cwd=release,
            env=self.env,
            timeout=30,
        )

    def check(self, release: Path) -> None:
        if self.set_deepseek:
            self.stage_deepseek(release)
        flag = "--check-live" if self.check_live else "--check"
        command = f"import sys; from citation_tool.cli import main; sys.exit(main([{flag!r}]))"
        self.command(
            "真实演示预检" if self.check_live else "新版启动自检",
            [str(interpreter(release)), "-I", "-c", command],
            cwd=release,
            env=self.staged_environment(),
            timeout=1050 if self.check_live else max(90, self.wait),
        )

    @staticmethod
    def remove(directory: Path) -> None:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"未能清理闲置版本 {directory}：{type(exc).__name__}", file=sys.stderr)

    def stop(self, current: Instance) -> None:
        marker = stop_marker(self.state, current.generation)
        marker.touch()
        deadline = time.monotonic() + STOP_TIMEOUT
        while locked(self.state / "service.lock"):
            if time.monotonic() >= deadline:
                raise UpdateError("旧实例尚未完全退出。没有强杀，也没有启动第二个实例。")
            time.sleep(0.2)
        marker.unlink(missing_ok=True)

    def start(
        self, revision: str, port: int, *, fallback: bool = False, env: dict[str, str] | None = None
    ) -> Instance:
        if locked(self.state / "service.lock"):
            raise UpdateError("旧进程仍持有实例锁。没有启动第二个实例。")
        (self.state / "running.json").unlink(missing_ok=True)
        release = self.releases / revision
        requested = Instance(revision, port, uuid4().hex, self.profile)
        argv = [
            str(interpreter(release)),
            "-I",
            "-u",
            str(Path(__file__).resolve()),
            "_serve",
            str(self.state),
            json.dumps(asdict(requested)),
            "1" if fallback else "0",
        ]
        detached = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        service_log = self.state / "service.log"
        with service_log.open("ab") as sink:
            sink.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {revision[:12]}\n".encode())
            sink.flush()
            child = subprocess.Popen(  # noqa: S603
                argv,
                cwd=release,
                env=env if env is not None else self.env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                **detached,
            )
        deadline = time.monotonic() + self.wait
        marker = stop_marker(self.state, requested.generation)
        connected_port = None
        try:
            while child.poll() is None and time.monotonic() < deadline:
                current = Instance.read(self.state / "running.json")
                if current and current.generation == requested.generation:
                    connected_port = current.port
                    if healthy(current.port) and child.poll() is None:
                        return current
                time.sleep(0.2)
            if connected_port is None and child.poll() is not None:
                with listening_socket(range(port, port + PORT_COUNT) if fallback else (port,)):
                    pass
            raise UpdateError(f"新实例未通过 HTTP 健康检查，见 {service_log}")
        except (OSError, UpdateError, KeyboardInterrupt):
            if child.poll() is None:
                marker.touch()
                try:
                    child.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    raise UpdateError("候选实例尚未退出；未启动第二个实例。") from None
            marker.unlink(missing_ok=True)
            raise

    def report(self, item: Instance, message: str) -> None:
        print(f"{message} {item.revision[:12]}：http://{HOST}:{item.port}/", flush=True)

    def update(self, port: int | None) -> None:
        running = self.running()
        receipt = self.state / "last-good.json"
        previous_good = Instance.read(receipt)
        baseline = running or previous_good
        if running and running.profile != self.profile:
            raise UpdateError("当前部署环境与运行实例不同；请在原环境执行，未停止旧服务。")
        selected = port if port is not None else baseline.port if baseline else DEFAULT_PORT
        manual_ports = {DEFAULT_PORT, selected} - ({running.port} if running else set())
        if manual_instance(self.repo) or any(healthy(candidate) for candidate in manual_ports):
            raise UpdateError("发现未由脚本管理的文献服务；请用原方式停一次，再运行本脚本。")
        fallback = port is None and baseline is None
        if not running or selected != running.port:
            ports = range(selected, selected + PORT_COUNT) if fallback else (selected,)
            with listening_socket(ports):
                pass
        revision = self.target(baseline)
        if (
            not self.check_live
            and not self.set_deepseek
            and running
            and (running.revision, running.port) == (revision, selected)
            and healthy(selected)
        ):
            running.save(receipt)
            self.report(running, "已是最新版，HTTP 健康，未重启")
            return
        print("准备并检查候选版本；在检查通过前不停止旧实例。", flush=True)
        if self.check_live:
            print("将实际调用默认模型和 Europe PMC，详细进度见 " + str(self.log), flush=True)
        self.prepare(revision)
        item = None
        receipt_written = False
        try:
            if running:
                print("新版自检通过，正在停止脚本管理的旧实例…", flush=True)
                self.stop(running)
            extra = {"env": self.staged_environment()} if self.set_deepseek else {}
            item = self.start(revision, selected, fallback=fallback, **extra)
            item.save(receipt)
            receipt_written = True
            if self.set_deepseek:
                self.pending_model.replace(self.default_model)
        except (OSError, UpdateError, KeyboardInterrupt) as exc:
            try:
                if item:
                    self.stop(item)
                if receipt_written:
                    if previous_good is None:
                        receipt.unlink(missing_ok=True)
                    else:
                        previous_good.save(receipt)
                if running and not locked(self.state / "service.lock"):
                    restored = self.start(running.revision, running.port)
                    restored.save(receipt)
                    self.report(restored, "新版未上线，已恢复旧版")
            except (OSError, UpdateError, KeyboardInterrupt) as rollback:
                raise UpdateError(f"更新失败：{exc}；恢复旧版也失败：{rollback}") from rollback
            raise
        if self.set_deepseek:
            print("服务端默认模型：deepseek-flash（Key 已加密保存）。", flush=True)
        self.report(item, "真实演示预检通过，已启动" if self.check_live else "更新成功，HTTP 健康")
        if not self.check_live:
            print("未调用模型或在线库；正式演示前请使用 --check-live。", flush=True)
        keep = {revision, baseline.revision} if baseline else {revision}
        for directory in self.releases.iterdir():
            if (
                REVISION.fullmatch(directory.name)
                and directory.name not in keep
                and not directory.is_symlink()
                and (directory / ".prepared").is_file()
            ):
                self.remove(directory)


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "_serve":
        serve_child(Path(sys.argv[2]), Instance(**json.loads(sys.argv[3])), sys.argv[4] == "1")
        return 0
    parser = argparse.ArgumentParser(description="更新或管理本仓库的文献推荐演示实例。")
    parser.add_argument("action", choices=("update", "status", "stop"), nargs="?", default="update")
    parser.add_argument("--port", type=int, help="指定固定端口；占用时不自动更换")
    parser.add_argument("--wait", type=int, default=90, help="HTTP 启动验收时限（秒）")
    parser.add_argument(
        "--check-live", action="store_true", help="切换前实际验证模型、在线库和排名"
    )
    parser.add_argument(
        "--set-deepseek", action="store_true", help="隐藏输入 Key，设置服务端默认 DeepSeek"
    )
    args = parser.parse_args()
    if (args.check_live or args.set_deepseek) and args.action != "update":
        parser.error("--check-live 和 --set-deepseek 仅用于启动或更新。")
    if (args.port is not None and not 1024 <= args.port <= 65535) or not 5 <= args.wait <= 600:
        parser.error("端口须在 1024–65535，--wait 须在 5–600 秒内。")
    if not shutil.which("git") or not shutil.which("uv"):
        parser.error("需要安装 git 和 uv，并将它们加入 PATH。")
    try:
        repo = Path(git_text(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()
        if not (repo / "src/citation_tool/api.py").is_file():
            raise UpdateError("请在文献推荐工具的 Git 仓库目录运行本脚本。")
        os.umask(0o077)
        deploy = Deployment(
            repo, wait=args.wait, check_live=args.check_live, set_deepseek=args.set_deepseek
        )
        with lock(deploy.state / "update.lock"):
            if args.action == "update":
                try:
                    deploy.update(args.port)
                finally:
                    if args.set_deepseek:
                        deploy.pending_model.unlink(missing_ok=True)
            elif current := deploy.running():
                if args.action == "stop":
                    deploy.stop(current)
                    print("脚本管理的实例已停止。")
                elif healthy(current.port):
                    deploy.report(current, "运行中")
                else:
                    deploy.report(current, "受管理进程存在，但 HTTP 检查未通过")
                    return 1
            else:
                print("没有由脚本管理的运行实例。")
    except LockBusy:
        print("另一个操作正在进行，请等待完成。", file=sys.stderr)
        return 1
    except (UpdateError, OSError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作被中断。可运行 status 查看当前实例。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
