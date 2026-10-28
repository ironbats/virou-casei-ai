import os
import resource
import sys

from viracase.infrastructure.publication_build import TOOLCHAIN


def main():
    source, stack, revision = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (3_000_000_000, 3_000_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8_000_000, 8_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    command = [
        "bwrap",
        "--unshare-all",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "NODE_PATH",
        "/toolchain/node_modules",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--ro-bind",
        str(TOOLCHAIN),
        "/toolchain",
        "--ro-bind",
        source,
        "/project",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        "/project",
        "/usr/bin/node",
        "--max-old-space-size=256",
        "/toolchain/probe.mjs" if stack == "__probe__" else "/toolchain/build.mjs",
        stack,
        revision,
    ]
    os.execvpe("bwrap", command, {"PATH": "/usr/bin:/bin"})


if __name__ == "__main__":
    main()
