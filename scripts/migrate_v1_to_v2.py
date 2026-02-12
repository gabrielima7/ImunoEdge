#!/usr/bin/env python3
import os
import sys
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

# Tenta carregar variáveis de ambiente do .env se existir
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Adiciona src ao path caso necessário (para garantir importação)
src_path = Path(__file__).resolve().parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

try:
    from imunoedge.core.telemetry import TelemetryClient, DEFAULT_DB_PATH
except ImportError:
    print("Erro: Não foi possível importar 'imunoedge.core.telemetry'. Verifique se as dependências estão instaladas.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_v1_to_v2")

def get_legacy_dirs() -> list[Path]:
    """Retorna lista de diretórios onde arquivos legados podem estar."""
    dirs = [
        Path("/tmp/imunoedge_telemetry_buffer"),
        DEFAULT_DB_PATH.parent,
    ]
    # Remover duplicatas e validar existência
    unique_dirs = []
    seen = set()
    for d in dirs:
        resolved = d.resolve()
        if resolved not in seen and d.exists():
            unique_dirs.append(d)
            seen.add(resolved)
    return unique_dirs

def migrate():
    logger.info(f"Iniciando migração para DB em: {DEFAULT_DB_PATH}")

    # 1. Inicializar buffer.db usando TelemetryClient
    try:
        # Instanciar o cliente inicializa o banco no __init__ -> _init_db
        # Usamos apenas kwargs necessários
        client = TelemetryClient(db_path=DEFAULT_DB_PATH)
        client.stop() # Fecha conexão do cliente, usaremos sqlite3 direto
        logger.info(f"Banco de dados inicializado/verificado.")
    except Exception as e:
        logger.error(f"Falha ao inicializar TelemetryClient/Banco de dados: {e}")
        return

    # 2. Escanear por arquivos .json em diretórios legados
    legacy_dirs = get_legacy_dirs()
    json_files = []

    for d in legacy_dirs:
        found = list(d.glob("*.json"))
        if found:
            logger.info(f"Encontrados {len(found)} arquivos em {d}")
            json_files.extend(found)

    if not json_files:
        logger.info("Nenhum arquivo .json encontrado para migração.")
        print("0 arquivos migrados, 0 falhas")
        return

    logger.info(f"Total de {len(json_files)} arquivos .json para migrar.")

    migrated_count = 0
    failed_count = 0
    # Quarentena será no mesmo diretório do DB
    quarantine_dir = DEFAULT_DB_PATH.parent / ".quarantine"

    # Conectar ao SQLite para inserção manual
    try:
        conn = sqlite3.connect(str(DEFAULT_DB_PATH), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.Error as e:
        logger.error(f"Erro ao conectar no SQLite: {e}")
        print(f"0 arquivos migrados, {len(json_files)} falhas (erro de conexão DB)")
        return

    # 3. Processar cada arquivo
    for json_file in json_files:
        try:
            # Ler conteúdo
            with open(json_file, 'r', encoding='utf-8') as f:
                content = f.read()
                # Validação básica de JSON
                json.loads(content)

            # Inserir no SQLite
            # Usamos o timestamp atual para created_at pois o arquivo não garante ter timestamp
            # Tenta pegar mtime do arquivo
            try:
                created_at = json_file.stat().st_mtime
            except OSError:
                created_at = time.time()

            conn.execute(
                "INSERT INTO telemetry_queue (payload_json, created_at) VALUES (?, ?)",
                (content, created_at)
            )
            conn.commit()

            # Sucesso: deletar arquivo
            json_file.unlink()
            migrated_count += 1
            # logger.debug(f"Migrado: {json_file.name}")

        except (json.JSONDecodeError, sqlite3.Error, OSError) as e:
            logger.error(f"Falha ao migrar {json_file.name}: {e}")
            failed_count += 1

            # Mover para quarentena
            try:
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                destination = quarantine_dir / json_file.name
                if destination.exists():
                    # Evitar sobrescrever se já existir algo com mesmo nome na quarentena
                    timestamp = int(time.time())
                    destination = quarantine_dir / f"{json_file.stem}_{timestamp}{json_file.suffix}"

                shutil.move(str(json_file), str(destination))
                logger.info(f"Arquivo movido para quarentena: {destination.name}")
            except OSError as move_err:
                logger.error(f"Falha crítica: não foi possível mover {json_file.name} para quarentena: {move_err}")

    conn.close()

    # 4. Relatório final
    report = f"{migrated_count} arquivos migrados, {failed_count} falhas"
    logger.info(report)
    print(report)

if __name__ == "__main__":
    migrate()
