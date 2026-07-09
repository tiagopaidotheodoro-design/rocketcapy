"""
rocketcapy_snapshot.py

Rastreia holders do token $ROCKETCAPY na Solana ao longo do tempo, salva
snapshots em SQLite, e calcula elegibilidade pro airdrop com base em
tempo de hold contínuo.

REQUISITOS
    pip install requests base58 --break-system-packages

CONFIGURAÇÃO
    - Preencha TOKEN_MINT com o contract address do seu token depois do lançamento.
    - Preencha RPC_URL com um endpoint de RPC Solana. Recomendo Helius
      (https://helius.dev, tem free tier) em vez do RPC público, que tem
      rate limit agressivo pra getProgramAccounts.
      Ex: RPC_URL = "https://mainnet.helius-rpc.com/?api-key=SUA_CHAVE"

COMO RODAR
    Modo único (um snapshot agora):
        python rocketcapy_snapshot.py --once

    Modo contínuo (snapshot a cada N horas até você parar com Ctrl+C):
        python rocketcapy_snapshot.py --loop --interval-hours 4

    Calcular elegibilidade pro airdrop (depois de coletar snapshots):
        python rocketcapy_snapshot.py --eligibility

NOTA IMPORTANTE SOBRE PRECISÃO
    Esse script funciona por POLLING (tira uma "foto" do saldo de cada
    carteira a cada N horas). Isso significa que ele pode não perceber
    se alguém vendeu e recomprou entre dois snapshots. Pra maior precisão
    perto do prazo final do snapshot, rode com --interval-hours 1 ou menor
    nas últimas 24-48h antes do corte. Pra precisão total (pegar toda
    transação, sem brecha nenhuma), a alternativa é usar webhooks da
    Helius, que notificam em tempo real a cada compra/venda — mais
    trabalho de configurar, mas sem essa limitação.
"""

import argparse
import base64
import sqlite3
import struct
import time
from datetime import datetime, timezone

import base58
import requests

# ========== CONFIGURAÇÃO — EDITE AQUI ==========
TOKEN_MINT = "COLOQUE_O_CONTRACT_ADDRESS_AQUI"
RPC_URL = "https://api.mainnet-beta.solana.com"  # troque por Helius em produção
DB_PATH = "rocketcapy_snapshots.db"

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_ACCOUNT_SIZE = 165  # tamanho fixo de uma conta de token SPL

# Regras do airdrop (mesmas do site — ajuste se mudar lá)
MIN_HOLD_TOKENS = 50_000_000        # ~0.05 SOL em tokens no preço de lançamento; recalibrar depois
HOLD_SINCE_HOURS_AFTER_LAUNCH = 24  # precisa estar segurando desde T+24h
# =================================================


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            wallet TEXT NOT NULL,
            balance INTEGER NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ts ON snapshots(wallet, ts)")
    conn.commit()
    return conn


def fetch_token_holders(mint: str, rpc_url: str):
    """
    Busca todas as contas de token (holders) de um mint via getProgramAccounts,
    filtrando pelo Token Program e pelo tamanho fixo de conta SPL, com o mint
    no offset 0. Retorna lista de (wallet_owner, balance_raw).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            TOKEN_PROGRAM_ID,
            {
                "encoding": "base64",
                "filters": [
                    {"dataSize": SPL_TOKEN_ACCOUNT_SIZE},
                    {"memcmp": {"offset": 0, "bytes": mint}},
                ],
            },
        ],
    }
    resp = requests.post(rpc_url, json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()

    if "error" in result:
        raise RuntimeError(f"Erro no RPC: {result['error']}")

    balances = {}
    for entry in result.get("result", []):
        raw = base64.b64decode(entry["account"]["data"][0])
        # Layout de uma conta SPL Token (165 bytes):
        # mint (32) | owner (32) | amount (8, u64 LE) | ...resto
        owner_bytes = raw[32:64]
        amount = struct.unpack("<Q", raw[64:72])[0]
        owner = base58.b58encode(owner_bytes).decode()
        if amount > 0:
            # soma caso a carteira tenha mais de uma conta de token pro mesmo mint
            balances[owner] = balances.get(owner, 0) + amount

    return list(balances.items())


def take_snapshot():
    conn = init_db()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Buscando holders de {TOKEN_MINT}...")
    holders = fetch_token_holders(TOKEN_MINT, RPC_URL)
    ts = int(time.time())

    conn.executemany(
        "INSERT INTO snapshots (wallet, balance, ts) VALUES (?, ?, ?)",
        [(w, b, ts) for w, b in holders],
    )
    conn.commit()
    conn.close()
    print(f"  -> {len(holders)} carteiras salvas neste snapshot (ts={ts}).")


def run_loop(interval_hours: float):
    print(f"Rodando em loop a cada {interval_hours}h. Ctrl+C pra parar.")
    while True:
        try:
            take_snapshot()
        except Exception as e:
            print(f"  ERRO no snapshot: {e}")
        time.sleep(interval_hours * 3600)


def compute_eligibility(launch_ts: int, snapshot_ts: int):
    """
    Elegível = manteve saldo >= MIN_HOLD_TOKENS em TODOS os snapshots
    registrados entre (launch_ts + HOLD_SINCE_HOURS_AFTER_LAUNCH horas) e
    snapshot_ts. Se a carteira não aparece num snapshot nesse intervalo,
    ela é tratada como saldo zero naquele momento (desqualifica).
    """
    conn = init_db()
    cutoff_start = launch_ts + int(HOLD_SINCE_HOURS_AFTER_LAUNCH * 3600)

    rows = conn.execute(
        "SELECT DISTINCT ts FROM snapshots WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (cutoff_start, snapshot_ts),
    ).fetchall()
    snapshot_times = [r[0] for r in rows]

    if not snapshot_times:
        print("Nenhum snapshot encontrado nesse intervalo. Rode --once ou --loop primeiro.")
        return []

    wallets = conn.execute(
        "SELECT DISTINCT wallet FROM snapshots WHERE ts BETWEEN ? AND ?",
        (cutoff_start, snapshot_ts),
    ).fetchall()

    eligible = []
    for (wallet,) in wallets:
        balances = dict(
            conn.execute(
                "SELECT ts, balance FROM snapshots WHERE wallet=? AND ts BETWEEN ? AND ?",
                (wallet, cutoff_start, snapshot_ts),
            ).fetchall()
        )
        # precisa estar presente (com saldo suficiente) em TODOS os snapshots do intervalo
        held_throughout = all(
            balances.get(t, 0) >= MIN_HOLD_TOKENS for t in snapshot_times
        )
        if held_throughout:
            hours_held = (snapshot_ts - cutoff_start) / 3600
            eligible.append((wallet, hours_held, balances[snapshot_times[-1]]))

    conn.close()
    eligible.sort(key=lambda x: -x[1])  # mais tempo segurando primeiro
    return eligible


def main():
    parser = argparse.ArgumentParser(description="Snapshot de holders $ROCKETCAPY")
    parser.add_argument("--once", action="store_true", help="Tira um snapshot agora e sai")
    parser.add_argument("--loop", action="store_true", help="Roda continuamente")
    parser.add_argument("--interval-hours", type=float, default=4, help="Intervalo entre snapshots no modo loop")
    parser.add_argument("--eligibility", action="store_true", help="Calcula elegibilidade pro airdrop")
    parser.add_argument("--launch-ts", type=int, help="Timestamp unix do lançamento (obrigatório com --eligibility)")
    parser.add_argument("--snapshot-ts", type=int, default=int(time.time()), help="Timestamp unix do corte final (default: agora)")
    args = parser.parse_args()

    if args.once:
        take_snapshot()
    elif args.loop:
        run_loop(args.interval_hours)
    elif args.eligibility:
        if not args.launch_ts:
            print("Use --launch-ts <timestamp_unix_do_lançamento> junto com --eligibility")
            return
        result = compute_eligibility(args.launch_ts, args.snapshot_ts)
        print(f"\n{len(result)} carteiras elegíveis:\n")
        for wallet, hours, balance in result:
            print(f"  {wallet}  |  {hours:.1f}h segurando  |  saldo atual: {balance}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
