"""
Telemetria Resiliente com Autocura (Store-and-Forward via SQLite).

Envia dados de telemetria para a nuvem com proteção via @retry
e CircuitBreaker do TaipanStack. Quando o circuito abre (nuvem
indisponível), armazena os dados em banco SQLite local (WAL mode)
e tenta reenviar quando o circuito voltar a fechar.

O uso de SQLite reduz drasticamente o IOPS comparado a arquivos
JSON individuais, prolongando a vida útil de SD cards em hardware
embarcado (Raspberry Pi, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from taipanstack.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
)
from taipanstack.utils.metrics import MetricsCollector

logger = logging.getLogger("imunoedge.core.telemetry")


# Caminho padrão para o banco de telemetria (FHS com fallback)
def _default_db_path() -> Path:
    """Resolve path do buffer.db com fallback para ./data."""
    env = os.getenv("IMUNOEDGE_DATA_DIR")
    if env:
        return Path(env) / "buffer.db"
    fhs = Path("/var/lib/imunoedge")
    try:
        fhs.mkdir(parents=True, exist_ok=True)
        return fhs / "buffer.db"
    except PermissionError:
        return Path("data") / "buffer.db"


DEFAULT_DB_PATH = _default_db_path()

# Limite máximo de linhas no buffer (FIFO)
MAX_BUFFER_ROWS = int(os.getenv("IMUNOEDGE_MAX_BUFFER_ROWS", "10000"))

# Quantidade de payloads por ciclo de flush
FLUSH_BATCH_SIZE = 10


@dataclass(frozen=True)
class TelemetryPayload:
    """Payload de telemetria a ser enviado.

    Attributes:
        device_id: Identificador do dispositivo edge.
        timestamp: Momento da coleta em epoch seconds.
        data: Dados de telemetria (métricas, eventos, etc.).
        payload_id: ID único do payload para deduplicação.

    """

    device_id: str
    timestamp: float
    data: dict[str, Any]
    payload_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class CloudConnectionError(Exception):
    """Erro de conexão com a nuvem."""


class TelemetryClient:
    """Cliente de telemetria resiliente com circuit breaker e SQLite buffer.

    Faz envio de dados para a nuvem protegido por @retry e CircuitBreaker.
    Quando o circuito abre, salva os dados em banco SQLite local (WAL mode)
    e tenta reenviar posteriormente via flush loop.

    Example:
        >>> client = TelemetryClient(
        ...     device_id="edge-001",
        ...     endpoint="https://iot.example.com/telemetry",
        ... )
        >>> client.start()
        >>> client.send({"temperature": 42.3, "humidity": 65})

    """

    def __init__(
        self,
        *,
        device_id: str = "imunoedge-default",
        endpoint: str = "https://localhost/telemetry",
        db_path: Path | str = DEFAULT_DB_PATH,
        flush_interval: float = 30.0,
        circuit_failure_threshold: int = 3,
        circuit_timeout: float = 60.0,
        retry_max_attempts: int = 3,
        retry_initial_delay: float = 2.0,
        send_fn: Any | None = None,
    ) -> None:
        """Inicializa o cliente de telemetria.

        Args:
            device_id: Identificador do dispositivo.
            endpoint: URL do endpoint de telemetria na nuvem.
            db_path: Caminho do banco SQLite de buffer.
            flush_interval: Intervalo em segundos para reenviar buffer.
            circuit_failure_threshold: Falhas antes de abrir o circuito.
            circuit_timeout: Segundos antes de tentar half-open.
            retry_max_attempts: Tentativas por envio.
            retry_initial_delay: Delay inicial do retry em segundos.
            send_fn: Função customizada de envio (para testes/DI).

        """
        self._device_id = device_id
        self._endpoint = endpoint
        self._db_path = Path(db_path)
        self._flush_interval = flush_interval
        self._metrics = MetricsCollector()

        # Circuit Breaker protege contra nuvem indisponível
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            success_threshold=2,
            timeout=circuit_timeout,
            name=f"telemetry-{device_id}",
        )

        # Configurações de retry
        self._retry_max_attempts = retry_max_attempts
        self._retry_initial_delay = retry_initial_delay

        # Função de envio injetável (para testes / DI)
        self._send_fn = send_fn or self._default_send

        # Controle de threads
        self._flush_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

        # Inicializa o banco SQLite
        self._conn = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Inicializa o banco SQLite com WAL mode.

        Returns:
            Conexão SQLite configurada.

        """
        # Garante que o diretório pai existe
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.commit()
        logger.info(
            "SQLite buffer inicializado: %s (WAL mode)",
            self._db_path,
        )
        return conn

    @property
    def circuit_state(self) -> CircuitState:
        """Retorna o estado atual do circuit breaker."""
        return self._circuit_breaker.state

    @property
    def buffered_count(self) -> int:
        """Retorna a quantidade de payloads no buffer SQLite."""
        try:
            with self._lock:
                cursor = self._conn.execute("SELECT COUNT(*) FROM telemetry_queue")
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _default_send(self, payload: TelemetryPayload) -> bool:
        """Função padrão de envio (placeholder para HTTP real).

        Em produção, isso seria substituído por httpx/aiohttp/requests.
        Aqui simula envio com logging para demonstração.

        Args:
            payload: Dados a enviar.

        Returns:
            True se enviou com sucesso.

        Raises:
            CloudConnectionError: Se a conexão falhar.

        """
        logger.info(
            "📡 Enviando telemetria para %s: device=%s payload_id=%s",
            self._endpoint,
            payload.device_id,
            payload.payload_id,
        )
        return True

    def _send_with_retry(self, payload: TelemetryPayload) -> bool:
        """Envia com retry + circuit breaker.

        Args:
            payload: Dados a enviar.

        Returns:
            True se enviou com sucesso.

        """
        last_exc: Exception | None = None

        for attempt in range(1, self._retry_max_attempts + 1):
            try:
                if not self._circuit_breaker._should_attempt():
                    raise CircuitBreakerError(
                        f"Circuit {self._circuit_breaker.name} is open",
                        state=self._circuit_breaker.state,
                    )

                result: bool = self._send_fn(payload)
                self._circuit_breaker._record_success()
                return result

            except CircuitBreakerError:
                raise

            except (
                CloudConnectionError,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as e:
                self._circuit_breaker._record_failure(e)
                last_exc = e

                if attempt < self._retry_max_attempts:
                    delay = self._retry_initial_delay * (2 ** (attempt - 1))
                    logger.info(
                        "Tentativa %d/%d falhou: %s. Retry em %.1fs...",
                        attempt,
                        self._retry_max_attempts,
                        e,
                        delay,
                    )
                    time.sleep(delay)

        msg = f"Todas {self._retry_max_attempts} tentativas falharam"
        raise ConnectionError(msg) from last_exc

    def send(self, data: dict[str, Any]) -> bool:
        """Envia dados de telemetria (ou armazena em SQLite se falhar).

        Este é o método principal. Tenta enviar via retry+circuit_breaker.
        Se o circuito estiver aberto, armazena em SQLite para reenvio.

        Args:
            data: Dados de telemetria a enviar.

        Returns:
            True se enviou com sucesso, False se armazenou localmente.

        """
        payload = TelemetryPayload(
            device_id=self._device_id,
            timestamp=time.time(),
            data=data,
        )

        try:
            self._send_with_retry(payload)
            self._metrics.increment("telemetry_sent_ok")
            logger.debug("Telemetria enviada: %s", payload.payload_id)
            return True

        except CircuitBreakerError:
            self._store_locally(payload)
            self._metrics.increment("telemetry_circuit_open")
            logger.warning(
                "Circuito aberto — telemetria armazenada em SQLite: %s",
                payload.payload_id,
            )
            return False

        except Exception:
            self._store_locally(payload)
            self._metrics.increment("telemetry_send_failed")
            logger.exception(
                "Falha no envio — armazenado em SQLite: %s",
                payload.payload_id,
            )
            return False

    def _store_locally(self, payload: TelemetryPayload) -> None:
        """Armazena payload no banco SQLite local.

        Args:
            payload: Dados a armazenar.

        """
        try:
            payload_json = json.dumps(asdict(payload), default=str)
            with self._lock:
                self._conn.execute(
                    "INSERT INTO telemetry_queue "
                    "(payload_json, created_at) VALUES (?, ?)",
                    (payload_json, time.time()),
                )
                self._conn.commit()
            self._enforce_buffer_limit()
            self._metrics.increment("telemetry_buffered")
            logger.debug("Payload armazenado em SQLite: %s", payload.payload_id)
        except sqlite3.Error:
            logger.exception("Erro ao armazenar payload em SQLite")

    def _enforce_buffer_limit(self) -> None:
        """Mantém o buffer dentro do limite de linhas (FIFO)."""
        try:
            with self._lock:
                cursor = self._conn.execute("SELECT COUNT(*) FROM telemetry_queue")
                count = cursor.fetchone()[0]
                if count > MAX_BUFFER_ROWS:
                    excess = count - MAX_BUFFER_ROWS
                    self._conn.execute(
                        "DELETE FROM telemetry_queue WHERE id IN "
                        "(SELECT id FROM telemetry_queue "
                        "ORDER BY created_at ASC LIMIT ?)",
                        (excess,),
                    )
                    self._conn.commit()
                    logger.warning(
                        "Buffer FIFO: removidos %d payloads antigos (limite: %d)",
                        excess,
                        MAX_BUFFER_ROWS,
                    )
        except sqlite3.Error:
            logger.exception("Erro na rotação do buffer SQLite")

    def _flush_buffer(self) -> int:
        """Tenta reenviar payloads armazenados no SQLite.

        Returns:
            Quantidade de payloads reenviados com sucesso.

        """
        if self._circuit_breaker.state == CircuitState.OPEN:
            logger.debug("Flush: circuito ainda aberto, pulando")
            return 0

        flushed = 0
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT id, payload_json FROM telemetry_queue "
                    "ORDER BY created_at ASC LIMIT ?",
                    (FLUSH_BATCH_SIZE,),
                )
                rows = cursor.fetchall()
        except sqlite3.Error:
            logger.exception("Erro ao ler buffer para flush")
            return 0

        for row_id, payload_json in rows:
            try:
                data = json.loads(payload_json)
                payload = TelemetryPayload(**data)

                self._send_with_retry(payload)

                # Sucesso — remove do banco
                with self._lock:
                    self._conn.execute(
                        "DELETE FROM telemetry_queue WHERE id = ?",
                        (row_id,),
                    )
                    self._conn.commit()
                flushed += 1
                self._metrics.increment("telemetry_flushed")
                logger.info(
                    "Payload reenviado do buffer: %s",
                    payload.payload_id,
                )

            except CircuitBreakerError:
                logger.debug("Circuito abriu durante flush — interrompendo")
                break

            except Exception:
                logger.exception("Erro ao reenviar payload id=%d", row_id)

        if flushed > 0:
            logger.info("Flush concluído: %d payloads reenviados", flushed)

        return flushed

    def _flush_loop(self) -> None:
        """Loop periódico de flush do buffer SQLite."""
        while self._running:
            time.sleep(self._flush_interval)

            buffered = self.buffered_count
            if buffered > 0:
                logger.info(
                    "Flush loop: %d payloads no buffer, tentando reenviar...",
                    buffered,
                )
                self._flush_buffer()

    def start(self) -> None:
        """Inicia a thread de flush periódico."""
        if self._running:
            logger.warning("Flush loop já está rodando")
            return

        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="imunoedge-telemetry-flush",
        )
        self._flush_thread.start()
        logger.info(
            "Telemetry client iniciado (device=%s, flush_interval=%.0fs, db=%s)",
            self._device_id,
            self._flush_interval,
            self._db_path,
        )

    def stop(self) -> None:
        """Para a thread de flush e fecha o banco SQLite."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=self._flush_interval + 2)

        # Fecha conexão SQLite
        try:
            self._conn.close()
            logger.info("SQLite buffer fechado: %s", self._db_path)
        except sqlite3.Error:
            logger.exception("Erro ao fechar SQLite")

        logger.info("Telemetry client parado")

    def get_stats(self) -> dict[str, Any]:
        """Retorna estatísticas de telemetria.

        Returns:
            Dicionário com contadores e estado.

        """
        return {
            "device_id": self._device_id,
            "endpoint": self._endpoint,
            "circuit_state": self._circuit_breaker.state.value,
            "buffered_payloads": self.buffered_count,
            "db_path": str(self._db_path),
            "counters": {
                "sent_ok": self._metrics.get_counter("telemetry_sent_ok"),
                "send_failed": self._metrics.get_counter("telemetry_send_failed"),
                "circuit_open": self._metrics.get_counter("telemetry_circuit_open"),
                "buffered": self._metrics.get_counter("telemetry_buffered"),
                "flushed": self._metrics.get_counter("telemetry_flushed"),
            },
        }
