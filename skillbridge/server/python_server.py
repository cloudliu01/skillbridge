from __future__ import annotations

import contextlib
import logging
import subprocess
from argparse import ArgumentParser
from logging import WARNING, basicConfig, getLogger
from os import chmod, chown, getenv
from pathlib import Path
from select import select
from socketserver import BaseRequestHandler, BaseServer, StreamRequestHandler, ThreadingMixIn
from sys import argv, platform, stderr, stdin, stdout
from sys import exit as sys_exit
from typing import Iterable

LOG_DIRECTORY = Path(getenv('SKILLBRIDGE_LOG_DIRECTORY', '.'))
LOG_FILE = LOG_DIRECTORY / 'skillbridge_server.log'
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
LOG_DATE_FORMAT = '%d.%m.%Y %H:%M:%S'
LOG_LEVEL = WARNING

basicConfig(filename=LOG_FILE, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logger = getLogger("python-server")


def send_to_skill(data: str) -> None:
    stdout.write(data)
    stdout.write("\n")
    stdout.flush()


def read_from_skill(timeout: float | None) -> str:
    readable = data_ready(timeout)

    if readable:
        return stdin.readline()

    logger.debug("timeout")
    return 'failure <timeout>'


def create_windows_server_class(single: bool) -> type[BaseServer]:
    from socketserver import TCPServer  # noqa: PLC0415

    class SingleWindowsServer(TCPServer):
        request_queue_size = 0
        allow_reuse_address = True

        def __init__(self, port: int, handler: type[BaseRequestHandler]) -> None:
            super().__init__(('localhost', port), handler)

        def server_bind(self) -> None:
            try:
                import socket  # noqa: PLC0415

                self.socket.ioctl(  # type: ignore[attr-defined]
                    socket.SIO_LOOPBACK_FAST_PATH,  # type: ignore[attr-defined]
                    True,  # noqa: FBT003
                )
            except ImportError:
                pass
            super().server_bind()

    class ThreadingWindowsServer(ThreadingMixIn, SingleWindowsServer):
        pass

    return SingleWindowsServer if single else ThreadingWindowsServer


def data_windows_ready(timeout: float | None) -> bool:
    _ = timeout
    return True


def create_unix_server_class(single: bool) -> type[BaseServer]:
    from socketserver import UnixStreamServer  # noqa: PLC0415

    class SingleUnixServer(UnixStreamServer):
        request_queue_size = 0
        allow_reuse_address = True

        allow_gid: int | None = None
        allow_extra_user: str | None = None

        def __init__(self, file: str, handler: type[BaseRequestHandler]) -> None:
            self.path = f'/tmp/skill-server-{file}.sock'
            with contextlib.suppress(FileNotFoundError):
                Path(self.path).unlink()

            super().__init__(self.path, handler)

            if self.allow_gid is not None:
                from grp import getgrgid  # noqa: PLC0415

                chown(self.path, -1, self.allow_gid)
                chmod(self.path, 0o660)
                logger.info(
                    "shared unix socket %s with gid=%s (%s)",
                    self.path,
                    self.allow_gid,
                    getgrgid(self.allow_gid).gr_name,
                )

            if self.allow_extra_user is not None:
                if self.allow_gid is None:
                    chmod(self.path, 0o600)
                subprocess.run(
                    ['setfacl', '-m', f'u:{self.allow_extra_user}:rw', self.path],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info(
                    "shared unix socket %s with user=%s via ACL",
                    self.path,
                    self.allow_extra_user,
                )

    class ThreadingUnixServer(ThreadingMixIn, SingleUnixServer):
        pass

    return SingleUnixServer if single else ThreadingUnixServer


def resolve_allow_gid(allow_gid: int | None) -> int | None:
    if platform == 'win32':
        return None
    return allow_gid


def data_unix_ready(timeout: float | None) -> bool:
    readable, _, _ = select([stdin], [], [], timeout)

    return bool(readable)


if platform == 'win32':
    data_ready = data_windows_ready
    create_server_class = create_windows_server_class
else:
    create_server_class = create_unix_server_class
    data_ready = data_unix_ready


class Handler(StreamRequestHandler):
    def receive_all(self, remaining: int) -> Iterable[bytes]:
        while remaining:
            data = self.request.recv(remaining)
            remaining -= len(data)
            yield data

    def handle_one_request(self) -> bool:
        length = self.request.recv(10)
        if not length:
            logger.warning(f"client {self.client_address} lost connection")
            return False
        logger.debug(f"got length {length}")

        length = int(length)
        command = b''.join(self.receive_all(length))

        logger.debug(f"received {len(command)} bytes")

        if command.startswith(b'$close'):
            logger.debug(f"client {self.client_address} disconnected")
            return False
        logger.debug(f"got data {command[:1000].decode()}")

        send_to_skill(command.decode())
        logger.debug("sent data to skill")
        result = read_from_skill(self.server.skill_timeout).encode()  # type: ignore[attr-defined]
        logger.debug(f"got response from skill {result[:1000]!r}")

        self.request.send(f'{len(result):10}'.encode())
        self.request.send(result)
        logger.debug("sent response to client")

        return True

    def try_handle_one_request(self) -> bool:
        try:
            return self.handle_one_request()
        except Exception:
            logger.exception("Failed to handle request")
            return False

    def handle(self) -> None:
        logger.info(f"client {self.client_address} connected")
        client_is_connected = True
        while client_is_connected:
            client_is_connected = self.try_handle_one_request()


def main(
    id_: str,
    log_level: str,
    notify: bool,
    single: bool,
    timeout: float | None,
    allow_gid: int | None,
    allow_extra_user: str | None,
) -> None:
    logger.setLevel(getattr(logging, log_level))

    server_class = create_server_class(single)
    resolved_allow_gid = resolve_allow_gid(allow_gid)

    if platform != 'win32':
        server_class.allow_gid = resolved_allow_gid  # type: ignore[attr-defined]
        server_class.allow_extra_user = allow_extra_user  # type: ignore[attr-defined]

    with server_class(id_, Handler) as server:
        server.skill_timeout = timeout  # type: ignore[attr-defined]
        logger.info(
            f"starting server id={id_} log={log_level} notify={notify} "
            f"single={single} timeout={timeout} allow_gid={resolved_allow_gid} "
            f"allow_extra_user={allow_extra_user}",
        )
        if notify:
            send_to_skill('running')
        server.serve_forever()


if __name__ == '__main__':
    log_levels = ["DEBUG", "WARNING", "INFO", "ERROR", "CRITICAL", "FATAL"]
    argument_parser = ArgumentParser(argv[0])
    if platform == 'win32':
        argument_parser.add_argument('id', type=int)
    else:
        argument_parser.add_argument('id')
    argument_parser.add_argument('log_level', choices=log_levels)
    argument_parser.add_argument('--notify', action='store_true')
    argument_parser.add_argument('--single', action='store_true')
    argument_parser.add_argument('--timeout', type=float, default=None)
    argument_parser.add_argument('--allow-gid', type=int, default=None)
    argument_parser.add_argument('--allow-extra-user', default=None)

    ns = argument_parser.parse_args()

    if platform == 'win32' and ns.timeout is not None:
        print("Timeout is not possible on Windows", file=stderr)
        sys_exit(1)

    if platform == 'win32' and (ns.allow_gid is not None or ns.allow_extra_user is not None):
        print("Socket sharing options are not supported on Windows", file=stderr)
        sys_exit(1)

    with contextlib.suppress(KeyboardInterrupt):
        main(
            ns.id,
            ns.log_level,
            ns.notify,
            ns.single,
            ns.timeout,
            ns.allow_gid,
            ns.allow_extra_user,
        )
