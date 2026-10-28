import os
import resource
import sys

from viracase.infrastructure.publication_build import TOOLCHAIN
from viracase.infrastructure.publication_sandbox import sandbox_command
from viracase.infrastructure.publication_resources import workload_nproc


def main():
    source, stack, revision = sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (3_000_000_000, 3_000_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8_000_000, 8_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    process_limit = workload_nproc()
    command = sandbox_command() + [
        "--setenv",
        "NODE_PATH",
        "/toolchain/node_modules",
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
        "/usr/bin/prlimit",
        f"--nproc={process_limit}:{process_limit}",
        "--",
        "/usr/bin/node",
        "--max-old-space-size=256",
        "/toolchain/probe.mjs" if stack == "__probe__" else "/toolchain/build.mjs",
        stack,
        revision,
    ]
    os.execvpe("bwrap", command, {"PATH": "/usr/bin:/bin"})


if __name__ == "__main__":
    main()
