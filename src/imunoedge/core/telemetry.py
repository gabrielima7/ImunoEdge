"""
Telemetria Resiliente com Autocura (Store-and-Forward).

Envia dados de telemetria para a nuvem com proteção via @retry
e CircuitBreaker do TaipanStack. Quando o circuito abre (nuvem
indisponível), armazena os dados localmente e tenta reenviar
quando o circuito voltar a fechar.
"""

from __future__ import annotations

import json
import logging
import os
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

# Diretório de buffer local para store-and-forward
DEFAULT_BUFFER_DIR = Path("/tmp/imunoedge_telemetry_buffer")  # noqa: S108  # nosec


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
    """Cliente de telemetria resiliente com circuit breaker e store-and-forward.

    Faz envio de dados para a nuvem protegido por @retry e CircuitBreaker.
    Quando o circuito abre, salva os dados em disco e tenta reenviar
    posteriormente via flush loop.

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
        buffer_dir: Path | str = DEFAULT_BUFFER_DIR,
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
            buffer_dir: Diretório para armazenamento local de fallback.
            flush_interval: Intervalo em segundos para tentar reenviar buffer.
            circuit_failure_threshold: Falhas antes de abrir o circuito.
            circuit_timeout: Segundos antes de tentar half-open.
            retry_max_attempts: Tentativas por envio.
            retry_initial_delay: Delay inicial do retry em segundos.
            send_fn: Função customizada de envio (para testes/DI).

        """
        self._device_id = device_id
        self._endpoint = endpoint
        self._buffer_dir = Path(buffer_dir)
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

        # Garante que o diretório de buffer existe
        self._buffer_dir.mkdir(parents=True, exist_ok=True)

    @property
    def circuit_state(self) -> CircuitState:
        """Retorna o estado atual do circuit breaker."""
        return self._circuit_breaker.state

    @property
    def buffered_count(self) -> int:
        """Retorna a quantidade de payloads armazenados localmente."""
        try:
            return len(list(self._buffer_dir.glob("*.json")))
        except OSError:
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
        # Em um cenário real, aqui teria:
        # response = httpx.post(self._endpoint, json=asdict(payload))
        # response.raise_for_status()
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
                # Circuit breaker protege a chamada
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

            except (CloudConnectionError, ConnectionError, TimeoutError, OSError) as e:
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
        """Envia dados de telemetria (ou armazena localmente se falhar).

        Este é o método principal. Tenta enviar via retry+circuit_breaker.
        Se o circuito estiver aberto, armazena localmente para reenvio.

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
            # Circuito aberto — armazena localmente
            self._store_locally(payload)
            self._metrics.increment("telemetry_circuit_open")
            logger.warning(
                "Circuito aberto — telemetria armazenada localmente: %s",
                payload.payload_id,
            )
            return False

        except Exception:
            # Retry exauriu — armazena localmente também
            self._store_locally(payload)
            self._metrics.increment("telemetry_send_failed")
            logger.exception(
                "Falha no envio de telemetria — armazenado localmente: %s",
                payload.payload_id,
            )
            return False

    def _store_locally(self, payload: TelemetryPayload) -> None:
        """Armazena payload em arquivo JSON local.

        Args:
            payload: Dados a armazenar.

        """
        try:
            self._enforce_buffer_limit()
            filepath = self._buffer_dir / f"{payload.payload_id}.json"
            filepath.write_text(
                json.dumps(asdict(payload), indent=2, default=str),
                encoding="utf-8",
            )
            self._metrics.increment("telemetry_buffered")
            logger.debug("Payload armazenado em %s", filepath)
        except OSError:
            logger.exception("Erro ao armazenar payload localmente")

    def _enforce_buffer_limit(self) -> None:
        """Enforce log rotation (FIFO) to keep buffer size under limit.

        If buffer exceeds 50MB, removes oldest .json files until size drops
        below 45MB (hysteresis to prevent constant deletion/writing).
        """
        max_size_mb = float(os.getenv("IMUNOEDGE_MAX_BUFFER_MB", "50"))
        cleanup_target_mb = max(0.0, max_size_mb - 5.0)  # Target 45MB

        max_size_bytes = int(max_size_mb * 1024 * 1024)
        target_size_bytes = int(cleanup_target_mb * 1024 * 1024)

        try:
            # 1. List all files and calculate total size
            files = list(self._buffer_dir.glob("*.json"))
            if not files:
                return

            # Stat once to avoid race conditions/multiple calls
            file_stats = []
            total_size = 0
            for p in files:
                try:
                    st = p.stat()
                    file_stats.append((p, st.st_size, st.st_mtime))
                    total_size += st.st_size
                except OSError:
                    # File might have been deleted
                    pass

            if total_size <= max_size_bytes:
                return

            # 2. Sort by mtime (oldest first)
            file_stats.sort(key=lambda x: x[2])

            bytes_to_free = total_size - target_size_bytes
            freed = 0
            deleted_count = 0

            # 3. Delete files until target reached
            for p, size, _ in file_stats:
                if freed >= bytes_to_free:
                    break

                try:
                    p.unlink()
                    freed += size
                    deleted_count += 1
                except OSError:
                    continue

            if deleted_count > 0:
                logger.warning(
                    "HARDENING: Rotação de buffer acionada. "
                    "Tamanho: %.2fMB > %.2fMB. "
                    "Removidos %d arquivos (%.2fMB). Nova ocupação: %.2fMB.",
                    total_size / (1024 * 1024),
                    max_size_mb,
                    deleted_count,
                    freed / (1024 * 1024),
                    (total_size - freed) / (1024 * 1024),
                )

        except Exception:
            logger.exception("Erro crítico durante rotação de buffer (Hardening)")

    def _flush_buffer(self) -> int:
        """Tenta reenviar todos os payloads armazenados.

        Returns:
            Quantidade de payloads reenviados com sucesso.

        """
        if self._circuit_breaker.state == CircuitState.OPEN:
            logger.debug("Flush: circuito ainda aberto, pulando")
            return 0

        flushed = 0
        try:
            buffer_files = sorted(self._buffer_dir.glob("*.json"))
        except OSError:
            return 0

        for filepath in buffer_files:
            try:
                raw = filepath.read_text(encoding="utf-8")
                data = json.loads(raw)
                payload = TelemetryPayload(**data)

                self._send_with_retry(payload)

                # Sucesso — remove o arquivo
                filepath.unlink(missing_ok=True)
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
                logger.exception("Erro ao reenviar %s", filepath.name)

        if flushed > 0:
            logger.info("Flush concluído: %d payloads reenviados", flushed)

        return flushed

    def _flush_loop(self) -> None:
        """Loop periódico de flush do buffer local."""
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
            "Telemetry client iniciado (device=%s, flush_interval=%.0fs)",
            self._device_id,
            self._flush_interval,
        )

    def stop(self) -> None:
        """Para a thread de flush."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=self._flush_interval + 2)
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
            "counters": {
                "sent_ok": self._metrics.get_counter("telemetry_sent_ok"),
                "send_failed": self._metrics.get_counter("telemetry_send_failed"),
                "circuit_open": self._metrics.get_counter("telemetry_circuit_open"),
                "buffered": self._metrics.get_counter("telemetry_buffered"),
                "flushed": self._metrics.get_counter("telemetry_flushed"),
            },
        }
