#!/usr/bin/env python3
"""
VERIFY_HARDENING.py
Script da Verdade para verificar blindagem do ImunoEdge.
"""
import os
import time
import shutil
import logging
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Ajusta path para importar módulos do projeto
import sys
sys.path.insert(0, str(Path.cwd() / "src"))
sys.path.insert(0, str(Path.cwd() / "TaipanStack" / "src"))

import imunoedge.core.telemetry
from imunoedge.core.telemetry import TelemetryClient
from imunoedge.core.orchestrator import ProcessOrchestrator, WorkerProcess, WorkerState

# Configura logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("VERIFY_HARDENING")

def test_disk_hardening():
    """Teste 1: Verifica se o TelemetryClient limpa o buffer SQLite (FIFO)."""
    logger.info(">>> INICIANDO TESTE DE BUFFER FIFO (SQLITE) <<<")
    
    # Cria diretório temporário para o banco
    temp_dir = Path(tempfile.mkdtemp())
    db_path = temp_dir / "buffer.db"
    
    # Patch MAX_BUFFER_ROWS directly in the module
    original_max = imunoedge.core.telemetry.MAX_BUFFER_ROWS
    imunoedge.core.telemetry.MAX_BUFFER_ROWS = 50
    logger.info(f"Patched MAX_BUFFER_ROWS to {imunoedge.core.telemetry.MAX_BUFFER_ROWS}")
    
    # Mock send_fn que falha sempre
    def fail_send(payload):
        raise ConnectionError("Simulated failure")

    client = None
    try:
        client = TelemetryClient(
            db_path=db_path,
            endpoint="http://invalid-endpoint",
            circuit_timeout=0.1,
            retry_max_attempts=1, # No retry to be faster
            send_fn=fail_send,
        )

        # Inserir 60 payloads (10 a mais que o limite)
        logger.info("Inserindo 60 payloads no buffer (limite configurado: 50)...")
        for i in range(60):
            client.send({"msg": f"payload_{i}", "val": i})

        # Verificar contagem no banco
        count = client.buffered_count
        logger.info(f"Contagem final no buffer: {count}")

        if count <= 50:
            logger.info("SUCESSO: Buffer respeitou o limite de 50 linhas!")
            return True
        else:
            logger.error(f"FALHA: Buffer tem {count} linhas (Esperado <= 50)")
            return False

    finally:
        if client:
            client.stop()
        # Restore original value
        imunoedge.core.telemetry.MAX_BUFFER_ROWS = original_max
        shutil.rmtree(temp_dir)

def test_zombie_hardening():
    """Teste 2: Verifica detecção de Zumbis via Heartbeat."""
    logger.info(">>> INICIANDO TESTE DE ZUMBI (HEARTBEAT) <<<")
    
    beat_file = Path("/tmp/imunoedge_mock_zombie.beat")
    beat_file.touch()
    
    # Simula heartbeat antigo (60 segundos atrás)
    old_time = time.time() - 60
    os.utime(beat_file, (old_time, old_time))
    
    orch = ProcessOrchestrator()
    
    # Cria worker mockado
    worker = WorkerProcess(
        name="mock_zombie",
        command=["echo", "zombie"],
        enable_heartbeat=True,
        state=WorkerState.RUNNING
    )
    worker.heartbeat_file = beat_file
    
    # Mock do processo (subprocess.Popen)
    worker.process = MagicMock()
    worker.process.poll.return_value = None # Processo está "rodando" (None) no SO
    worker.pid = 12345
    
    # Injeta worker no orquestrador
    orch._workers["mock_zombie"] = worker
    
    # Executa _is_alive (método privado, mas acessível para teste)
    logger.info("Verificando se worker zumbi é detectado...")
    is_alive = orch._is_alive(worker)
    
    # Limpa arquivo
    if beat_file.exists():
        beat_file.unlink()

    if is_alive is False:
        logger.info("SUCESSO: Worker Zumbi detectado e marcado como morto!")
        return True
    else:
        logger.error("FALHA: Worker Zumbi foi considerado VIVO incorretamente.")
        return False

def main():
    print("=== IMUNOEDGE VERIFY HARDENING ===")
    
    disk_ok = test_disk_hardening()
    print("-" * 30)
    zombie_ok = test_zombie_hardening()
    
    print("=" * 30)
    if disk_ok and zombie_ok:
        print("RESULTADO FINAL: OK (Todos os testes passaram)")
        sys.exit(0)
    else:
        print("RESULTADO FINAL: FALHA (Verifique logs)")
        sys.exit(1)

if __name__ == "__main__":
    main()
