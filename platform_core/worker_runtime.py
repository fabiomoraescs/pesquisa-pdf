"""Primitivas pequenas para encerrar workers duráveis sem abandonar claims novos."""

from __future__ import annotations

from contextlib import contextmanager
import signal
from threading import Event, current_thread, main_thread
from typing import Iterator


@contextmanager
def graceful_worker_shutdown(existing_event: Event | None = None) -> Iterator[Event]:
    """Converte SIGTERM/SIGINT em parada cooperativa do loop do worker.

    Uma tarefa já reivindicada termina sua fase de persistência antes que o
    loop seja encerrado. Se o processo for morto além do prazo Docker, o lease
    durável existente continua sendo a proteção de recuperação.
    """
    if existing_event is not None:
        yield existing_event
        return

    stop_event = Event()
    # ``signal.signal`` só é permitido na thread principal. O desvio mantém
    # os executores utilizáveis em testes que os chamam por outra thread.
    if current_thread() is not main_thread():
        yield stop_event
        return

    installed: dict[int, object] = {}

    def request_shutdown(_signum, _frame) -> None:
        stop_event.set()

    for signal_name in ("SIGTERM", "SIGINT"):
        candidate = getattr(signal, signal_name, None)
        if candidate is None:
            continue
        try:
            installed[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, request_shutdown)
        except (OSError, RuntimeError, ValueError):
            # Plataformas restritivas podem não permitir registrar um handler;
            # nesse caso a proteção de lease continua válida.
            continue

    try:
        yield stop_event
    finally:
        for candidate, previous in installed.items():
            try:
                signal.signal(candidate, previous)
            except (OSError, RuntimeError, ValueError):
                pass
