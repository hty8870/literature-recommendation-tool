"""Authorized single-host deployment with encrypted model credentials and no secret argv."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

from citation_tool.config import Settings
from citation_tool.jcs import sha256_json
from citation_tool.reasons import load_model_config, validate_openai_proxy

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "data/runtime/quality-corpus/large-corpus.json"
SAMPLE_CORPUS_PATH = ROOT / "data/runtime/quality-corpus/real-corpus.json"


def ssh_command(*, connect_timeout: int = 10) -> list[str]:
    target = os.environ.get("CITATION_DEPLOY_SSH_TARGET", "").strip()
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*", target):
        raise ValueError("set CITATION_DEPLOY_SSH_TARGET to an SSH alias or user@host")
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
    identity = os.environ.get("CITATION_DEPLOY_SSH_IDENTITY_FILE", "").strip()
    if identity:
        command.extend(("-i", str(Path(identity).expanduser())))
    return [*command, target]


def remote(command: str, payload: bytes | None = None, timeout: int = 600) -> str:
    try:
        completed = subprocess.run(
            [*ssh_command(), command],
            input=payload,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"remote command failed: {detail}") from None
    return completed.stdout.decode("utf-8").strip()


def model_metadata() -> bytes:
    source = (
        ROOT
        / "data/runtime/embedding-models/models--intfloat--multilingual-e5-small"
        / "files_metadata.json"
    )
    metadata = json.loads(source.read_text(encoding="utf-8"))
    portable = {key.replace("\\", "/"): value for key, value in metadata.items()}
    if not portable or any(not key.startswith("snapshots/") for key in portable):
        raise ValueError("model metadata contains non-portable paths")
    return (json.dumps(portable, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def package(vectors: list[Path], revision: str, *, include_model: bool) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.dereference = True
        for name in ("pyproject.toml", "uv.lock", "src", "web", ".agents"):
            path = ROOT / name
            archive.add(
                path,
                arcname=name,
                filter=lambda item: (
                    None if "__pycache__" in item.name or item.name.endswith(".pyc") else item
                ),
            )
        archive.add(CORPUS_PATH, arcname="corpus.json")
        archive.add(SAMPLE_CORPUS_PATH, arcname="sample-corpus.json")
        archive.add(ROOT / "data/runtime/deploy-requirements.txt", arcname="requirements.txt")
        for path in vectors:
            archive.add(path, arcname=f"embedding-cache/{path.name}")
        archive.add(ROOT / "data/runtime/deploy-wheelhouse", arcname="wheelhouse")
        if include_model:
            archive.add(
                ROOT / "data/runtime/embedding-models/models--intfloat--multilingual-e5-small",
                arcname="embedding-models/models/models--intfloat--multilingual-e5-small",
                filter=lambda item: None if item.name.endswith("/files_metadata.json") else item,
            )
            metadata = model_metadata()
            entry = tarfile.TarInfo(
                "embedding-models/models/models--intfloat--multilingual-e5-small/"
                "files_metadata.json"
            )
            entry.size = len(metadata)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(metadata))
    with tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r:gz") as packaged:
        if include_model:
            model = packaged.getmember(
                "embedding-models/models/models--intfloat--multilingual-e5-small/"
                f"snapshots/{revision}/onnx/model.onnx"
            )
            if not model.isreg() or model.size < 400_000_000:
                raise ValueError("offline package does not contain the full ONNX model")
            metadata_member = packaged.getmember(
                "embedding-models/models/models--intfloat--multilingual-e5-small/"
                "files_metadata.json"
            )
            metadata_file = packaged.extractfile(metadata_member)
            assert metadata_file is not None
            metadata = json.load(metadata_file)
            if not metadata or any("\\" in key for key in metadata):
                raise ValueError("model metadata was not normalized to POSIX paths")
        requirements = packaged.getmember("requirements.txt")
        if not requirements.isreg():
            raise ValueError("offline package does not contain locked requirements")
    return buffer.getvalue()


def prepare_offline_assets() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to build offline deployment assets")
    wheelhouse = ROOT / "data/runtime/deploy-wheelhouse"
    source_hash = hashlib.sha256(
        (ROOT / "uv.lock").read_bytes() + subprocess.check_output([uv, "--version"])
    ).hexdigest()
    marker = wheelhouse / ".source-hash"
    if marker.is_file() and marker.read_text(encoding="utf-8") == source_hash:
        return
    requirements = ROOT / "data/runtime/deploy-requirements.txt"
    subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    wheelhouse.mkdir(parents=True, exist_ok=True)
    platform_args = [
        "--dest",
        str(wheelhouse),
        "--only-binary=:all:",
        "--platform",
        "manylinux2014_x86_64",
        "--platform",
        "manylinux_2_17_x86_64",
        "--platform",
        "manylinux_2_28_x86_64",
        "--python-version",
        "3.12",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
    ]
    packaging_python = ROOT / "data/runtime/deploy-tools/Scripts/python.exe"
    if not packaging_python.is_file():
        subprocess.run(
            [uv, "venv", "--python", "3.12", str(packaging_python.parent.parent)], check=True
        )
        subprocess.run([uv, "pip", "install", "--python", str(packaging_python), "pip"], check=True)
    pip_download = [str(packaging_python), "-m", "pip", "download"]
    subprocess.run(
        [*pip_download, "--requirement", str(requirements), *platform_args],
        cwd=ROOT,
        check=True,
    )
    marker.write_text(source_hash, encoding="utf-8")


def rpc(method: str, params=None) -> dict:
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return message


def remote_python(script: str, *arguments: str) -> str:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    code = f'import base64,sys;exec(base64.b64decode("{encoded}"))'
    args = " ".join(shlex.quote(argument) for argument in arguments)
    return remote(f"sudo python3 -c {shlex.quote(code)} {args}")


RELEASE_DEDUP_SCRIPT = """
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

new_root = Path(sys.argv[1])
allowed = (".venv", "wheelhouse", "embedding-cache")
references = {}
for old_root in sorted(path for path in new_root.parent.iterdir() if path.is_dir()):
    if old_root == new_root:
        continue
    for part in allowed:
        for path in sorted((old_root / part).rglob("*")):
            if path.is_file() and not path.is_symlink():
                references.setdefault((part, path.relative_to(old_root / part)), path)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

saved = 0
linked = 0
for part in allowed:
    for path in sorted((new_root / part).rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        reference = references.get((part, path.relative_to(new_root / part)))
        if reference is None or path.samefile(reference):
            continue
        current = path.stat()
        previous = reference.stat()
        if (current.st_uid, current.st_gid, current.st_mode) != (
            previous.st_uid, previous.st_gid, previous.st_mode
        ):
            continue
        if digest(path) != digest(reference):
            continue
        temporary = path.with_name(path.name + ".dedup-" + uuid.uuid4().hex)
        try:
            os.link(reference, temporary)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        if not path.samefile(reference):
            raise RuntimeError("release hardlink verification failed")
        linked += 1
        saved += current.st_size
print(json.dumps({"linked_files": linked, "estimated_bytes_reused": saved}))
"""


def deduplicate_release_assets(root: str) -> None:
    result = remote_python(RELEASE_DEDUP_SCRIPT, root)
    print(f"release dependency reuse: {result}", flush=True)


DATABASE_BACKUP_SCRIPT = """
import json
import os
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
if target.exists():
    raise FileExistsError("deployment database backup already exists")
os.umask(0o077)
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
    with sqlite3.connect(target) as backup:
        connection.backup(backup)
        if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("deployment database backup integrity check failed")
os.chmod(target, 0o600)
print(json.dumps({"path": str(target), "bytes": target.stat().st_size, "integrity": "ok"}))
"""


def backup_application_database(root: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = f"{root}/app-before-{stamp}.sqlite"
    result = remote_python(DATABASE_BACKUP_SCRIPT, "/data/citation-tool/app.db", target)
    print(f"database rollback snapshot: {result}", flush=True)
    return target


def preflight_active_users() -> None:
    connections = remote("ss -Htn state established '( sport = :8765 )'")
    if connections:
        raise RuntimeError(
            "active literature connections detected; defer deployment without disconnecting users"
        )
    started = remote("systemctl show citation-tool -p ActiveEnterTimestamp --value")
    timezone = remote("timedatectl show -p Timezone --value")
    script = """
import datetime
import json
import sqlite3
import sys
import zoneinfo

started = datetime.datetime.strptime(
    sys.argv[1], "%a %Y-%m-%d %H:%M:%S %Z"
).replace(tzinfo=zoneinfo.ZoneInfo(sys.argv[2])).timestamp()
conn = sqlite3.connect("file:/data/citation-tool/app.db?mode=ro", uri=True)
runs = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM agent_runs")]
print(sum(
    run.get("status") in {"pending", "running"}
    and datetime.datetime.fromisoformat(
        run["created_at"].replace("Z", "+00:00")
    ).timestamp() >= started
    for run in runs
))
"""
    active = remote_python(script, started, timezone)
    if active != "0":
        raise RuntimeError(
            "in-progress AgentRun detected; defer deployment without cancelling users"
        )
    print("preflight: no active literature connections or AgentRun", flush=True)


def record_staged_release(root: str, snapshot: str, sample_snapshot: str, count: int) -> None:
    manifest = {
        "schema": "citation-staged-release-v1",
        "release_root": root,
        "status": "staged_not_active",
        "corpus_snapshot": snapshot,
        "corpus_records": count,
        "sample_snapshot": sample_snapshot,
        "sample_records": 50,
        "manuscript_upload_enabled": False,
        "model_acceptance": "not_run",
        "activation_requires": "real remote model, MCP and dense retrieval acceptance",
    }
    remote(
        f"sudo tee {shlex.quote(root + '/staged.json')} >/dev/null",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    note = (
        f"\n\n## citation-tool staged release {root.rsplit('/', 1)[-1]}\n\n"
        f"- Installed at `{root}` with {count} snapshot records and 50 teacher samples.\n"
        f"- Corpus hashes: `{snapshot}` / `teacher-{sample_snapshot}`.\n"
        "- Staged only: service unit, active symlink, Caddy and live database unchanged.\n"
        "- Default model is currently blocked by insufficient balance; real model acceptance "
        "and activation remain pending. No provider credentials were copied or replaced.\n"
        "- Public manuscript uploads remain disabled until production safety is verified.\n"
    )
    remote("sudo tee -a /opt/OPS_NOTES.md >/dev/null", note.encode())
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


def clone_current_release() -> str:
    preflight_active_users()
    release = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = f"/opt/citation-tool/releases/{release}"
    script = """
import json,os,shutil,sys
from pathlib import Path
source=Path('/opt/citation-tool/current').resolve()
target=Path(sys.argv[1])
assert source.parent==target.parent and source.is_dir() and not target.exists()
target.mkdir(mode=0o755)
for name in ('.venv','wheelhouse','embedding-cache'):
    shutil.copytree(source/name,target/name,copy_function=os.link,symlinks=True)
for name in ('uv.lock','requirements.txt','corpus.json','sample-corpus.json'):
    shutil.copy2(source/name,target/name)
manifest=json.loads((source/'staged.json').read_text())
manifest.update(release_root=str(target),status='staged_not_active',model_acceptance='not_run')
(target/'staged.json').write_text(json.dumps(manifest))
print(json.dumps({'staged_root':str(target),'immutable_dependencies_reused_from':str(source)}))
"""
    available = int(remote("df --output=avail -B1 / | tail -n 1"))
    if available < 600_000_000:
        raise RuntimeError("insufficient space for a dependency-reusing release and backup")
    print(remote_python(script, root), flush=True)
    return release


def activate_staged_release(context: dict, release: str) -> None:
    if not re.fullmatch(r"\d{8}-\d{6}", release):
        raise ValueError("invalid staged release name")
    if not re.fullmatch(
        r"/etc/credstore\.encrypted/citation-luna-\d{8}-\d{6}\.json",
        context.get("model_credential") or "",
    ):
        raise ValueError("staged activation requires an explicitly provisioned model credential")
    root = f"/opt/citation-tool/releases/{release}"
    preflight_active_users()
    check = """
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
if root.resolve() == Path('/opt/citation-tool/current').resolve():
    raise ValueError('cannot update an active release')
manifest=json.loads((root/'staged.json').read_text())
assert manifest['status']=='staged_not_active'
assert (root/'.venv/bin/python').is_file()
print(json.dumps({'manifest':manifest,'lock':hashlib.sha256((root/'uv.lock').read_bytes()).hexdigest(),
    'corpus':hashlib.sha256((root/'corpus.json').read_bytes()).hexdigest(),
    'sample':hashlib.sha256((root/'sample-corpus.json').read_bytes()).hexdigest()}))
"""
    staged = json.loads(remote_python(check, root))
    for key, path in (
        ("lock", ROOT / "uv.lock"),
        ("corpus", CORPUS_PATH),
        ("sample", SAMPLE_CORPUS_PATH),
    ):
        if staged[key] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(f"staged {key} differs; full restaging is required")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.dereference = True
        for name in ("src", "web", ".agents", "pyproject.toml"):
            archive.add(
                ROOT / name,
                arcname=name,
                filter=lambda item: (
                    None if "__pycache__" in item.name or item.name.endswith(".pyc") else item
                ),
            )
    archive_bytes = buffer.getvalue()
    available = int(remote("df --output=avail -B1 / | tail -n 1"))
    database_size = int(remote("sudo stat -c %s /data/citation-tool/app.db"))
    required = 500_000_000 + 3 * len(archive_bytes) + 2 * database_size
    if available < required:
        raise RuntimeError("insufficient space for code update, database backup and safety reserve")
    remote(f"sudo tar -xzf - -C {root}", archive_bytes)
    remote(f"{root}/.venv/bin/pip check")
    manifest = staged["manifest"]
    activate_release(
        context,
        root,
        manifest["corpus_snapshot"],
        manifest["sample_snapshot"],
        manifest["corpus_records"],
    )


def deploy(context: dict) -> None:
    preflight_active_users()
    available_bytes = int(remote("df --output=avail -B1 / | tail -n 1").strip())
    if available_bytes < 2_000_000_000:
        raise RuntimeError(
            "insufficient disk space for a safe release: "
            f"{available_bytes} bytes available, 2000000000 required; "
            "refusing to delete old releases or restart the service"
        )
    release = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = f"/opt/citation-tool/releases/{release}"
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    sample_corpus = json.loads(SAMPLE_CORPUS_PATH.read_text(encoding="utf-8"))
    snapshot = sha256_json(corpus["records"])[:16]
    sample_snapshot = sha256_json(sample_corpus["records"])[:16]
    if len(sample_corpus["records"]) != 50:
        raise ValueError("the teacher sample corpus must contain exactly 50 records")
    model = load_model_config(Settings().model_config_path)
    if model is None:
        raise ValueError("approved local model configuration is missing")
    model_payload = json.dumps(
        {
            "api_key": model.api_key,
            "base_url": model.base_url,
            "model": model.model,
            "provider": model.provider,
            "api_type": model.api_type,
        }
    ).encode("utf-8")
    from citation_tool.semantic import EmbeddingIndex

    embedding_index = EmbeddingIndex(
        corpus["records"],
        model_name=Settings().embedding_model,
        cache_dir=ROOT / "data/runtime/embedding-models",
    )
    vector_path = embedding_index.cache_path
    if embedding_index.model_revision == "unresolved":
        raise ValueError("a resolved local model snapshot is required for offline deployment")
    if not vector_path.is_file():
        raise ValueError("a prebuilt vector cache is required; refusing runtime index building")
    vectors = [vector_path]
    model_file = (
        ROOT
        / "data/runtime/embedding-models/models--intfloat--multilingual-e5-small"
        / f"snapshots/{embedding_index.model_revision}/onnx/model.onnx"
    )
    if not model_file.is_file() or model_file.stat().st_size < 400_000_000:
        raise ValueError("the local model snapshot is incomplete")
    model_digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
    remote_model = (
        "/data/citation-tool/embeddings/models/models--intfloat--multilingual-e5-small/"
        f"snapshots/{embedding_index.model_revision}/onnx/model.onnx"
    )
    remote_digest = remote(
        f"if sudo test -f {remote_model}; then sudo sha256sum {remote_model}; fi"
    )
    model_cached = remote_digest.split(" ", 1)[0] == model_digest if remote_digest else False
    prepare_offline_assets()
    archive = package(vectors, embedding_index.model_revision, include_model=not model_cached)
    print(
        f"deploying release={release}, records={len(corpus['records'])}, "
        f"vectors_cached={bool(vectors)}, model_cached={model_cached}",
        flush=True,
    )
    remote("sudo install -d -o ubuntu -g ubuntu -m 0755 /opt/citation-tool/releases")
    remote(f"sudo install -d -o ubuntu -g ubuntu -m 0755 {root}")
    remote("sudo tee /tmp/citation-tool-release.tgz >/dev/null", archive)
    remote(
        f"sudo tar -xzf /tmp/citation-tool-release.tgz -C {root} "
        "&& sudo rm /tmp/citation-tool-release.tgz"
    )
    remote(
        "id -u citation >/dev/null 2>&1 || sudo useradd --system "
        "--home /data/citation-tool --shell /usr/sbin/nologin citation"
    )
    remote("sudo install -d -o citation -g citation -m 0700 /data/citation-tool")
    remote(
        "sudo install -d -o citation -g citation -m 0700 "
        "/data/citation-tool/corpora /data/citation-tool/embeddings/models"
    )
    remote(
        f"sudo install -o citation -g citation -m 0600 {root}/corpus.json "
        f"/data/citation-tool/corpora/{snapshot}.json"
    )
    remote(
        f"sudo install -o citation -g citation -m 0600 {root}/sample-corpus.json "
        f"/data/citation-tool/corpora/teacher-{sample_snapshot}.json"
    )
    if vectors:
        remote(
            f"sudo install -o citation -g citation -m 0600 "
            f"{root}/embedding-cache/{vector_path.name} "
            f"/data/citation-tool/embeddings/{vector_path.name}"
        )
    if not model_cached:
        remote(
            f"sudo cp -a {root}/embedding-models/models/. /data/citation-tool/embeddings/models/"
        )
    remote("sudo chown -R citation:citation /data/citation-tool/embeddings")
    remote(
        "sudo -u citation python3 -c 'import json,sys; from pathlib import Path; "
        'data=json.load(sys.stdin); Path("/data/citation-tool/embeddings/models/'
        'models--intfloat--multilingual-e5-small/files_metadata.json").write_text('
        'json.dumps(data,sort_keys=True,separators=(",",":"))+chr(10))\'',
        model_metadata(),
    )
    remote("sudo -u citation test -w /data/citation-tool")
    remote("sudo install -d -m 0700 /etc/credstore.encrypted")
    credential_present = remote(
        "if sudo test -s /etc/credstore.encrypted/deepseek.local.json; then printf present; fi"
    )
    if credential_present != "present":
        remote(
            "sudo systemd-creds encrypt --with-key=host "
            "- /etc/credstore.encrypted/deepseek.local.json",
            model_payload,
        )
    remote("sudo chmod 0600 /etc/credstore.encrypted/deepseek.local.json")
    remote(
        f"python3 -m venv {root}/.venv && {root}/.venv/bin/pip install "
        "--no-cache-dir --disable-pip-version-check --no-index "
        f"--find-links {root}/wheelhouse --requirement {root}/requirements.txt",
        timeout=600,
    )
    deduplicate_release_assets(root)
    context["staged_root"] = root
    if context.get("stage_only"):
        record_staged_release(root, snapshot, sample_snapshot, len(corpus["records"]))
        return
    activate_release(context, root, snapshot, sample_snapshot, len(corpus["records"]))


def activate_release(
    context: dict, root: str, snapshot: str, sample_snapshot: str, record_count: int
) -> None:
    release = root.rsplit("/", 1)[-1]
    credential = context.get("model_credential")
    if credential:
        if not re.fullmatch(
            r"/etc/credstore\.encrypted/citation-luna-\d{8}-\d{6}\.json", credential
        ):
            raise ValueError("unsupported encrypted model credential path")
        credential_name = "model.local.json"
        remote(f"sudo test -s {credential}")
    else:
        credential = "/etc/credstore.encrypted/deepseek.local.json"
        credential_name = "deepseek.local.json"
    proxy = validate_openai_proxy(context.get("openai_proxy"))
    proxy_environment = f"Environment=CITATION_OPENAI_PROXY={proxy}" if proxy else ""
    uploads = bool(context.get("enable_uploads"))
    if uploads:
        scanner_probe = (
            "from citation_tool.manuscript_scan import ClamAVScanner; "
            "ClamAVScanner('/run/citation-av/clamd.sock').probe(); print('scanner ready')"
        )
        remote(
            f"sudo -u citation env PYTHONPATH={root}/src {root}/.venv/bin/python -c "
            + shlex.quote(scanner_probe)
        )
    unit = f"""[Unit]
Description=Evidence-bounded literature recommendation
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=citation
Group=citation
WorkingDirectory={root}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HOME=/data/citation-tool
Environment=CITATION_AUTH_REQUIRED=0
Environment=CITATION_USER_MODELS_ENABLED=1
Environment=CITATION_MANUSCRIPT_UPLOAD_ENABLED={int(uploads)}
Environment=CITATION_MANUSCRIPT_SCAN_REQUIRED={int(uploads)}
Environment=CITATION_MANUSCRIPT_SCAN_SOCKET=/run/citation-av/clamd.sock
Environment=CITATION_MANUSCRIPT_SCAN_TIMEOUT_S=60
Environment=CITATION_DEFAULT_TENANT=local
Environment=CITATION_AGENT_TIMEOUT_MS=900000
Environment=CITATION_AGENT_FULL_TEXT_TIMEOUT_MS=1800000
Environment=CITATION_MODEL_TIMEOUT_MS=180000
{proxy_environment}
Environment=CITATION_AGENT_MAX_TOOL_CALLS=32
Environment=CITATION_AGENT_RECURSION_LIMIT=96
Environment=CITATION_AGENT_MAX_CONCURRENT_RUNS=2
Environment=CITATION_LIBRARY_MODE=metadata
Environment=CITATION_CANDIDATE_PIPELINE=agentic
Environment=CITATION_CORPUS_PATH=/data/citation-tool/corpora/{snapshot}.json
Environment=CITATION_SAMPLE_CORPUS_PATH=/data/citation-tool/corpora/teacher-{sample_snapshot}.json
Environment=CITATION_EUROPEPMC_ONLINE=1
Environment=CITATION_DB_PATH=/data/citation-tool/app.db
Environment=CITATION_AUTH_DB_PATH=/data/citation-tool/auth.db
Environment=CITATION_BASE_PATH=/literature
Environment=CITATION_SECURE_COOKIES=1
Environment=CITATION_TRUSTED_HOSTS=biodataagent.com,www.biodataagent.com,127.0.0.1,localhost
Environment=CITATION_MODEL_CONFIG=/run/credentials/citation-tool.service/{credential_name}
Environment=CITATION_EMBEDDING_ENABLED=1
Environment=CITATION_EMBEDDING_MODEL=intfloat/multilingual-e5-small
Environment=CITATION_EMBEDDING_DENSE_THRESHOLD=0.80
Environment=CITATION_EMBEDDING_CACHE_DIR=/data/citation-tool/embeddings
Environment=HF_HUB_OFFLINE=1
Environment=RES_OPTIONS=use-vc
LoadCredentialEncrypted={credential_name}:{credential}
ExecStart={root}/.venv/bin/python -m uvicorn \
  --app-dir {root}/src citation_tool.api:create_app --factory \
  --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
CPUQuota=180%
MemoryHigh=1400M
MemoryMax=1900M
TasksMax=64
LimitNOFILE=4096
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/data/citation-tool
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
"""
    context["database_backup"] = backup_application_database(root)
    context["previous_root"] = remote("readlink -f /opt/citation-tool/current")
    backup_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    context["unit_backup"] = f"{root}/previous-{backup_stamp}.service"
    remote(f"sudo cp /etc/systemd/system/citation-tool.service {context['unit_backup']}")
    remote("sudo tee /etc/systemd/system/citation-tool.service >/dev/null", unit.encode())
    preflight_active_users()
    remote("sudo systemctl daemon-reload && sudo systemctl enable citation-tool")
    context["restart_attempted"] = True
    remote("sudo systemctl restart citation-tool")
    time.sleep(2)
    request_script = (
        "python3 -c 'import json,sys,urllib.request; p=json.load(sys.stdin); "
        'body=json.dumps(p["body"]).encode(); '
        'request=urllib.request.Request(p["url"],data=body,headers={"Content-Type":"application/json",'
        '"Accept":"application/json, text/event-stream"}); '
        "print(urllib.request.urlopen(request,timeout=600).read().decode())'"
    )

    def call(method: str, params=None, timeout=600):
        payload = json.dumps(
            {"url": "http://127.0.0.1:8765/mcp", "body": rpc(method, params)}
        ).encode()
        return json.loads(remote(request_script, payload, timeout))

    for attempt in range(10):
        try:
            initialized = call("initialize")
            break
        except RuntimeError:
            if attempt == 9:
                raise
            time.sleep(2)
    if initialized.get("result", {}).get("protocolVersion") != "2025-03-26":
        raise RuntimeError("MCP initialize failed")
    result = call(
        "tools/call",
        {
            "name": "search_literature",
            "arguments": {
                "input_type": "keywords",
                "keywords": ["LFY2", "soybean", "floral organ"],
            },
        },
    )
    content = result.get("result", {}).get("structuredContent", {})
    target = "work:10.1007/s42994-024-00192-2"
    candidates = content.get("candidate_diagnostics", [])
    target_candidate = next((item for item in candidates if item["work_id"] == target), None)
    if target_candidate is None:
        raise RuntimeError("real target was not returned by the remote MCP search")
    if set(target_candidate.get("library_ids", [])) != {
        "europepmc-online",
        "europepmc-snapshot",
        "abiotech-sample",
    }:
        raise RuntimeError("online, large snapshot and teacher sample did not all find the target")
    if (target_candidate.get("reason") or {}).get("status") != "generated":
        raise RuntimeError("real model reason was not generated for the target")
    llm_rank, base_rank = target_candidate.get("llm_rank"), target_candidate.get("base_rank")
    quote = target_candidate.get("reason_evidence_quote")
    if (
        target_candidate.get("ranking_status") != "ranked"
        or not isinstance(llm_rank, int)
        or not isinstance(base_rank, int)
        or target_candidate.get("rank_delta") != base_rank - llm_rank
        or not quote
        or not (
            quote in (target_candidate.get("title") or "")
            or quote in (target_candidate.get("evidence_excerpt") or "")
        )
    ):
        raise RuntimeError("remote target lacks a valid LLM ranking and original evidence quote")
    state_response = call(
        "tools/call",
        {"name": "get_search_state", "arguments": {"query_id": content["query_id"]}},
    )
    workers = state_response["result"]["structuredContent"]["workers"]
    if len(workers) != 3 or any(
        worker.get("worker_state") != "completed"
        or worker.get("source_agent_status") != "completed"
        for worker in workers
    ):
        raise RuntimeError("all three deployed source agents must complete before publication")
    snapshot_workers = [
        worker for worker in workers if worker.get("library_id") == "europepmc-snapshot"
    ]
    if len(snapshot_workers) != 1 or snapshot_workers[0].get("index_snapshot") != (
        f"europepmc-{snapshot}"
    ):
        raise RuntimeError("remote worker did not use the newly deployed corpus snapshot")
    if snapshot_workers[0]["model_versions"].get("dense_status") != "available":
        raise RuntimeError(
            "dense model is unavailable; refusing to expose a sparse-only deployment"
        )
    print(
        f"remote MCP search passed: {len(content.get('candidate_diagnostics', []))} "
        "candidates, target found",
        flush=True,
    )
    block = """\thandle_path /literature/* {
\t\treverse_proxy 127.0.0.1:8765 {
\t\t\tflush_interval -1
\t\t\theader_up X-Forwarded-For {remote_host}
\t\t}
\t}
"""
    injector = (
        'sudo python3 -c \'from pathlib import Path; p=Path("/etc/caddy/Caddyfile"); '
        'text=p.read_text(); marker="biodataagent.com, www.biodataagent.com {"; '
        'start=text.index(marker); end=text.index("\\n}\\n",start); segment=text[start:end]; '
        'assert "handle_path /literature/*" not in segment; '
        'root=segment.rfind("\\n\\thandle {\\n"); assert root >= 0; '
        'p.with_name("Caddyfile.bak-citation-' + release + '").write_text(text); '
        'import base64; block=base64.b64decode("'
        + base64.b64encode(block.encode()).decode()
        + '").decode(); '
        "p.write_text(text[:start+root+1]+block+text[start+root+1:])'"
    )
    route_status = remote(
        "sudo python3 -c 'from pathlib import Path; "
        'text=Path("/etc/caddy/Caddyfile").read_text(); '
        'start=text.index("biodataagent.com, www.biodataagent.com {"); '
        'segment=text[start:text.index("\\n}\\n",start)]; '
        'print("present" if "handle_path /literature/*" in segment '
        'and "reverse_proxy 127.0.0.1:8765" in segment else "missing")\''
    )
    caddy_note = f"Caddyfile.bak-citation-{release}"
    if route_status == "missing":
        remote(injector)
        context["caddy_backup"] = f"/etc/caddy/Caddyfile.bak-citation-{release}"
        remote("sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy")
    elif route_status == "present":
        caddy_note = "复用已验证的现有 /literature/* 路由，未重复写入"
    else:
        raise RuntimeError("unexpected Caddy route state")
    remote("sudo ln -sfn " + root + " /opt/citation-tool/current")
    default_model_note = (
        "OpenAI gpt-6-luna / Responses / store=false"
        if context.get("model_credential")
        else "既有批准配置"
    )
    proxy_note = (
        f"OpenAI 专用 HTTP 回环出口 `{proxy}`，仅经本项目 SSH 加密回连到本机现有出口；"
        "临时依赖 ROG 电脑与回连进程在线，未改共享代理或全局 DNS，不能称独立云端出口。"
        if proxy
        else "模型使用服务器直接出口。"
    )
    upload_note = (
        "公网上传已开启，必须经本地 ClamAV 完整扫描；不可用、过期或不完整时拒绝。"
        if uploads
        else "公网上传关闭，仍待扫描防护与验收。"
    )
    note = f"""

## 文献推荐工具 citation-tool（{release}）

- 入口：`https://biodataagent.com/literature/`；MCP：`POST /literature/mcp`（匿名，不用于私稿）。
- 发布：`{root}`，`current` 指向该目录；`/data/citation-tool/` 持久化 SQLite 与语料，进入每日备份。
- 单进程 FastAPI/uvicorn，仅 `127.0.0.1:8765`，`citation` 低权限用户。
  Caddy 仅新增 `/literature/*`，不新增公网端口，不修改旧 Docker 业务。
- 认证：按用户要求关闭令牌认证，网页/API/MCP 匿名开放；不得用于私稿或保密材料。
  模型 JSON 使用 `LoadCredentialEncrypted` 与 `{credential}`，不写明文环境文件。
  本次默认模型路径：{default_model_note}。
- 网络：{proxy_note}
  用户可显式选择 DeepSeek、Moonshot AI、OpenAI 官方端点；自有 Key 仅在浏览器
  sessionStorage 和单次运行内存中使用，不写入持久化。未选择时使用默认模型。
- 检索：Europe PMC 在线 API、{record_count} 篇多学科快照（`{snapshot}`，
  E5 small + BM25 RRF）与 50 篇 aBiotech 老师样例（`teacher-{sample_snapshot}`，BM25）。
  当前使用来源 ReAct 子 Agent、统一候选池与固定背景共同基础分；
  LLM 先盲排，再参考同池基础分和位次复核，不与基础分混算。
  复核失败保留有效盲排并标记部分完成；不补 ID 或模板理由，不是全文核验。
  理由按租户与证据输入哈希缓存；未接入此前退化的训练 LTR。
- 服务端支持多轮对话与独立排行榜。新网页检索在定榜后自动准备合法开放全文的引用建议，
  可选择 APA 7、Vancouver（NLM）、BibTeX 或 RIS 书目格式。
  建议须作者核对，不自动改变候选资格或快照，不写回稿件。
  首页可直接上传 PDF/DOCX，OCR 已随包安装。{upload_note}
  扫描是恶意文件防护，不证明科学结论、OCR 或 PDF 阅读顺序正确。
- 容量：仅对发布目录间同相对路径、同 SHA-256、同 owner/mode 的依赖文件做硬链接复用；
  不删除旧发布、备份、文献数据或凭据。
- 升级前通过 SQLite backup 保存一致回退快照 `{context["database_backup"]}`，
  quick_check 通过、权限 0600；不自动回卷数据库以免覆盖上线后的用户记录。
- 运行中 Agent/Worker 重启后不自动续跑；已提交事件、查询与快照持久化。
  Caddy：{caddy_note}。
"""
    remote("sudo tee -a /opt/OPS_NOTES.md >/dev/null", note.encode())
    print("https://biodataagent.com/literature/\nMCP: anonymous POST /literature/mcp", flush=True)


def main(
    *,
    stage_only: bool = False,
    staged_release: str | None = None,
    model_credential: str | None = None,
    openai_proxy: str | None = None,
    clone_current: bool = False,
    enable_uploads: bool = False,
) -> None:
    context = {"stage_only": stage_only}
    if enable_uploads:
        context["enable_uploads"] = True
    if model_credential:
        context["model_credential"] = model_credential
    if openai_proxy:
        context["openai_proxy"] = validate_openai_proxy(openai_proxy)
    try:
        if clone_current:
            if stage_only or staged_release or not model_credential:
                raise ValueError(
                    "clone activation requires only an explicit encrypted model credential"
                )
            staged_release = clone_current_release()
        if staged_release:
            if stage_only:
                raise ValueError("cannot stage and activate simultaneously")
            activate_staged_release(context, staged_release)
        else:
            deploy(context)
    except Exception as exc:
        if context.get("unit_backup"):
            restart = (
                " && sudo systemctl restart citation-tool"
                if context.get("restart_attempted")
                else ""
            )
            remote(
                f"sudo cp {context['unit_backup']} /etc/systemd/system/citation-tool.service "
                f"&& sudo systemctl daemon-reload{restart}"
            )
            if context.get("previous_root"):
                remote(f"sudo ln -sfn {context['previous_root']} /opt/citation-tool/current")
            print("remote verification failed; previous service unit restored", flush=True)
        if context.get("caddy_backup"):
            remote(
                f"sudo cp {context['caddy_backup']} /etc/caddy/Caddyfile "
                "&& sudo caddy validate --config /etc/caddy/Caddyfile "
                "&& sudo systemctl reload caddy"
            )
            print("new Caddy route rolled back; existing routes preserved", flush=True)
        raise RuntimeError(
            f"deployment failed with rollback attempted: {type(exc).__name__}"
        ) from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Install an inactive release; do not change the live service or pass model gates.",
    )
    parser.add_argument("--activate-staged", metavar="RELEASE")
    parser.add_argument(
        "--model-credential", help="Explicitly provisioned encrypted credential path"
    )
    parser.add_argument("--openai-proxy", help="Optional project-owned HTTP loopback proxy")
    parser.add_argument(
        "--clone-current", action="store_true", help="Reuse immutable active dependencies"
    )
    parser.add_argument(
        "--enable-uploads", action="store_true", help="Require ready ClamAV before uploads"
    )
    options = parser.parse_args()
    main(
        stage_only=options.stage_only,
        staged_release=options.activate_staged,
        model_credential=options.model_credential,
        openai_proxy=options.openai_proxy,
        clone_current=options.clone_current,
        enable_uploads=options.enable_uploads,
    )
