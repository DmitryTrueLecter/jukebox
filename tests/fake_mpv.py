"""Minimal stand-in for `mpv --idle --input-ipc-server=...` used by PlayerTests.

The file name drives the behavior: "*eof*" ends after 0.2 s, "*broken*" fails to
load, "*crash*" kills the process; anything else plays until stopped.
"""
import json
import os
import socket
import sys
import threading

path = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--input-ipc-server="))
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
conn, _ = server.accept()
out = conn.makefile("wb")
lock = threading.Lock()
entry = 0


def send(message):
    with lock:
        out.write((json.dumps(message) + "\n").encode())
        out.flush()


def finish(entry_id, reason, **extra):
    send({"event": "end-file", "reason": reason, "playlist_entry_id": entry_id, **extra})


for line in conn.makefile("rb"):
    request = json.loads(line)
    command, rid = request["command"], request.get("request_id")
    reply = {"request_id": rid, "error": "success"}
    if command[0] == "loadfile":
        if entry:
            finish(entry, "stop")
        entry += 1
        reply["data"] = {"playlist_entry_id": entry}
        send(reply)
        name = os.path.basename(command[1])
        if "crash" in name:
            os._exit(3)
        if "broken" in name:
            finish(entry, "error", file_error="unrecognized file format")
        elif "eof" in name:
            threading.Timer(0.2, finish, (entry, "eof")).start()
        continue
    if command[0] == "get_property":
        reply["data"] = {"time-pos": 12.5, "duration": 60.0}.get(command[1])
    elif command[0] == "stop":
        finish(entry, "stop")
    elif command[0] == "quit":
        send(reply)
        break
    send(reply)
