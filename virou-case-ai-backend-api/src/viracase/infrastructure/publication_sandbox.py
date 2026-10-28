from pathlib import Path


def sandbox_command():
    command = [
        "bwrap",
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/bin", "/lib", "/lib64"):
        path = Path(directory)
        if path.is_symlink():
            command.extend(["--symlink", str(path.readlink()), directory])
        elif path.is_dir():
            command.extend(["--ro-bind", directory, directory])
    return command
