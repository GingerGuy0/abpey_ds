"""Тести RPC-клієнта без мережі: ноди й БД замокані."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chains.base import ChainUnavailable, TxNotFound  # noqa: E402
from app.chains.evm_rpc import EvmRpcChainClient  # noqa: E402
from app.config import NETWORKS  # noqa: E402

CFG = NETWORKS["bsc"]
ADDR = "0x07b86dd40697790e824d03b8d2b10df51a25afe4"
CONTRACT = CFG.usdt_contract.lower()
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def topic_addr(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0").lower()


def make_client(monkeypatch, urls=None):
    client = EvmRpcChainClient(CFG, session=None, urls=urls or ["http://a", "http://b"])
    # Курсор сканування зберігається в БД — підміняємо на пам'ять.
    store = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_get(_db, key, default=None):
        return store.get(key, default)

    async def fake_set(_db, key, value):
        store[key] = value

    monkeypatch.setattr("app.chains.evm_rpc.async_session", lambda: FakeSession())
    monkeypatch.setattr("app.chains.evm_rpc.repo.get_setting", fake_get)
    monkeypatch.setattr("app.chains.evm_rpc.repo.set_setting", fake_set)
    return client, store


@pytest.mark.asyncio
async def test_range_error_halves_chunk(monkeypatch):
    """"limit exceeded" має зменшувати вікно, а не вбивати ноди."""
    client, _ = make_client(monkeypatch)
    client._chunk_blocks = 512
    seen_ranges = []

    async def fake_request(method, url, **kwargs):
        rpc_method = kwargs["json"]["method"]
        if rpc_method == "eth_blockNumber":
            return {"result": hex(1000)}
        if rpc_method == "eth_getLogs":
            p = kwargs["json"]["params"][0]
            span = int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1
            seen_ranges.append(span)
            if span > 128:
                return {"error": {"code": -32000, "message": "limit exceeded"}}
            return {"result": []}
        raise AssertionError(rpc_method)

    monkeypatch.setattr(client, "_request", fake_request)
    await client.get_incoming_transfers(ADDR, datetime.now(timezone.utc) - timedelta(hours=1))

    # Кожне вікно пробується на всіх нодах, тому дивимось послідовність без повторів.
    steps = [span for i, span in enumerate(seen_ranges) if i == 0 or span != seen_ranges[i - 1]]
    assert steps[:3] == [512, 256, 128]
    # Ноди живі: жодна не відправлена в кулдаун.
    assert all(cd == 0.0 for cd in client.cooldowns.values())
    # Робоче вікно запам'яталося.
    assert client._chunk_blocks == 128


@pytest.mark.asyncio
async def test_node_without_getlogs_is_excluded(monkeypatch):
    """bsc-dataseed відмовляє на будь-якому вікні -> сканування йде іншою нодою."""
    client, _ = make_client(monkeypatch, urls=["http://dataseed", "http://good"])
    client._chunk_blocks = 128
    used_for_logs = []

    async def fake_request(method, url, **kwargs):
        rpc_method = kwargs["json"]["method"]
        if rpc_method == "eth_blockNumber":
            return {"result": hex(1000)}
        if rpc_method == "eth_getLogs":
            used_for_logs.append(url)
            if url == "http://dataseed":
                return {"error": {"code": -32000, "message": "limit exceeded"}}
            return {"result": []}
        raise AssertionError(rpc_method)

    monkeypatch.setattr(client, "_request", fake_request)
    await client.get_incoming_transfers(ADDR, datetime.now(timezone.utc) - timedelta(hours=1))

    # Погана нода виключена з майбутніх запитів логів, але не з кулдауну загалом.
    assert client.logs_blocked_until["http://dataseed"] > 0
    assert client.logs_blocked_until["http://good"] == 0.0
    assert client.cooldowns["http://dataseed"] == 0.0
    # Вікно не зменшувалось — проблема була в ноді, а не в розмірі.
    assert client._chunk_blocks == 128

    used_for_logs.clear()
    await client.get_incoming_transfers(ADDR, datetime.now(timezone.utc) - timedelta(hours=1))
    assert set(used_for_logs) == {"http://good"}


@pytest.mark.asyncio
async def test_all_nodes_refuse_getlogs_degrades_quietly(monkeypatch):
    """Якщо логів не дає ніхто — сканер мовчки вимикається, а не сипле трейсбеки."""
    client, store = make_client(monkeypatch)
    client._chunk_blocks = 64

    async def fake_request(method, url, **kwargs):
        rpc_method = kwargs["json"]["method"]
        if rpc_method == "eth_blockNumber":
            return {"result": hex(1000)}
        if rpc_method == "eth_getLogs":
            return {"error": {"code": -32000, "message": "limit exceeded"}}
        raise AssertionError(rpc_method)

    monkeypatch.setattr(client, "_request", fake_request)
    store["last_scanned_block:bsc"] = "900"

    assert await client.get_incoming_transfers(ADDR, datetime.now(timezone.utc)) == []
    assert all(v > 0 for v in client.logs_blocked_until.values())
    # Курсор не зрушив — коли ноди повернуться, нічого не пропустимо.
    assert store["last_scanned_block:bsc"] == "900"
    # Другий виклик не падає й не звертається до нод узагалі.
    assert await client.get_incoming_transfers(ADDR, datetime.now(timezone.utc)) == []


@pytest.mark.asyncio
async def test_failover_on_dead_node(monkeypatch):
    """Транспортна помилка -> кулдаун ноди й перехід на наступну."""
    client, _ = make_client(monkeypatch)
    calls = []

    async def fake_request(method, url, **kwargs):
        calls.append(url)
        if url == "http://a":
            raise TimeoutError("boom")
        return {"result": hex(500)}

    monkeypatch.setattr(client, "_request", fake_request)
    assert await client._block_number() == 500
    assert calls == ["http://a", "http://b"]
    assert client.cooldowns["http://a"] > 0
    assert client.cooldowns["http://b"] == 0.0


@pytest.mark.asyncio
async def test_all_nodes_down(monkeypatch):
    client, _ = make_client(monkeypatch)

    async def fake_request(method, url, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(ChainUnavailable):
        await client._block_number()


@pytest.mark.asyncio
async def test_receipt_sums_multiple_transfers(monkeypatch):
    """Кілька Transfer на нашу адресу в одній транзакції -> сума."""
    client, _ = make_client(monkeypatch)

    def log(to_addr, units, contract=CONTRACT):
        return {
            "address": contract,
            "topics": [TRANSFER, topic_addr("0x1111111111111111111111111111111111111111"), topic_addr(to_addr)],
            "data": hex(units),
        }

    async def fake_request(method, url, **kwargs):
        m = kwargs["json"]["method"]
        if m == "eth_getTransactionReceipt":
            return {"result": {
                "status": "0x1",
                "blockNumber": hex(900),
                "logs": [
                    log("0x9999999999999999999999999999999999999999", 5 * 10**18),
                    log(ADDR, 10 * 10**18),
                    log(ADDR, 20 * 10**18),
                    log(ADDR, 7 * 10**18, contract="0xdeadbeef00000000000000000000000000000000"),
                ],
            }}
        if m == "eth_blockNumber":
            return {"result": hex(950)}
        if m == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(1_700_000_000)}}
        raise AssertionError(m)

    monkeypatch.setattr(client, "_request", fake_request)
    tx = await client.get_tx_by_hash("0x" + "a" * 64, ADDR)

    assert tx.units == 30 * 10**18          # чужа адреса й чужий контракт не враховані
    assert tx.amount == Decimal("30")
    assert tx.confirmations == 50
    assert tx.is_success is True


@pytest.mark.asyncio
async def test_receipt_without_our_transfer(monkeypatch):
    client, _ = make_client(monkeypatch)

    async def fake_request(method, url, **kwargs):
        m = kwargs["json"]["method"]
        if m == "eth_getTransactionReceipt":
            return {"result": {"status": "0x1", "blockNumber": hex(900), "logs": [{
                "address": CONTRACT,
                "topics": [TRANSFER, topic_addr(ADDR), topic_addr("0x9999999999999999999999999999999999999999")],
                "data": hex(10**18),
            }]}}
        if m == "eth_blockNumber":
            return {"result": hex(950)}
        if m == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(1_700_000_000)}}
        raise AssertionError(m)

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(TxNotFound):
        await client.get_tx_by_hash("0x" + "a" * 64, ADDR)


@pytest.mark.asyncio
async def test_missing_receipt_is_not_found(monkeypatch):
    client, _ = make_client(monkeypatch)

    async def fake_request(method, url, **kwargs):
        return {"result": None}

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(TxNotFound):
        await client.get_tx_by_hash("0x" + "a" * 64, ADDR)


@pytest.mark.asyncio
async def test_scan_parses_logs_and_moves_cursor(monkeypatch):
    client, store = make_client(monkeypatch)
    client._chunk_blocks = 100
    now = datetime.now(timezone.utc)
    block_ts = int(now.timestamp()) - 60

    async def fake_request(method, url, **kwargs):
        m = kwargs["json"]["method"]
        if m == "eth_blockNumber":
            return {"result": hex(10_000)}
        if m == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(block_ts)}}
        if m == "eth_getLogs":
            p = kwargs["json"]["params"][0]
            assert p["topics"][2] == topic_addr(ADDR)   # фільтр отримувача на боці ноди
            assert p["address"] == CONTRACT
            lo, hi = int(p["fromBlock"], 16), int(p["toBlock"], 16)
            if lo <= 9_900 <= hi:
                return {"result": [{
                    "topics": [TRANSFER, topic_addr("0x2222222222222222222222222222222222222222"), topic_addr(ADDR)],
                    "data": hex(29 * 10**18),
                    "blockNumber": hex(9_900),
                    "transactionHash": "0x" + "b" * 64,
                }]}
            return {"result": []}
        raise AssertionError(m)

    monkeypatch.setattr(client, "_request", fake_request)
    store["last_scanned_block:bsc"] = "9800"
    txs = await client.get_incoming_transfers(ADDR, now - timedelta(hours=1))

    assert len(txs) == 1
    assert txs[0].amount == Decimal("29")
    assert txs[0].tx_hash == "0x" + "b" * 64
    # Курсор просунувся, але з перекриттям — не далі за safe_latest.
    saved = int(store["last_scanned_block:bsc"])
    assert 9_800 <= saved <= 10_000 - CFG.min_confirmations


@pytest.mark.asyncio
async def test_scan_skips_logs_older_than_since(monkeypatch):
    client, store = make_client(monkeypatch)
    client._chunk_blocks = 100
    now = datetime.now(timezone.utc)
    old_ts = int((now - timedelta(days=2)).timestamp())

    async def fake_request(method, url, **kwargs):
        m = kwargs["json"]["method"]
        if m == "eth_blockNumber":
            return {"result": hex(10_000)}
        if m == "eth_getBlockByNumber":
            return {"result": {"timestamp": hex(old_ts)}}
        if m == "eth_getLogs":
            return {"result": [{
                "topics": [TRANSFER, topic_addr("0x2222222222222222222222222222222222222222"), topic_addr(ADDR)],
                "data": hex(10**18),
                "blockNumber": hex(9_900),
                "transactionHash": "0x" + "c" * 64,
            }]}
        raise AssertionError(m)

    monkeypatch.setattr(client, "_request", fake_request)
    store["last_scanned_block:bsc"] = "9800"
    assert await client.get_incoming_transfers(ADDR, now - timedelta(hours=1)) == []


def test_normalize_hash():
    client = EvmRpcChainClient(CFG, session=None, urls=["http://a"])
    h = "d2f3" + "0" * 60
    assert client.normalize_hash(h) == "0x" + h
    assert client.normalize_hash(("0x" + h).upper()) == "0x" + h
    with pytest.raises(ValueError):
        client.normalize_hash("0xzz")
