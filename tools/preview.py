#!/usr/bin/env python3
"""Serve the local archive with byte ranges for seeking in large media files."""

from __future__ import annotations

import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchiveHandler(SimpleHTTPRequestHandler):
    remaining: int | None = None

    def send_head(self):
        self.remaining = None
        header = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not header or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if not match or size == 0 or not any(match.groups()):
            return self.invalid_range(size)
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:
            length = int(last)
            if not length:
                return self.invalid_range(size)
            start, end = max(0, size - length), size - 1
        if start >= size or start > end:
            return self.invalid_range(size)
        stream = path.open("rb")
        stream.seek(start)
        self.remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(os.fspath(path)))
        self.send_header("Content-Length", str(self.remaining))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.end_headers()
        return stream

    def invalid_range(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    def end_headers(self):
        request_path = self.path.split("?", 1)[0]
        if request_path == "/" or request_path.endswith((".html", ".css", ".js", ".json")):
            self.send_header("Cache-Control", "no-store")
        if self.remaining is None:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def copyfile(self, source, outputfile):
        if self.remaining is None:
            return super().copyfile(source, outputfile)
        left = self.remaining
        while left:
            block = source.read(min(left, 64 * 1024))
            if not block:
                break
            try:
                outputfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                break
            left -= len(block)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    handler = partial(ArchiveHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Local: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
