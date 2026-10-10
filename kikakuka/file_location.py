import os
import platform
import subprocess


def file_location(path):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        return path
    return os.path.dirname(path)


def open_file_location(path):
    location = file_location(path)
    if platform.system() == "Darwin":
        subprocess.run(["open", location])
    elif platform.system() == "Windows":
        subprocess.run(["explorer", location])
    else:
        subprocess.run(["xdg-open", location])
