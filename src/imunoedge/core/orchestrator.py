"""
Orquestrador de Processos com Watchdog.

Gerencia processos workers (leitores de sensores, etc.) e monitora
se estão vivos. Reinicia automaticamente qualquer worker que morrer.
Utiliza `run_safe_command` do TaipanStack para execução segura.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from taipanstack.utils.metrics import MetricsCollector
from taipanstack.utils.subprocess import DEFAULT_ALLOWED_COMMANDS

logger = logging.getLogger("imunoedge.core.orchestrator")


class WorkerState(Enum):
    """Estados possíveis de um worker."""

    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    RESTARTING = "restarting"
    FAILED = "failed"


@dataclass
class WorkerProcess:
    """Representação de um processo worker gerenciado.

    Attributes:
        name: Identificador único do worker.
        command: Comando para iniciar o worker.
        pid: PID do processo atual (None se não estiver rodando).
        state: Estado atual do worker.
        restart_count: Quantas vezes foi reiniciado automaticamente.
        essential: Se True, nunca será pausado por autopreservação.
        max_restarts: Limite de restarts antes de marcar como FAILED.
        process: Referência ao objeto Popen do processo.

    """

    name: str
    command: list[str]
    pid: int | None = None
    state: WorkerState = WorkerState.STOPPED
    restart_count: int = 0
    essential: bool = False
    max_restarts: int = 10
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    enable_heartbeat: bool = False
    heartbeat_file: Path | None = None


# Whitelist expandida para incluir python3 (workers são scripts Python)
IMUNOEDGE_ALLOWED_COMMANDS: frozenset[str] = DEFAULT_ALLOWED_COMMANDS | frozenset(
    {"python3", "python", "bash", "sh"}
)


class ProcessOrchestrator:
    """Orquestrador de processos workers com watchdog automático.

    Inicia, monitora e reinicia processos workers. Permite pausar
    workers não essenciais quando solicitado pelo módulo de saúde.

    Example:
        >>> orch = ProcessOrchestrator()
        >>> orch.register_worker("sensor_temp", ["python3", "sensor.py"])
        >>> orch.start_all()

    """

    def __init__(
        self,
        *,
        watchdog_interval: float = 5.0,
        cwd: Path | str | None = None,
    ) -> None:
        """Inicializa o orquestrador.

        Args:
            watchdog_interval: Intervalo em segundos entre checks do watchdog.
            cwd: Diretório de trabalho para execução dos workers.

        """
        self._workers: dict[str, WorkerProcess] = {}
        self._watchdog_interval = watchdog_interval
        self._watchdog_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._cwd = Path(cwd) if cwd else None
        self._metrics = MetricsCollector()

    @property
    def workers(self) -> dict[str, WorkerProcess]:
        """Retorna cópia do dicionário de workers."""
        with self._lock:
            return dict(self._workers)

    def register_worker(
        self,
        name: str,
        command: list[str],
        *,
        essential: bool = False,
        max_restarts: int = 10,
        enable_heartbeat: bool = False,
    ) -> None:
        """Registra um novo worker para ser gerenciado.

        Args:
            name: Nome único do worker.
            command: Comando e argumentos para execução.
            essential: Se True, não pode ser pausado por autopreservação.
            max_restarts: Limite de restarts automáticos.

        Raises:
            ValueError: Se já existir um worker com o mesmo nome.

        """
        with self._lock:
            if name in self._workers:
                msg = f"Worker '{name}' já está registrado"
                raise ValueError(msg)

            self._workers[name] = WorkerProcess(
                name=name,
                command=command,
                essential=essential,
                max_restarts=max_restarts,
                enable_heartbeat=enable_heartbeat,
            )
            logger.info(
                "Worker registrado: %s → %s (Heartbeat: %s)",
                name,
                " ".join(command),
                enable_heartbeat,
            )

    def _start_worker(self, worker: WorkerProcess) -> bool:
        """Inicia um worker individual usando subprocess direto.

        Usa subprocess.Popen para manter o processo rodando em
        background (run_safe_command é síncrono e bloqueante).

        Args:
            worker: O worker a ser iniciado.

        Returns:
            True se iniciou com sucesso, False caso contrário.

        """
        try:
            # SECURITY: shell=False prevents shell injection attacks
            # Arguments are passed as a list, ensuring they are not interpreted by shell
            if not isinstance(worker.command, list):
                raise ValueError(
                    "Command must be a list of strings (security requirement)"
                )

            env = os.environ.copy()

            if worker.enable_heartbeat:
                beat_path = f"/tmp/imunoedge_{worker.name}.beat"  # noqa: S108
                worker.heartbeat_file = Path(beat_path)

                # Cleanup old heartbeat file to ensure fresh start
                with contextlib.suppress(OSError):
                    worker.heartbeat_file.unlink()

                # Initialize heartbeat file
                worker.heartbeat_file.touch()
                env["IMUNOEDGE_HEARTBEAT_FILE"] = str(worker.heartbeat_file)

            proc = subprocess.Popen(
                worker.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._cwd) if self._cwd else None,
                shell=False,  # SECURITY: Explicitly disable shell
                env=env,
            )
            worker.process = proc
            worker.pid = proc.pid
            worker.state = WorkerState.RUNNING

            self._metrics.gauge(
                "workers_active",
                float(self._count_active_workers()),
            )
            logger.info(
                "Worker '%s' iniciado com PID %d",
                worker.name,
                worker.pid,
            )
            return True

        except (OSError, subprocess.SubprocessError) as e:
            worker.state = WorkerState.FAILED
            logger.exception("Falha ao iniciar worker '%s': %s", worker.name, e)
            return False

    def _count_active_workers(self) -> int:
        """Conta workers em estado RUNNING."""
        return sum(1 for w in self._workers.values() if w.state == WorkerState.RUNNING)

    def _is_alive(self, worker: WorkerProcess) -> bool:
        """Verifica se o processo do worker ainda está vivo.

        Args:
            worker: Worker a verificar.

        Returns:
            True se o processo está rodando.

        """
        if worker.process is None or worker.pid is None:
            return False

        # Usa poll() do Popen — retorna None se ainda está rodando
        # Usa poll() do Popen — retorna None se ainda está rodando
        is_running = worker.process.poll() is None

        if not is_running:
            return False

        # Zombie Check (Heartbeat)
        if worker.enable_heartbeat and worker.heartbeat_file:
            try:
                last_beat = worker.heartbeat_file.stat().st_mtime
                if time.time() - last_beat > 30.0:
                    logger.error(
                        "ZOMBIE DETECTED: Worker '%s' (PID %s) não responde há >30s. "
                        "Matando...",
                        worker.name,
                        worker.pid,
                    )
                    self._stop_worker(worker)
                    return False
            except OSError:
                # Se o arquivo sumiu, algo está errado, mas assumimos vivo
                # ou morto na próxima iteração
                pass

        return True

    def start_all(self) -> dict[str, bool]:
        """Inicia todos os workers registrados e o watchdog.

        Returns:
            Dicionário {nome: sucesso} para cada worker.

        """
        results: dict[str, bool] = {}

        with self._lock:
            for name, worker in self._workers.items():
                if worker.state == WorkerState.STOPPED:
                    results[name] = self._start_worker(worker)

        # Inicia o watchdog thread
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="imunoedge-watchdog",
        )
        self._watchdog_thread.start()
        logger.info("Watchdog iniciado (intervalo: %.1fs)", self._watchdog_interval)

        return results

    def stop_all(self) -> None:
        """Para todos os workers e o watchdog."""
        self._running = False

        with self._lock:
            for worker in self._workers.values():
                self._stop_worker(worker)

        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=self._watchdog_interval + 2)

        logger.info("Todos os workers parados")

    def _stop_worker(self, worker: WorkerProcess) -> None:
        """Para um worker individual com SIGTERM, depois SIGKILL.

        Args:
            worker: Worker a parar.

        """
        if worker.process is not None and worker.process.poll() is None:
            try:
                worker.process.terminate()
                worker.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                worker.process.wait(timeout=2)
            except OSError:
                pass

        worker.state = WorkerState.STOPPED
        worker.pid = None
        worker.process = None

        # Cleanup heartbeat file
        if worker.heartbeat_file and worker.heartbeat_file.exists():
            with contextlib.suppress(OSError):
                worker.heartbeat_file.unlink()

    def pause_worker(self, name: str) -> bool:
        """Pausa um worker não essencial (envia SIGSTOP).

        Args:
            name: Nome do worker a pausar.

        Returns:
            True se pausou com sucesso.

        """
        with self._lock:
            worker = self._workers.get(name)
            if worker is None:
                logger.warning("Worker '%s' não encontrado", name)
                return False

            if worker.essential:
                logger.warning(
                    "Worker '%s' é essencial e não pode ser pausado",
                    name,
                )
                return False

            if worker.state != WorkerState.RUNNING or worker.pid is None:
                return False

            try:
                os.kill(worker.pid, signal.SIGSTOP)
                worker.state = WorkerState.PAUSED
                self._metrics.gauge(
                    "workers_active",
                    float(self._count_active_workers()),
                )
                logger.info("Worker '%s' pausado (PID %d)", name, worker.pid)
                return True
            except OSError as e:
                logger.warning("Erro ao pausar '%s': %s", name, e)
                return False

    def resume_worker(self, name: str) -> bool:
        """Retoma um worker pausado (envia SIGCONT).

        Args:
            name: Nome do worker a retomar.

        Returns:
            True se retomou com sucesso.

        """
        with self._lock:
            worker = self._workers.get(name)
            if worker is None or worker.state != WorkerState.PAUSED:
                return False

            if worker.pid is None:
                return False

            try:
                os.kill(worker.pid, signal.SIGCONT)
                worker.state = WorkerState.RUNNING
                self._metrics.gauge(
                    "workers_active",
                    float(self._count_active_workers()),
                )
                logger.info("Worker '%s' retomado (PID %d)", name, worker.pid)
                return True
            except OSError as e:
                logger.warning("Erro ao retomar '%s': %s", name, e)
                return False

    def get_non_essential_workers(self) -> list[str]:
        """Retorna nomes dos workers não essenciais que estão rodando.

        Returns:
            Lista de nomes de workers não essenciais em RUNNING.

        """
        with self._lock:
            return [
                name
                for name, w in self._workers.items()
                if not w.essential and w.state == WorkerState.RUNNING
            ]

    def _watchdog_loop(self) -> None:
        """Loop do watchdog: verifica e reinicia workers mortos."""
        while self._running:
            time.sleep(self._watchdog_interval)

            with self._lock:
                for worker in self._workers.values():
                    if worker.state != WorkerState.RUNNING:
                        continue

                    if not self._is_alive(worker):
                        logger.warning(
                            "Worker '%s' (PID %s) morreu! Restart %d/%d",
                            worker.name,
                            worker.pid,
                            worker.restart_count + 1,
                            worker.max_restarts,
                        )

                        worker.restart_count += 1
                        self._metrics.increment("worker_restarts")

                        if worker.restart_count >= worker.max_restarts:
                            worker.state = WorkerState.FAILED
                            logger.error(
                                "Worker '%s' atingiu limite de restarts (%d). "
                                "Marcado como FAILED.",
                                worker.name,
                                worker.max_restarts,
                            )
                            continue

                        worker.state = WorkerState.RESTARTING
                        self._start_worker(worker)

    def status(self) -> dict[str, Any]:
        """Snapshot do estado de todos os workers.

        Returns:
            Dicionário com estado de cada worker.

        """
        with self._lock:
            return {
                name: {
                    "state": w.state.value,
                    "pid": w.pid,
                    "restart_count": w.restart_count,
                    "essential": w.essential,
                }
                for name, w in self._workers.items()
            }
