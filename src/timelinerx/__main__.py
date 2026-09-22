"""Entry point: GUI when started without arguments, CLI otherwise.

The Windows build ships two executables: ``TimelinerX.exe`` (GUI
subsystem) and ``timelinerx-cli.exe`` (console, for scripting). If the
GUI executable is started with arguments it attaches to the parent console
when there is one, so ``--version`` and friends still print something.
"""

import os
import sys


def _ensure_streams() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                return
        except Exception:  # noqa: BLE001
            pass
    devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or devnull
    sys.stderr = sys.stderr or devnull


def main() -> int:
    exe = os.path.basename(sys.argv[0]).lower()
    cli_exe = exe.startswith("timelinerx-cli")
    if len(sys.argv) > 1 or (cli_exe and getattr(sys, "frozen", False)):
        _ensure_streams()
        from .cli.main import main as cli_main
        return cli_main(sys.argv[1:])
    from .app.main import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
