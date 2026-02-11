"""
ImunoEdge — SDK para Desenvolvedores de Workers.

Fornece a classe ``EdgeWorker`` que abstrai toda a mecânica de
heartbeat e sinais de parada, permitindo que o desenvolvedor
foque exclusivamente na lógica de negócio do seu worker.

Exemplo de uso::

    from imunoedge.sdk import EdgeWorker

    worker = EdgeWorker("meu_sensor")

    while worker.should_run():
        # ... lógica de leitura do sensor ...
        worker.heartbeat()
        time.sleep(2)
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("imunoedge.sdk")


class EdgeWorker:
    """Abstração para workers do ImunoEdge.

    Encapsula heartbeat e controle de ciclo de vida, eliminando
    a necessidade do desenvolvedor saber *onde* ou *como* sinalizar
    liveness para o Orchestrator.

    Attributes:
        name: Nome identificador do worker.

    Example:
        >>> worker = EdgeWorker("sensor_temp")
        >>> while worker.should_run():
        ...     # lógica do sensor
        ...     worker.heartbeat()

    """

    def __init__(self, name: str) -> None:
        """Inicializa o EdgeWorker.

        Args:
            name: Nome do worker (deve ser único por instância).

        """
        self.name = name
        self._stopped = False

        # Heartbeat: usa env var se definida, senão gera
        # caminho volátil em /tmp ou /run (correto para estado
        # efêmero que não precisa sobreviver a reboots).
        env_path = os.getenv("IMUNOEDGE_HEARTBEAT_FILE")
        if env_path:
            self._heartbeat_path = Path(env_path)
        else:
            tmp_dir = Path(tempfile.gettempdir())
            self._heartbeat_path = tmp_dir / f"imunoedge_{name}.beat"

        # Sinal de parada: arquivo criado pelo orchestrator
        stop_env = os.getenv("IMUNOEDGE_STOP_SIGNAL")
        self._stop_path = Path(stop_env) if stop_env else None

        logger.debug(
            "EdgeWorker '%s' inicializado (heartbeat=%s, stop=%s)",
            name,
            self._heartbeat_path,
            self._stop_path,
        )

    def heartbeat(self) -> None:
        """Sinaliza liveness para o Orchestrator.

        Atualiza o mtime do arquivo de heartbeat. Operação
        silenciosa em caso de erro (não deve derrubar o worker).
        """
        with contextlib.suppress(OSError):
            self._heartbeat_path.touch()

    def should_run(self) -> bool:
        """Verifica se o worker deve continuar executando.

        Returns:
            False se um sinal de parada foi recebido ou
            se ``stop()`` foi chamado manualmente.

        """
        if self._stopped:
            return False

        if self._stop_path and self._stop_path.exists():
            logger.info(
                "EdgeWorker '%s': sinal de parada detectado",
                self.name,
            )
            self._stopped = True
            return False

        return True

    def stop(self) -> None:
        """Para o worker programaticamente."""
        self._stopped = True
        logger.info("EdgeWorker '%s' parado via stop()", self.name)
