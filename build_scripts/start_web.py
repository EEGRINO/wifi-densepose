"""Prepare the local environment and run the existing web and CSI servers."""

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time
import webbrowser


SERVER_DIR = Path(__file__).resolve().parents[1] / "laptop_server"
LOOPBACK = "127.0.0.1"
HTTP_PORT = 8000
WS_PORT = 8765
START_PAGE = f"http://{LOOPBACK}:{HTTP_PORT}/localization.html"


def prepare_environment():
    environment = SERVER_DIR / ".venv"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        print("Создаю окружение laptop_server/.venv…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)

    has_pip = subprocess.run([
        str(interpreter), "-c", "import importlib.util, sys; sys.exit(importlib.util.find_spec('pip') is None)",
    ]).returncode == 0
    if not has_pip:
        subprocess.run([str(interpreter), "-m", "ensurepip"], check=True)

    print("Проверяю зависимости…", flush=True)
    subprocess.run([
        str(interpreter), "-X", "utf8", "-m", "pip", "--disable-pip-version-check",
        "install", "--no-input", "--quiet", "-r", str(SERVER_DIR / "requirements.txt"),
    ], check=True)
    return interpreter


def is_listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((LOOPBACK, port)) == 0


async def announce_start(open_browser):
    deadline = time.monotonic() + 20
    while not is_listening(WS_PORT):
        if time.monotonic() >= deadline:
            raise RuntimeError("Сервер CSI не запустил WebSocket на порту 8765. См. лог выше.")
        await asyncio.sleep(0.1)
    print(f"План комнаты\n{START_PAGE}\nДля остановки нажмите Ctrl+C в этом окне.", flush=True)
    if open_browser:
        Thread(target=webbrowser.open, args=(START_PAGE,), daemon=True).start()


async def run_csi_server(pipeline, open_browser):
    await asyncio.gather(pipeline.main(), announce_start(open_browser))


def serve(open_browser):
    sys.path.insert(0, str(SERVER_DIR))
    import diagnostics

    diagnostics.start("start_web.py", http_port=HTTP_PORT)
    phase = "preflight"
    try:
        for port in (HTTP_PORT, WS_PORT):
            if is_listening(port):
                diagnostics.event("server.bind_failed", level="error", protocol="tcp",
                                  port=port, reason="port_in_use")
                raise RuntimeError(f"Порт {port} уже занят. Если WiFi DensePose запущен, откройте {START_PAGE}")

        phase = "load_pipeline"
        import nn_pipeline

        phase = "bind_http"
        static = partial(SimpleHTTPRequestHandler, directory=SERVER_DIR / "static")
        with ThreadingHTTPServer((LOOPBACK, HTTP_PORT), static) as http:
            diagnostics.event("server.socket_bound", protocol="http", port=HTTP_PORT)
            phase = "serve"
            worker = Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                asyncio.run(run_csi_server(nn_pipeline, open_browser))
            finally:
                http.shutdown()
                worker.join()
    except KeyboardInterrupt:
        diagnostics.finish("keyboard_interrupt")
        raise
    except Exception as error:
        if phase == "bind_http":
            diagnostics.failure("server.bind_failed", error, protocol="http", port=HTTP_PORT)
        diagnostics.failure("server.failed", error, phase=phase)
        diagnostics.finish("failure")
        raise
    else:
        diagnostics.finish("normal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup-only", action="store_true", help="установить зависимости без запуска")
    parser.add_argument("--no-browser", action="store_true", help="запустить серверы без открытия браузера")
    parser.add_argument("--run-installed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("Требуется Python 3.11 или новее. Проверенная версия — Python 3.12.", file=sys.stderr)
        return 1

    try:
        if not args.run_installed:
            interpreter = prepare_environment()
            if args.setup_only:
                print("Зависимости готовы. Запустите эту же команду без --setup-only.")
                return 0
            if Path(sys.executable).absolute() != interpreter:
                command = [str(interpreter), "-X", "utf8", "-u", str(Path(__file__).resolve()), "--run-installed"]
                if args.no_browser:
                    command.append("--no-browser")
                return subprocess.call(command)
        serve(open_browser=not args.no_browser)
    except KeyboardInterrupt:
        print("Серверы остановлены.")
        return 0
    except subprocess.CalledProcessError:
        print("Подготовка окружения не удалась. Проверьте вывод выше и доступ к PyPI при первой установке.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
