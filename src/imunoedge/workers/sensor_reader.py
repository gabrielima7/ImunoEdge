#!/usr/bin/env python3
"""
Worker de exemplo: Leitor de Sensor Fictício.

Simula leitura de sensores IoT (temperatura e umidade)
e imprime os dados em stdout no formato JSON.
Este script é projetado para ser executado como processo
filho pelo ProcessOrchestrator.
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import UTC, datetime


def simulate_sensor_reading() -> dict[str, float | str]:
    """Gera uma leitura simulada de sensor.

    Returns:
        Dicionário com dados do sensor.

    """
    return {
        "sensor_id": "temp_humidity_01",
        "temperature_celsius": round(20.0 + random.uniform(-5, 15), 2),
        "humidity_percent": round(40.0 + random.uniform(-10, 30), 2),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    """Loop principal do worker de leitura de sensores."""
    interval = 2.0  # Segundos entre leituras
    reading_count = 0

    sys.stdout.write(
        json.dumps({"event": "worker_started", "sensor": "temp_humidity_01"}) + "\n"
    )
    sys.stdout.flush()

    try:
        while True:
            reading = simulate_sensor_reading()
            reading_count += 1

            output = json.dumps(reading)
            sys.stdout.write(output + "\n")
            sys.stdout.flush()

            time.sleep(interval)

    except KeyboardInterrupt:
        sys.stdout.write(
            json.dumps(
                {
                    "event": "worker_stopped",
                    "total_readings": reading_count,
                }
            )
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
