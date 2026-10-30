#!/usr/bin/env python3
"""Portable host build driver. Outputs are always static Linux ARM64 executables.

Builds need only Python, Zig and Go on Linux/macOS/Windows. Native development
checks remain POSIX-only; no command in this module contacts a CGS2 device.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]


class BuildError(RuntimeError):
    pass


def executable(name: str, explicit: str | None = None) -> str:
    requested = explicit or os.environ.get(name.upper()) or name
    found = shutil.which(requested)
    if not found:
        raise BuildError(f"Cannot find {name}: {requested}. Run 'mise install' or supply --{name} PATH.")
    return found


def build_environment(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key, relative in {"GOCACHE":"go-cache", "ZIG_GLOBAL_CACHE_DIR":"zig-cache", "ZIG_LOCAL_CACHE_DIR":"zig-local-cache"}.items():
        directory = repo / ".tools" / relative
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory.resolve())
    env.update(GOTOOLCHAIN="local", GOOS="linux", GOARCH="arm64", CGO_ENABLED="0")
    return env


def run(command: list[str], repo: Path, env: dict[str,str], *, capture=False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, cwd=repo, env=env, check=True, text=True,
                              capture_output=capture, shell=False)
    except FileNotFoundError as error:
        raise BuildError(f"Build tool is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise BuildError(f"{Path(command[0]).name} failed with exit status {error.returncode}.") from error


def verify_target(path: Path) -> None:
    """Reject host executables and dynamic interpreters before publishing a build."""
    data = path.read_bytes()
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
        raise BuildError(f"{path.name} is not a little-endian ARM64 ELF executable")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if size < 56 or not count or count > 1024 or offset+size*count > len(data):
        raise BuildError(f"{path.name} has an invalid ELF program table")
    if any(struct.unpack_from("<I", data, offset+n*size)[0] == 3 for n in range(count)):
        raise BuildError(f"{path.name} needs a dynamic loader; a static build is required")


def _compile(repo: Path, output_dir: Path | None, name: str, make_command, env) -> Path:
    destination = (output_dir or repo/"build").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=name+"-", suffix=".tmp", dir=destination)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        run(make_command(str(temporary)), repo, env)
        verify_target(temporary)
        temporary.chmod(0o755)
        result = destination/name
        os.replace(temporary, result)
        print(f"Built {result}")
        return result
    finally:
        temporary.unlink(missing_ok=True)


def build_filter(repo: Path = REPO, output_dir: Path | None = None, zig: str | None = None) -> Path:
    repo = Path(repo).resolve()
    tool = executable("zig", zig)
    return _compile(repo, output_dir, "cgs2-filter", lambda out:[tool,"cc","-target","aarch64-linux-musl",
        "-static","-Os","-std=c11","-Wall","-Wextra","-Werror","-fstack-protector-strong",
        str(repo/"src/cgs2-filter.c"),"-o",out], build_environment(repo))


def build_services(repo: Path = REPO, output_dir: Path | None = None, go: str | None = None) -> Path:
    repo = Path(repo).resolve()
    tool = executable("go", go)
    return _compile(repo, output_dir, "cgs2-services", lambda out:[tool,"build","-trimpath",
        "-ldflags=-s -w -buildid=","-o",out,"./src/services"], build_environment(repo))


def build_all(repo: Path = REPO, output_dir: Path | None = None, zig: str | None = None, go: str | None = None) -> dict[str,Path]:
    # Check both prerequisites before doing either build.
    executable("zig",zig); executable("go",go)
    return {"filter":build_filter(repo,output_dir,zig), "services":build_services(repo,output_dir,go)}


def zig_lib_directory(text: str) -> Path:
    """Zig 0.17 uses ZON; older compiler versions printed JSON."""
    try:
        value = json.loads(text)
        name = value.get("lib_dir") or value.get("zig_lib_dir")
        if isinstance(name,str): return Path(name)
    except (json.JSONDecodeError, AttributeError):
        pass
    match = re.search(r'^\s*\.lib_dir\s*=\s*("(?:[^"\\]|\\.)*")',text,re.MULTILINE)
    if match: return Path(json.loads(match[1]))
    raise BuildError("Could not locate Zig's Linux UAPI headers from 'zig env'.")


def test_filter(repo: Path = REPO, zig: str | None = None, cc: str | None = None) -> bool:
    host = platform.system()
    if host not in ("Linux","Darwin"):
        print("SKIP native filter tests: they use POSIX fork/network headers; use Linux or macOS. Target cross-build remains available.")
        return False
    repo = Path(repo).resolve(); env = build_environment(repo)
    for key in ("GOOS","GOARCH","CGO_ENABLED"): env.pop(key,None)
    compiler = executable("cc",cc or ("clang" if host=="Darwin" else None))
    output = repo/"build/test-filter"
    output.parent.mkdir(parents=True,exist_ok=True)
    command=[compiler,"-std=c11","-Wall","-Wextra","-Werror","-Wno-unused-function","-fsanitize=address,undefined","-g"]
    if host=="Darwin":
        tool=executable("zig",zig)
        lib=zig_lib_directory(run([tool,"env"],repo,env,capture=True).stdout)
        command += ["-idirafter",str(lib/"libc/include/any-linux-any")]
    command += [str(repo/"tests/test_filter.c"),"-o",str(output)]
    run(command,repo,env);run([str(output)],repo,env)
    return True


def test_services(repo: Path = REPO, go: str | None = None, *, race=False) -> bool:
    if platform.system() not in ("Linux","Darwin"):
        print("SKIP native services tests: the controller uses POSIX file locks. Building the Linux ARM64 target still verifies compilation.")
        build_services(repo,go=go)
        return False
    repo = Path(repo).resolve();env=build_environment(repo)
    for key in ("GOOS","GOARCH"):env.pop(key,None)
    env["CGO_ENABLED"]="1" if race else "0"
    command=[executable("go",go),"test"]+(["-race"] if race else [])+["./src/services"]
    run(command,repo,env)
    return True


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("all","filter","services","test-filter","test-services"),nargs="?",default="all")
    parser.add_argument("--zig");parser.add_argument("--go");parser.add_argument("--cc")
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--race",action="store_true",help="Enable Go race detector (requires a native C toolchain)")
    args=parser.parse_args()
    try:
        if args.action=="all":build_all(output_dir=args.output_dir,zig=args.zig,go=args.go)
        elif args.action=="filter":build_filter(output_dir=args.output_dir,zig=args.zig)
        elif args.action=="services":build_services(output_dir=args.output_dir,go=args.go)
        elif args.action=="test-filter":test_filter(zig=args.zig,cc=args.cc)
        else:test_services(go=args.go,race=args.race)
    except BuildError as error:
        parser.exit(1,f"Build error: {error}\n")


if __name__ == "__main__":main()
