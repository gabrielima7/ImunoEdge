"""
ImunoEdge — Ponto de Entrada Principal.

Inicializa todos os subsistemas (Orchestrator, Health Monitor,
Telemetry Client), conecta os callbacks de autopreservação,
e gerencia o ciclo de vida com graceful shutdown via sinais UNIX.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from types import FrameType

from imunoedge.core.health import HealthMonitor, HealthStatus
from imunoedge.core.orchestrator import ProcessOrchestrator
from imunoedge.core.telemetry import TelemetryClient
from taipanstack.utils.logging import setup_logging
from taipanstack.utils.metrics import MetricsCollector

logger = logging.getLogger("imunoedge.main")


# ─── Caminhos FHS (com fallback para dev/CI) ────────────────
def _resolve_data_dir() -> Path:
    """Resolve o diretório de dados.

    Usa env var se definida, senão tenta FHS,
    com fallback para './data' se sem permissão.
    """
    env = os.getenv("IMUNOEDGE_DATA_DIR")
    if env:
        return Path(env)
    fhs = Path("/var/lib/imunoedge")
    try:
        fhs.mkdir(parents=True, exist_ok=True)
        return fhs
    except PermissionError:
        return Path("data")


def _resolve_log_dir() -> Path:
    """Resolve o diretório de logs."""
    env = os.getenv("IMUNOEDGE_LOG_DIR")
    if env:
        return Path(env)
    fhs = Path("/var/log/imunoedge")
    try:
        fhs.mkdir(parents=True, exist_ok=True)
        return fhs
    except PermissionError:
        return Path("logs")


FHS_DATA_DIR = _resolve_data_dir()
FHS_LOG_DIR = _resolve_log_dir()

# ─── Banner ──────────────────────────────────────────────────────
BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   ██╗███╗   ███╗██╗   ██╗███╗   ██╗ ██████╗             ║
║   ██║████╗ ████║██║   ██║████╗  ██║██╔═══██╗            ║
║   ██║██╔████╔██║██║   ██║██╔██╗ ██║██║   ██║            ║
║   ██║██║╚██╔╝██║██║   ██║██║╚██╗██║██║   ██║            ║
║   ██║██║ ╚═╝ ██║╚██████╔╝██║ ╚████║╚██████╔╝            ║
║   ╚═╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝            ║
║                     ███████╗██████╗  ██████╗ ███████╗    ║
║                     ██╔════╝██╔══██╗██╔════╝ ██╔════╝    ║
║                     █████╗  ██║  ██║██║  ███╗█████╗      ║
║                     ██╔══╝  ██║  ██║██║   ██║██╔══╝      ║
║                     ███████╗██████╔╝╚██████╔╝███████╗    ║
║                     ╚══════╝╚═════╝  ╚═════╝ ╚══════╝    ║
║                                                          ║
║   ImunoEdge v0.1.0 — IoT Runtime com Autocura            ║
║   🛡️  Protection Active                                  ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
"""


# ─── Configuração via Env ────────────────────────────────────────
def _env(key: str, default: str) -> str:
    """Lê variável de ambiente com fallback.

    Args:
        key: Nome da variável.
        default: Valor padrão se não definida.

    Returns:
        Valor da variável ou default.

    """
    return os.getenv(key, default)


def _env_float(key: str, default: float) -> float:
    """Lê variável de ambiente como float.

    Args:
        key: Nome da variável.
        default: Valor padrão.

    Returns:
        Valor float da variável.

    """
    return float(os.getenv(key, str(default)))


def _env_int(key: str, default: int) -> int:
    """Lê variável de ambiente como int.

    Args:
        key: Nome da variável.
        default: Valor padrão.

    Returns:
        Valor int da variável.

    """
    return int(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    """Lê variável de ambiente como bool.

    Args:
        key: Nome da variável.
        default: Valor padrão.

    Returns:
        True se o valor for '1', 'true' ou 'yes' (case-insensitive).

    """
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


# ─── Classe Principal ───────────────────────────────────────────
class ImunoEdgeRuntime:
    """Runtime principal do ImunoEdge.

    Orquestra todos os subsistemas e gerencia o ciclo de vida
    da aplicação, incluindo graceful shutdown.
    """

    def __init__(self) -> None:
        """Inicializa o runtime com configurações do ambiente."""
        # Configurações
        self._device_id = _env("IMUNOEDGE_DEVICE_ID", "edge-001")
        self._log_level = _env("IMUNOEDGE_LOG_LEVEL", "INFO")

        # Setup logging
        setup_logging(
            level=self._log_level,  # type: ignore[arg-type]
            format_type="detailed",
        )

        # Métricas globais
        self._metrics = MetricsCollector()

        # Subsistemas
        self._orchestrator = ProcessOrchestrator(
            watchdog_interval=_env_float("IMUNOEDGE_WATCHDOG_INTERVAL", 5.0),
            cwd=Path.cwd(),
        )

        self._health_monitor = HealthMonitor(
            interval=_env_float("IMUNOEDGE_HEALTH_INTERVAL", 10.0),
            temp_threshold=_env_float("IMUNOEDGE_TEMP_THRESHOLD", 75.0),
            cpu_threshold=_env_float("IMUNOEDGE_CPU_THRESHOLD", 95.0),
            memory_threshold=_env_float("IMUNOEDGE_MEMORY_THRESHOLD", 90.0),
        )

        # Garante que o diretório de dados existe
        FHS_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self._telemetry = TelemetryClient(
            device_id=self._device_id,
            endpoint=_env(
                "IMUNOEDGE_TELEMETRY_ENDPOINT",
                "https://localhost/telemetry",
            ),
            db_path=FHS_DATA_DIR / "buffer.db",
            flush_interval=_env_float("IMUNOEDGE_FLUSH_INTERVAL", 30.0),
            circuit_failure_threshold=_env_int(
                "IMUNOEDGE_CIRCUIT_FAILURE_THRESHOLD",
                3,
            ),
            circuit_timeout=_env_float("IMUNOEDGE_CIRCUIT_TIMEOUT", 60.0),
            retry_max_attempts=_env_int(
                "IMUNOEDGE_RETRY_MAX_ATTEMPTS",
                3,
            ),
            retry_initial_delay=_env_float("IMUNOEDGE_RETRY_INITIAL_DELAY", 2.0),
        )

        # Controle de shutdown
        self._shutdown_event = threading.Event()
        self._setup_signal_handlers()

        # Conecta callbacks de autopreservação
        self._health_monitor.on_overheat = self._on_overheat
        self._health_monitor.on_recover = self._on_recover

        # Validação de configuração
        self._validate_config()

    def _validate_config(self) -> None:
        """Valida configurações críticas e emite alertas."""
        endpoint = _env(
            "IMUNOEDGE_TELEMETRY_ENDPOINT",
            "https://localhost/telemetry",
        )
        if "localhost" in endpoint or "127.0.0.1" in endpoint:
            logger.warning(
                "⚠️  ALERTA: Rodando com endpoint de "
                "telemetria local (%s). "
                "Isso não funcionará em produção real!",
                endpoint,
            )

    def _setup_signal_handlers(self) -> None:
        """Registra handlers para SIGINT e SIGTERM."""
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)

    def _handle_shutdown_signal(
        self,
        signum: int,
        _frame: FrameType | None,
    ) -> None:
        """Handle shutdown signal gracefully.

        Args:
            signum: Número do sinal recebido.
            _frame: Frame da stack (não utilizado).

        """
        sig_name = signal.Signals(signum).name
        logger.warning(
            "⚠️ Sinal %s recebido — iniciando graceful shutdown...",
            sig_name,
        )
        self._shutdown_event.set()

    def _on_overheat(self, status: HealthStatus) -> None:
        """Pause non-essential workers on overheat.

        Args:
            status: Snapshot de saúde com a temperatura atual.

        """
        non_essential = self._orchestrator.get_non_essential_workers()
        for name in non_essential:
            self._orchestrator.pause_worker(name)
            logger.warning("🔥 Worker '%s' pausado por autopreservação", name)

        # Envia alerta de telemetria
        self._telemetry.send(
            {
                "event": "overheat_protection",
                "temperature": status.temperature_celsius,
                "paused_workers": non_essential,
            }
        )

    def _on_recover(self, status: HealthStatus) -> None:
        """Resume paused workers after temperature recovery.

        Args:
            status: Snapshot de saúde com a temperatura normalizada.

        """
        for name, worker in self._orchestrator.workers.items():
            if worker.state.value == "paused":
                self._orchestrator.resume_worker(name)
                logger.info("✅ Worker '%s' retomado após recuperação", name)

        self._telemetry.send(
            {
                "event": "temperature_recovered",
                "temperature": status.temperature_celsius,
            }
        )

    def _register_default_workers(self) -> None:
        """Registra workers padrão configurados via env vars."""
        workers_config = _env("IMUNOEDGE_WORKERS", "")

        if not workers_config:
            # Workers padrão de demonstração
            sensor_script = str(Path(__file__).parent / "workers" / "sensor_reader.py")
            self._orchestrator.register_worker(
                "sensor_reader",
                ["python3", sensor_script],
                essential=False,
                max_restarts=_env_int("IMUNOEDGE_MAX_RESTARTS", 10),
                enable_heartbeat=True,
            )
            return

        # Formato: "name1:cmd1:essential,name2:cmd2:essential"
        for entry in workers_config.split(","):
            parts = entry.strip().split(":")
            min_parts = 2
            if len(parts) >= min_parts:
                name = parts[0].strip()
                cmd = parts[1].strip().split()
                essential = (
                    len(parts) > min_parts and parts[2].strip().lower() == "true"
                )
                self._orchestrator.register_worker(
                    name,
                    cmd,
                    essential=essential,
                    max_restarts=_env_int("IMUNOEDGE_MAX_RESTARTS", 10),
                )

    def _telemetry_heartbeat_loop(self) -> None:
        """Envia heartbeat periódico com métricas de saúde."""
        interval = _env_float("IMUNOEDGE_HEARTBEAT_INTERVAL", 60.0)

        while not self._shutdown_event.is_set():
            status = self._health_monitor.last_status
            if status is not None:
                self._telemetry.send(
                    {
                        "event": "heartbeat",
                        "device_id": self._device_id,
                        "cpu_percent": status.cpu_percent,
                        "memory_percent": status.memory_percent,
                        "temperature_celsius": status.temperature_celsius,
                        "disk_usage_percent": status.disk_usage_percent,
                        "workers": self._orchestrator.status(),
                        "telemetry_stats": self._telemetry.get_stats(),
                    }
                )

            self._shutdown_event.wait(timeout=interval)

    def run(self) -> None:
        """Executa o runtime ImunoEdge.

        Este método bloqueia até receber SIGINT ou SIGTERM.
        """
        # Banner
        for line in BANNER.strip().splitlines():
            logger.info(line)

        logger.info("Device ID: %s", self._device_id)
        logger.info(
            "Telemetry Endpoint: %s",
            _env("IMUNOEDGE_TELEMETRY_ENDPOINT", "https://localhost/telemetry"),
        )
        logger.info(
            "Temp Threshold: %.1f°C", _env_float("IMUNOEDGE_TEMP_THRESHOLD", 75.0)
        )

        # 1. Registra workers
        self._register_default_workers()

        # 2. Inicia subsistemas
        logger.info("Iniciando subsistemas...")
        self._health_monitor.start()
        self._telemetry.start()
        results = self._orchestrator.start_all()

        for name, ok in results.items():
            if ok:
                logger.info("  ✅ Worker '%s' iniciado", name)
            else:
                logger.error("  ❌ Worker '%s' falhou ao iniciar", name)

        # 3. Inicia heartbeat em thread separada
        heartbeat_thread = threading.Thread(
            target=self._telemetry_heartbeat_loop,
            daemon=True,
            name="imunoedge-heartbeat",
        )
        heartbeat_thread.start()

        logger.info("🛡️  ImunoEdge ativo — aguardando sinal de parada...")

        # 4. Aguarda sinal de shutdown
        try:
            self._shutdown_event.wait()
        except KeyboardInterrupt:
            self._shutdown_event.set()

        # 5. Graceful Shutdown
        self._graceful_shutdown()

    def _graceful_shutdown(self) -> None:
        """Executa o desligamento gracioso de todos os subsistemas."""
        logger.info("═══ GRACEFUL SHUTDOWN INICIADO ═══")

        # Para workers primeiro (podem estar escrevendo dados)
        logger.info("Parando workers...")
        self._orchestrator.stop_all()

        # Para health monitor
        logger.info("Parando health monitor...")
        self._health_monitor.stop()

        # Flush final de telemetria
        logger.info("Flush final de telemetria...")
        self._telemetry.send(
            {
                "event": "shutdown",
                "device_id": self._device_id,
                "reason": "graceful_shutdown",
            }
        )

        # Para telemetry client
        logger.info("Parando telemetry client...")
        self._telemetry.stop()

        # Relatório final
        all_metrics = self._metrics.get_all_metrics()
        logger.info("Métricas finais: %s", all_metrics)

        logger.info("═══ GRACEFUL SHUTDOWN CONCLUÍDO ═══")
        logger.info("🛡️  ImunoEdge desativado com segurança.")


def main() -> None:
    """Entry point do ImunoEdge."""
    # Carrega .env se python-dotenv estiver disponível
    try:
        from dotenv import load_dotenv

        env_file = Path.cwd() / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            logging.getLogger("imunoedge.main").info(
                "Configuração carregada de %s",
                env_file,
            )
    except ImportError:
        pass  # python-dotenv é opcional

    runtime = ImunoEdgeRuntime()
    runtime.run()


if __name__ == "__main__":
    main()
