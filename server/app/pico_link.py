"""TCP server the Pico connects to (port 8080).

Accepts newline-delimited JSON (firmware v2) and also the original firmware's
back-to-back JSON objects without separators (legacy mode).
"""
import asyncio
import json
import time

_decoder = json.JSONDecoder()


def split_frames(buf: str):
    """Pull complete JSON objects from the buffer; return (frames, rest)."""
    frames = []
    i = 0
    n = len(buf)
    while i < n:
        while i < n and buf[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        try:
            obj, end = _decoder.raw_decode(buf, i)
        except json.JSONDecodeError:
            nl = buf.find("\n", i)
            if nl == -1 or len(buf) - i < 2:
                break  # incomplete object: wait for more bytes
            # a full line that is not valid JSON: drop it
            i = nl + 1
            continue
        frames.append(obj)
        i = end
    return frames, buf[i:]


async def serve(hub, host="0.0.0.0", port=8080):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        hub.on_connect(peer)
        buf = ""
        try:
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "ignore")
                if len(buf) > 1_000_000:
                    buf = ""
                frames, buf = split_frames(buf)
                for fr in frames:
                    if not isinstance(fr, dict):
                        continue
                    if hub.peer != peer:
                        continue  # a newer connection took over
                    lines = hub.on_frame(fr, time.time())
                    writer.write(("\n".join(lines) + "\n").encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            hub.on_disconnect(peer)
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    return server
