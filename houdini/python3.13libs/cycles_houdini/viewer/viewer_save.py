# SPDX-License-Identifier: Apache-2.0
"""Asynchronous viewer exports with exclusive filename reservation."""

import os
from pathlib import Path
import threading
from PySide6 import QtGui
from ..rop import output


def _begin(paths, write):
    event = threading.Event()
    event.error = None

    def run():
        reserved = []
        try:
            for name in paths:
                path = Path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
                os.close(fd)
                reserved.append(path)
            write()
        except Exception as error:
            event.error = error
            for path in reserved:
                try:
                    if path.stat().st_size == 0:
                        path.unlink()
                except OSError:
                    pass
        finally:
            event.set()

    threading.Thread(target=run, name="Cycles viewer save", daemon=True).start()
    return event


def begin_exr(session, jobs):
    from copy import deepcopy

    jobs = deepcopy(jobs)
    return _begin([job["filename"] for job in jobs], lambda: output.write_outputs(session, jobs))


def begin_image(image, filename, kind):
    def write():
        source = image
        if kind == "jpg":
            # JPEG has no alpha. Composite on black instead of discarding it.
            source = QtGui.QImage(image.size(), QtGui.QImage.Format_RGB32)
            source.fill(QtGui.QColor("black"))
            painter = QtGui.QPainter(source)
            painter.drawImage(0, 0, image)
            painter.end()
        writer = QtGui.QImageWriter(filename, kind.encode("ascii"))
        if kind == "jpg":
            writer.setQuality(95)
        if not writer.write(source):
            raise OSError(writer.errorString())

    return _begin([filename], write)
