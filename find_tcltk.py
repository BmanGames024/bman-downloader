"""Print the Tcl and Tk library folders for Nuitka's --tcl-library-dir / --tk-library-dir.

Python 3.14+ ships Tcl/Tk 9, which keeps its library scripts packed inside the
DLL (paths like //zipfs:/lib/tcl/tcl_library). Nuitka needs real folders, so
those get unpacked into build_tcltk next to this file.

Output (two lines):  TCL=<folder>  and  TK=<folder>
"""
import os
import shutil
import sys
import tkinter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "build_tcltk")


def tcl_path(path: str) -> str:
    return "{" + path.replace("\\", "/") + "}"


def real_folder(interp, library: str, name: str) -> str:
    if not library.startswith("//zipfs:"):
        return os.path.normpath(library)
    dest = os.path.join(OUT, name)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(OUT, exist_ok=True)
    # Tcl itself can read its zipfs, so let it copy the folder out.
    interp.eval(f"file copy -force {tcl_path(library)} {tcl_path(dest)}")
    if not os.path.isdir(dest):
        raise SystemExit(f"Could not unpack {library}")
    return dest


def main() -> None:
    root = tkinter.Tk()
    root.withdraw()
    try:
        tcl_lib = root.eval("info library")
        tk_lib = root.eval("set tk_library")
        tcl_ver = root.eval("info tclversion")
        tk_ver = root.eval("set tk_version")
        tcl_dir = real_folder(root, tcl_lib, f"tcl{tcl_ver}")
        tk_dir = real_folder(root, tk_lib, f"tk{tk_ver}")
    finally:
        root.destroy()

    for folder, marker in ((tcl_dir, "init.tcl"), (tk_dir, "tk.tcl")):
        if not os.path.isfile(os.path.join(folder, marker)):
            print(f"WARNING: {marker} not found in {folder}", file=sys.stderr)

    print(f"TCL={tcl_dir}")
    print(f"TK={tk_dir}")


if __name__ == "__main__":
    main()
