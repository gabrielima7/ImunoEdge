#!/usr/bin/env python3
"""
VERIFY_HARDENING.py
Script da Verdade para verificar blindagem do ImunoEdge.
"""
import os
import time
import shutil
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

# Ajusta path para importar módulos do projeto
import sys
sys.path.insert(0, str(Path.cwd() / "src"))
sys.path.insert(0, str(Path.cwd() / "TaipanStack" / "src"))

from imunoedge.core.telemetry import TelemetryClient
from imunoedge.core.orchestrator import ProcessOrchestrator, WorkerProcess

# Configura logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("VERIFY_HARDENING")

def test_disk_hardening():
    """Teste 1: Verifica se o TelemetryClient limpa o disco."""
    logger.info(">>> INICIANDO TESTE DE DISCO (LOG ROTATION) <<<")
    
    buffer_dir = Path("/tmp/imunoedge_verify_buffer")
    if buffer_dir.exists():
        shutil.rmtree(buffer_dir)
    buffer_dir.mkdir(parents=True)

    # 1. Encher o buffer com 60MB (arquivos de 1MB)
    logger.info("Gerando 60MB de lixo...")
    for i in range(60):
        p = buffer_dir / f"garbage_{i}.json"
        # Cria arquivo de 1MB
        with p.open("wb") as f:
            f.write(b"0" * 1024 * 1024)
        # Ajusta mtime para simular antiguidade (os primeiros são mais velhos)
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))

    initial_size = sum(f.stat().st_size for f in buffer_dir.glob("*.json")) / (1024 * 1024)
    logger.info(f"Tamanho inicial do buffer: {initial_size:.2f} MB")
    
    if initial_size < 59:
        logger.error("FALHA: Não conseguiu gerar 60MB de teste.")
        return False

    # 2. Instanciar TelemetryClient e forçar escrita (que deve disparar limpeza)
    # Configura limite de 50MB via env
    os.environ["IMUNOEDGE_MAX_BUFFER_MB"] = "50"
    
    # Mock send_fn que falha sempre
    def fail_send(payload):
        raise ConnectionError("Simulated failure")

    client = TelemetryClient(
        buffer_dir=buffer_dir,
        endpoint="http://invalid-endpoint",
        circuit_timeout=0.1,
        send_fn=fail_send,
    )
    
    # Simula envio que falha e salva localmente
    # Isso deve acionar _enforce_buffer_limit ANTES de salvar
    logger.info("Tentando enviar payload (deve falhar e acionar limpeza)...")
    success = client.send({"test": "data"})
    
    # 3. Verificar tamanho final
    final_size = sum(f.stat().st_size for f in buffer_dir.glob("*.json")) / (1024 * 1024)
    logger.info(f"Tamanho final do buffer: {final_size:.2f} MB")
    
    if final_size < 50:
        logger.info("SUCESSO: Buffer foi reduzido para < 50MB!")
        return True
    else:
        logger.error(f"FALHA: Buffer continua com {final_size:.2f} MB (Esperado < 50MB)")
        return False

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
        state="running"
    )
    worker.heartbeat_file = beat_file
    
    # Mock do processo (subprocess.Popen)
    worker.process = MagicMock()
    worker.process.poll.return_value = None # Processo está "rodando" (None) no SO
    worker.pid = 12345
    
    # Injeta worker no orquestrador (hack para teste unitário)
    orch._workers["mock_zombie"] = worker
    
    # Executa _is_alive
    logger.info("Verificando se worker zumbi é detectado...")
    is_alive = orch._is_alive(worker)
    
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
