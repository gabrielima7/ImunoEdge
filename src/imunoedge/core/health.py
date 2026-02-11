"""
Monitoramento de Saúde do Dispositivo com Autopreservação.

Coleta métricas do sistema (CPU, RAM, temperatura) em loop e
aciona lógica de autopreservação quando limites são ultrapassados.
Utiliza `MetricsCollector` do TaipanStack para registro de métricas.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psutil

from taipanstack.utils.metrics import MetricsCollector

logger = logging.getLogger("imunoedge.core.health")


# Sensores conhecidos, em ordem de prioridade de leitura.
PREFERRED_SENSORS: tuple[str, ...] = (
    "cpu_thermal",
    "thermal_zone0",
    "coretemp",
    "k10temp",
)


@dataclass(frozen=True)
class HealthStatus:
    """Snapshot do estado de saúde do sistema.

    Attributes:
        cpu_percent: Uso de CPU em percentual.
        memory_percent: Uso de memória RAM em percentual.
        temperature_celsius: Temperatura do CPU em °C (0.0 se indisponível).
        is_overheating: Se True, a temperatura ultrapassou o threshold.
        disk_usage_percent: Uso do disco principal em percentual.
        timestamp: Timestamp do snapshot em epoch seconds.

    """

    cpu_percent: float
    memory_percent: float
    temperature_celsius: float
    is_overheating: bool
    disk_usage_percent: float
    timestamp: float


# Callbacks type alias:
# on_overheat recebe a lista de workers não essenciais para pausar
OnOverheatCallback = Callable[[HealthStatus], None]
OnRecoverCallback = Callable[[HealthStatus], None]


class HealthMonitor:
    """Monitor de saúde do dispositivo edge.

    Coleta métricas do sistema em intervalo configurável e aciona
    callbacks de autopreservação quando a temperatura ultrapassa
    o threshold definido.

    Example:
        >>> monitor = HealthMonitor(temp_threshold=75.0)
        >>> monitor.on_overheat = lambda status: print("QUENTE!", status)
        >>> monitor.start()

    """

    def __init__(
        self,
        *,
        interval: float = 5.0,
        temp_threshold: float = 75.0,
        cpu_threshold: float = 95.0,
        memory_threshold: float = 90.0,
    ) -> None:
        """Inicializa o monitor de saúde.

        Args:
            interval: Intervalo entre coletas em segundos.
            temp_threshold: Temperatura (°C) que aciona autopreservação.
            cpu_threshold: CPU (%) que gera alerta.
            memory_threshold: RAM (%) que gera alerta.

        """
        self._interval = interval
        self._temp_threshold = temp_threshold
        self._cpu_threshold = cpu_threshold
        self._memory_threshold = memory_threshold

        self._running = False
        self._thread: threading.Thread | None = None
        self._metrics = MetricsCollector()
        self._lock = threading.Lock()

        # Estado de overheating (para detectar transições)
        self._is_overheating = False
        self._last_status: HealthStatus | None = None

        # Flag para logar aviso de sensor ausente apenas uma vez
        self._temp_warning_logged = False

        # Callbacks configuráveis externamente
        self.on_overheat: OnOverheatCallback | None = None
        self.on_recover: OnRecoverCallback | None = None

    @property
    def last_status(self) -> HealthStatus | None:
        """Retorna o último snapshot coletado."""
        with self._lock:
            return self._last_status

    @property
    def is_overheating(self) -> bool:
        """Retorna se o sistema está em estado de superaquecimento."""
        with self._lock:
            return self._is_overheating

    def _get_cpu_temperature(self) -> float:
        """Lê a temperatura da CPU de forma resiliente.

        Tenta sensores conhecidos em ordem de prioridade.
        Se nenhum for encontrado (VM, WSL, etc.), retorna 0.0
        e emite um aviso *uma única vez*.

        Returns:
            Temperatura em °C ou 0.0 se indisponível.

        """
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return self._handle_no_sensor()

            # 1) Tenta sensores conhecidos na ordem de prioridade
            for sensor_name in PREFERRED_SENSORS:
                if sensor_name in temps:
                    readings = temps[sensor_name]
                    if readings:
                        return float(readings[0].current)

            # 2) Fallback: maior temperatura entre todos os sensores
            max_temp = 0.0
            for sensor_readings in temps.values():
                for reading in sensor_readings:
                    if reading.current > max_temp:
                        max_temp = reading.current

            return max_temp if max_temp > 0 else self._handle_no_sensor()

        except (
            AttributeError,
            OSError,
            IndexError,
            KeyError,
            RuntimeError,
        ):
            return self._handle_no_sensor()

    def _handle_no_sensor(self) -> float:
        """Retorna valor seguro e loga aviso apenas uma vez."""
        if not self._temp_warning_logged:
            logger.warning(
                "⚠️  Sensor de temperatura não encontrado "
                "(VM/WSL/hardware incompatível). "
                "Usando 0.0°C como fallback.",
            )
            self._temp_warning_logged = True
        return 0.0

    def _collect_metrics(self) -> HealthStatus:
        """Coleta todas as métricas do sistema.

        Returns:
            HealthStatus com o snapshot atual.

        """
        cpu = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        temperature = self._get_cpu_temperature()

        is_overheating = temperature > 0 and temperature >= self._temp_threshold

        status = HealthStatus(
            cpu_percent=cpu,
            memory_percent=memory,
            temperature_celsius=temperature,
            is_overheating=is_overheating,
            disk_usage_percent=disk,
            timestamp=time.time(),
        )

        # Registra em MetricsCollector (gauges)
        self._metrics.gauge("system_cpu_percent", cpu)
        self._metrics.gauge("system_memory_percent", memory)
        self._metrics.gauge("system_disk_percent", disk)
        self._metrics.gauge("system_temperature_celsius", temperature)

        return status

    def _check_thresholds(self, status: HealthStatus) -> None:
        """Verifica limites e aciona callbacks de autopreservação.

        Args:
            status: O snapshot de saúde atual.

        """
        # Detecção de transição para overheating
        was_overheating = self._is_overheating

        if status.is_overheating and not was_overheating:
            # Transição: normal → superaquecimento
            self._is_overheating = True
            self._metrics.increment("overheat_events")
            logger.warning(
                "🔥 AUTOPRESERVAÇÃO: Temperatura %.1f°C > %.1f°C! "
                "Pausando workers não essenciais.",
                status.temperature_celsius,
                self._temp_threshold,
            )
            if self.on_overheat is not None:
                self.on_overheat(status)

        elif not status.is_overheating and was_overheating:
            # Transição: superaquecimento → normal
            self._is_overheating = False
            self._metrics.increment("recovery_events")
            logger.info(
                "✅ RECUPERAÇÃO: Temperatura %.1f°C voltou ao normal. "
                "Retomando workers.",
                status.temperature_celsius,
            )
            if self.on_recover is not None:
                self.on_recover(status)

        # Alertas de CPU e memória (sem ação automática, apenas log)
        if status.cpu_percent >= self._cpu_threshold:
            logger.warning(
                "⚠️ CPU em %.1f%% (threshold: %.1f%%)",
                status.cpu_percent,
                self._cpu_threshold,
            )

        if status.memory_percent >= self._memory_threshold:
            logger.warning(
                "⚠️ RAM em %.1f%% (threshold: %.1f%%)",
                status.memory_percent,
                self._memory_threshold,
            )

    def _monitor_loop(self) -> None:
        """Loop principal de monitoramento."""
        while self._running:
            try:
                status = self._collect_metrics()

                with self._lock:
                    self._last_status = status

                self._check_thresholds(status)

                logger.debug(
                    "Health: CPU=%.1f%% RAM=%.1f%% Temp=%.1f°C Disk=%.1f%%",
                    status.cpu_percent,
                    status.memory_percent,
                    status.temperature_celsius,
                    status.disk_usage_percent,
                )

            except Exception:
                logger.exception("Erro na coleta de métricas de saúde")

            time.sleep(self._interval)

    def start(self) -> None:
        """Inicia o loop de monitoramento em thread separada."""
        if self._running:
            logger.warning("Monitor de saúde já está rodando")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="imunoedge-health",
        )
        self._thread.start()
        logger.info(
            "Monitor de saúde iniciado (intervalo: %.1fs, temp_threshold: %.1f°C)",
            self._interval,
            self._temp_threshold,
        )

    def stop(self) -> None:
        """Para o loop de monitoramento."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 2)
        logger.info("Monitor de saúde parado")

    def get_report(self) -> dict[str, Any]:
        """Gera relatório completo de saúde.

        Returns:
            Dicionário com todas as métricas e estado.

        """
        status = self.last_status
        return {
            "status": "overheating" if self._is_overheating else "healthy",
            "cpu_percent": status.cpu_percent if status else None,
            "memory_percent": status.memory_percent if status else None,
            "temperature_celsius": status.temperature_celsius if status else None,
            "disk_usage_percent": status.disk_usage_percent if status else None,
            "thresholds": {
                "temperature": self._temp_threshold,
                "cpu": self._cpu_threshold,
                "memory": self._memory_threshold,
            },
        }
