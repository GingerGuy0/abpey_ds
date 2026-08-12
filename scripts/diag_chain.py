"""Діагностика: чому check_by_hash повертає chain_error.

Запуск (з кореня проєкту, у venv):
    python scripts/diag_chain.py 0x<hash>            # мережа bsc за замовчуванням
    python scripts/diag_chain.py 0x<hash> eth

Скрипт б'є в ті самі три ендпоінти Etherscan V2, що й EvmChainClient.get_tx_by_hash,
і друкує СИРУ відповідь — саме її ковтає _safe_request і перетворює на
"Помилка мережі блокчейн".
"""
import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, ".")
from app.config import NETWORKS, settings  # noqa: E402

BASE_URL = "https://api.etherscan.io/v2/api"


async def call(session: aiohttp.ClientSession, params: dict) -> None:
    params = {**params, "apikey": settings.etherscan_api_key}
    print(f"\n>>> {params.get('action')}  (chainid={params.get('chainid')})")
    try:
        async with session.get(BASE_URL, params=params) as resp:
            print(f"    HTTP {resp.status}  content-type={resp.headers.get('content-type')}")
            body = await resp.text()
    except Exception as e:  # noqa: BLE001
        print(f"    !!! transport error: {type(e).__name__}: {e}")
        return

    try:
        data = json.loads(body)
    except ValueError:
        print(f"    !!! не JSON (перші 300 символів):\n    {body[:300]}")
        return

    status = data.get("status")
    result = data.get("result")
    if status == "0":
        print(f"    !!! Etherscan status=0  message={data.get('message')!r}  result={result!r}")
        print("    ^ саме це _safe_request перетворює на ChainUnavailable -> chain_error")
        return
    if data.get("error"):
        print(f"    !!! JSON-RPC error: {data['error']}")
        return
    if result is None:
        print("    result = null (для receipt це означає TxNotFound -> 'not_found')")
        return

    preview = json.dumps(result)[:300]
    print(f"    OK: {preview}")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    tx_hash = sys.argv[1]
    net = sys.argv[2] if len(sys.argv) > 2 else "bsc"
    cfg = NETWORKS[net]

    key = settings.etherscan_api_key
    print(f"мережа: {net} (chainid={cfg.chainid})")
    print(f"ETHERSCAN_API_KEY: {'ВІДСУТНІЙ!' if not key else key[:4] + '...' + key[-4:] + f' (довжина {len(key)})'}")
    print(f"гаманець з .env: {settings.wallet_from_env(net) or 'НЕ ЗАДАНО'}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        await call(session, {
            "module": "proxy", "action": "eth_getTransactionReceipt",
            "txhash": tx_hash, "chainid": cfg.chainid,
        })
        await asyncio.sleep(0.3)
        await call(session, {
            "module": "proxy", "action": "eth_blockNumber", "chainid": cfg.chainid,
        })
        await asyncio.sleep(0.3)
        await call(session, {
            "module": "account", "action": "tokentx",
            "contractaddress": cfg.usdt_contract,
            "address": settings.wallet_from_env(net),
            "page": 1, "offset": 5, "sort": "desc", "chainid": cfg.chainid,
        })


if __name__ == "__main__":
    asyncio.run(main())
