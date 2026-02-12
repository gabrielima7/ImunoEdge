# 🧪 ImunoEdge — Guia de Stress Tests

Testes manuais para validar que a autocura funciona como prometido.

> [!IMPORTANT]
> Execute o installer primeiro (`sudo bash install.sh`) e confirme que o serviço está ativo antes de rodar esses testes.

---

## Pré-requisitos

```bash
# Confirmar que o serviço está rodando
sudo systemctl status imunoedge

# Abrir logs em tempo real (mantenha aberto em outro terminal)
journalctl -u imunoedge -f
```

---

## Teste 1: O Imortal (Watchdog)

**Objetivo:** Matar um worker e verificar que o watchdog o ressuscita automaticamente.

### Passo a passo

```bash
# 1. Descubra o PID do worker sensor_reader
ps aux | grep sensor_reader
```

Saída esperada:
```
imunoedge  12345  0.0  0.2  python3 /opt/imunoedge/src/imunoedge/workers/sensor_reader.py
```

```bash
# 2. Mate o processo sem piedade
sudo kill -9 <PID_DO_SENSOR>
```

```bash
# 3. Observe o log (no terminal com journalctl -f)
```

### Resultado esperado no log

```
⚠️ Worker 'sensor_reader' morreu (exit_code=-9). Reiniciando... (restart 1/10)
✅ Worker 'sensor_reader' reiniciado com PID 12399
```

```bash
# 4. Confirme que o worker está vivo novamente
ps aux | grep sensor_reader
```

> [!TIP]
> Repita o kill várias vezes para testar o limite de `IMUNOEDGE_MAX_RESTARTS` (padrão: 10).

---

## Teste 2: A Febre (Autopreservação Térmica)

**Objetivo:** Simular superaquecimento e verificar que workers não essenciais são pausados automaticamente.

### Passo a passo

```bash
# 1. Edite o .env para um threshold ridiculamente baixo
sudo nano /opt/imunoedge/.env
```

Altere a linha:
```diff
- IMUNOEDGE_TEMP_THRESHOLD=75.0
+ IMUNOEDGE_TEMP_THRESHOLD=20.0
```

```bash
# 2. Reinicie o serviço
sudo systemctl restart imunoedge
```

```bash
# 3. Observe o log (no terminal com journalctl -f)
```

### Resultado esperado no log

```
🔥 SOBREAQUECIMENTO DETECTADO: 44.0°C > 20.0°C (limite)
🔥 Worker 'sensor_reader' pausado por autopreservação
📡 Enviando telemetria: event=overheat_protection
```

```bash
# 4. Verifique que o worker está pausado (SIGSTOP)
ps aux | grep sensor_reader
# O processo deve existir mas estar em estado T (stopped)
```

```bash
# 5. Restaure o threshold original
sudo nano /opt/imunoedge/.env
# Mude IMUNOEDGE_TEMP_THRESHOLD de volta para 75.0
sudo systemctl restart imunoedge
```

### Resultado esperado no log após restaurar

```
✅ Temperatura normalizada: 44.0°C < 75.0°C
✅ Worker 'sensor_reader' retomado após recuperação
```

---

## Teste 3: A Queda da Nuvem (Store-and-Forward)

**Objetivo:** Simular falha de rede e verificar que a telemetria é armazenada localmente.

### Passo a passo

```bash
# 1. Configure um endpoint inexistente no .env
sudo nano /opt/imunoedge/.env
```

```diff
- IMUNOEDGE_TELEMETRY_ENDPOINT=https://iot.example.com/api/v1/telemetry
+ IMUNOEDGE_TELEMETRY_ENDPOINT=https://192.168.99.99:9999/telemetry
```

```bash
# 2. Reduza os timeouts para ver o efeito mais rápido
# No mesmo .env:
IMUNOEDGE_CIRCUIT_FAILURE_THRESHOLD=2
IMUNOEDGE_CIRCUIT_TIMEOUT=15.0
IMUNOEDGE_RETRY_MAX_ATTEMPTS=1
IMUNOEDGE_FLUSH_INTERVAL=10.0
```

```bash
# 3. Reinicie o serviço
sudo systemctl restart imunoedge
```

```bash
# 4. Observe nos logs
journalctl -u imunoedge -f
```

### Resultado esperado

```
📡 Tentativa 1/1 falhou: Connection refused. Retry em 2.0s...
⚠️ Circuito aberto — telemetria armazenada em SQLite: <payload-id>
```

```bash
# 5. Verifique o banco SQLite de buffer (necessita sqlite3 instalado)
# O buffer fica em /var/lib/imunoedge/buffer.db
sudo sqlite3 /var/lib/imunoedge/buffer.db "SELECT COUNT(*) FROM telemetry_queue;"
# Deve retornar um número > 0
```

```bash
# 6. Restaure o endpoint e veja o flush
sudo nano /opt/imunoedge/.env
# Restore o endpoint padrão
sudo systemctl restart imunoedge
```

> [!NOTE]
> Com o endpoint padrão (localhost), o flush loop tentará reenviar os payloads armazenados. As linhas serão removidas da tabela `telemetry_queue` conforme são reenviadas.

---

## Teste 4: Graceful Shutdown

**Objetivo:** Confirmar que o shutdown é limpo, sem corromper dados.

```bash
# 1. Pare o serviço graciosamente
sudo systemctl stop imunoedge

# 2. Observe no log
journalctl -u imunoedge -n 20 --no-pager
```

### Resultado esperado

```
⚠️ Sinal SIGTERM recebido — iniciando graceful shutdown...
═══ GRACEFUL SHUTDOWN INICIADO ═══
Parando workers...
Parando health monitor...
Flush final de telemetria...
Parando telemetry client...
Métricas finais: {...}
═══ GRACEFUL SHUTDOWN CONCLUÍDO ═══
🛡️ ImunoEdge desativado com segurança.
```

---

## Checklist de Validação

| # | Teste | Status |
|---|---|---|
| 1 | Worker morto é ressuscitado pelo watchdog | ⬜ |
| 2 | Autopreservação pausa workers sob calor | ⬜ |
| 3 | Telemetria faz store-and-forward sem rede | ⬜ |
| 4 | Graceful shutdown executa sem erros | ⬜ |

Preencha com ✅ após cada teste passar.

---

## Comandos de Referência Rápida

```bash
# Status do serviço
sudo systemctl status imunoedge

# Logs em tempo real
journalctl -u imunoedge -f

# Últimas 50 linhas de log
journalctl -u imunoedge -n 50 --no-pager

# Reiniciar serviço
sudo systemctl restart imunoedge

# Parar serviço
sudo systemctl stop imunoedge

# PIDs dos workers
ps aux | grep imunoedge

# Buffer de telemetria
sudo sqlite3 /var/lib/imunoedge/buffer.db "SELECT * FROM telemetry_queue LIMIT 5;"

# Editar configuração
sudo nano /opt/imunoedge/.env
```
