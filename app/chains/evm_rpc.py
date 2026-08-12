"""EVM-клієнт поверх публічних JSON-RPC нод (без Etherscan API).

Причина існування: Etherscan V2 прибрав BNB Smart Chain з безкоштовного тарифу
("Free API access is not supported for this chain"), тож дані по BSC беремо
напряму з нод.

Два принципи, на яких тримається цей файл:

1. Помилка ноди (таймаут, 5xx, не-JSON) і помилка запиту (некоректні параметри,
   завеликий діапазон) — це РІЗНІ речі. Першу лікуємо перемиканням на іншу ноду,
   другу — зміною самого запиту. Якщо їх плутати, один поганий запит вибиває
   з ладу всі ноди одразу.
2. Ліміти eth_getLogs у публічних нод недокументовані й різні. Тому розмір
   вікна підбирається на льоту: при "limit exceeded" він ділиться навпіл,
   а робоче значення запам'ятовується до кінця життя процесу.
"""

import logging
import re
import time
from datetime import datetime, timezone

from app.chains.base import BaseChainClient, IncomingTx, ChainError, ChainUnavailable, TxNotFound
from app.config import INVOICE_TTL_MINUTES, NetworkConfig, settings
from app.db.session import async_session
from app.services import repo
from app.services.amounts import from_units

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Маркери того, що нода відмовила саме через розмір вікна eth_getLogs, а не
# через власну несправність. Формулювання різняться від клієнта до клієнта.
_RANGE_ERROR_MARKERS = (
    "limit exceeded",
    "query returned more than",
    "block range",
    "range is too large",
    "too many blocks",
    "exceed maximum block range",
    "response size exceeded",
    "query timeout exceeded",
)

# Скільки блоків перескановувати повторно. Дешево (ProcessedTx усе одно
# дедуплікує), але рятує від втрати платежу, якщо процес упав між скануванням
# і зарахуванням інвойса.
RESCAN_OVERLAP_BLOCKS = 30

# Скільки вікон максимум обробити за один виклик, щоб не заблокувати планувальник.
MAX_CHUNKS_PER_CALL = 8

NODE_COOLDOWN_SEC = 60.0
MIN_CHUNK_BLOCKS = 32

# Наскільки довго не питати логи в ноди, яка відмовила навіть на мінімальному
# вікні. Довго — бо це не збій, а політика ноди; але не назавжди, бо політика
# може змінитись, а список нод — оновитись.
LOGS_BLOCK_SEC = 1800.0


class RpcQueryError(ChainError):
    """JSON-RPC відповів помилкою. Проблема в запиті, не в ноді."""


class RpcRangeError(RpcQueryError):
    """Нода відмовила через розмір/вартість запиту eth_getLogs."""


class LogsUnsupported(ChainError):
    """Жодна з нод не віддає eth_getLogs. Перевірка за хешем при цьому працює."""


class EvmRpcChainClient(BaseChainClient):
    def __init__(self, cfg: NetworkConfig, session, urls: list[str]):
        super().__init__(cfg, session)
        self.urls = list(urls)
        self.current_idx = 0
        self.cooldowns: dict[str, float] = {url: 0.0 for url in self.urls}
        # Публічні ноди BNB Chain (bsc-dataseed*) віддають "limit exceeded" на
        # eth_getLogs за будь-якого діапазону — вони просто не обслуговують цей
        # метод. Такі ноди позначаємо окремо: для receipt/blockNumber вони й далі
        # придатні, а от логи в них питати марно.
        self.logs_blocked_until: dict[str, float] = {url: 0.0 for url in self.urls}
        self._block_ts_cache: dict[int, datetime] = {}
        self._chunk_blocks = max(MIN_CHUNK_BLOCKS, settings.rpc_logs_chunk_blocks)
        self._logs_warned_at = 0.0

    # ---------- транспорт ----------

    async def _rpc_call(self, method: str, params: list, skip: set[str] | None = None):
        """Один JSON-RPC виклик із перемиканням між нодами.

        Кулдаун вішається ТІЛЬКИ на несправну ноду. Якщо нода відповіла
        валідною JSON-RPC помилкою — вона жива, але цей запит їй не подобається:
        пробуємо ту саму дію на інших нодах, і лише коли відмовили всі,
        віддаємо помилку нагору.
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        skip = skip or set()
        failures: list[str] = []
        range_failed: list[str] = []
        last_query_error: RpcQueryError | None = None

        for _ in range(len(self.urls)):
            url = self.urls[self.current_idx]
            self.current_idx = (self.current_idx + 1) % len(self.urls)

            if url in skip or time.time() < self.cooldowns[url]:
                continue

            try:
                data = await self._request("POST", url, json=payload)
            except Exception as e:
                logging.warning("RPC %s: нода %s недоступна (%s: %s)", method, url, type(e).__name__, e)
                failures.append(f"{url}: {type(e).__name__}")
                self.cooldowns[url] = time.time() + NODE_COOLDOWN_SEC
                continue

            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                err_msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                low = err_msg.lower()
                if any(marker in low for marker in _RANGE_ERROR_MARKERS):
                    logging.debug("RPC %s: нода %s відмовила по розміру запиту: %s", method, url, err_msg)
                    range_failed.append(url)
                    last_query_error = RpcRangeError(err_msg)
                else:
                    logging.warning("RPC %s: нода %s -> %s", method, url, err_msg)
                    last_query_error = RpcQueryError(err_msg)
                continue

            if not isinstance(data, dict) or "result" not in data:
                logging.warning("RPC %s: нода %s віддала відповідь без result: %.200s", method, url, str(data))
                failures.append(f"{url}: no result")
                self.cooldowns[url] = time.time() + NODE_COOLDOWN_SEC
                continue

            # Запит пройшов на цій ноді, але інші щойно відмовили по розміру —
            # значить це їхня політика, а не наш запит. Більше не питаємо в них
            # логи, поки не мине LOGS_BLOCK_SEC.
            if method == "eth_getLogs" and range_failed:
                until = time.time() + LOGS_BLOCK_SEC
                for bad in range_failed:
                    self.logs_blocked_until[bad] = until
                logging.info("%s: %s віддає eth_getLogs; %s — ні, виключаю їх зі сканування",
                             self.cfg.key, url, ", ".join(range_failed))

            return data["result"]

        if last_query_error is not None:
            raise last_query_error

        logging.error("RPC %s: жодна з %d нод не відповіла (%s)", method, len(self.urls),
                      "; ".join(failures) or "усі в кулдауні або пропущені")
        raise ChainUnavailable(f"Усі RPC-ноди {self.cfg.key} недоступні: {'; '.join(failures) or 'кулдаун'}")

    async def _block_number(self) -> int:
        return int(await self._rpc_call("eth_blockNumber", []), 16)

    async def _block_timestamp(self, block_number: int) -> datetime:
        cached = self._block_ts_cache.get(block_number)
        if cached is not None:
            return cached

        block = await self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(block, dict) or not block.get("timestamp"):
            # Без часу блоку інвойс не пройде перевірку вікна — краще чесно впасти,
            # ніж підставити now() і зарахувати чужий старий платіж.
            raise ChainUnavailable(f"Нода не віддала блок {block_number}")

        ts = datetime.fromtimestamp(int(block["timestamp"], 16), tz=timezone.utc)
        if len(self._block_ts_cache) > 5000:
            self._block_ts_cache.clear()
        self._block_ts_cache[block_number] = ts
        return ts

    # ---------- перевірка конкретного хеша ----------

    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        receipt = await self._rpc_call("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            raise TxNotFound("Транзакція ще не в блоці або хеш не з цієї мережі")

        is_success = receipt.get("status") == "0x1"
        tx_block = int(receipt.get("blockNumber") or "0x0", 16)
        current_block = await self._block_number()
        confirmations = max(0, current_block - tx_block) if tx_block > 0 else 0
        tx_time = await self._block_timestamp(tx_block) if tx_block > 0 else datetime.now(timezone.utc)

        usdt_contract = self.cfg.usdt_contract.lower()
        target = address.lower()

        total_units = 0
        from_addr = ""
        for log in receipt.get("logs") or []:
            if (log.get("address") or "").lower() != usdt_contract:
                continue
            topics = log.get("topics") or []
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            if ("0x" + topics[2][-40:]).lower() != target:
                continue
            # Одна транзакція може містити кілька Transfer на нашу адресу
            # (роутери, батчі) — рахуємо суму, а не перший-ліпший лог.
            total_units += int(log.get("data") or "0x0", 16)
            from_addr = from_addr or ("0x" + topics[1][-40:])

        if total_units == 0:
            raise TxNotFound("У транзакції немає USDT-переказу на нашу адресу")

        return IncomingTx(
            tx_hash=tx_hash,
            network=self.cfg.key,
            from_address=from_addr,
            to_address=address,
            amount=from_units(total_units, self.cfg.decimals),
            units=total_units,
            confirmations=confirmations,
            timestamp=tx_time,
            is_success=is_success,
        )

    # ---------- сканування гаманця ----------

    def _initial_lookback_blocks(self) -> int:
        """Скільки блоків дивитися назад при найпершому скануванні."""
        window_sec = (INVOICE_TTL_MINUTES + 30) * 60
        blocks = int(window_sec / max(self.cfg.block_time_sec, 0.1))
        return max(MIN_CHUNK_BLOCKS, min(blocks, 20000))

    def _logs_capable_urls(self) -> set[str]:
        now = time.time()
        return {u for u in self.urls if now >= self.logs_blocked_until[u]}

    def _mark_logs_unsupported(self, err: Exception) -> None:
        """Ноди відмовляють на eth_getLogs навіть при мінімальному вікні."""
        until = time.time() + LOGS_BLOCK_SEC
        for url in self._logs_capable_urls():
            self.logs_blocked_until[url] = until
        self._warn_logs_unavailable(err)

    def _warn_logs_unavailable(self, err: Exception) -> None:
        """Одне зрозуміле попередження на годину замість трейсбека кожні 40 с."""
        now = time.time()
        if now - self._logs_warned_at < 3600:
            return
        self._logs_warned_at = now
        logging.warning(
            "%s: автоматичне сканування гаманця вимкнено — жодна нода зі списку не "
            "обслуговує eth_getLogs (%s). Перевірка платежу за хешем працює. "
            "Щоб увімкнути автопошук, додай у %s_RPC_URLS ноду з підтримкою логів. "
            "Наступна спроба через %d хв.",
            self.cfg.key, err, self.cfg.key.upper(), int(LOGS_BLOCK_SEC / 60),
        )

    async def _get_logs(self, address: str, from_block: int, to_block: int) -> list[dict]:
        """Один eth_getLogs по нодах, які ще не позначені як «без логів»."""
        capable = self._logs_capable_urls()
        if not capable:
            raise LogsUnsupported("усі ноди позначені як такі, що не віддають eth_getLogs")

        topic_to = "0x" + address[2:].rjust(64, "0").lower()
        params = [{
            "address": self.cfg.usdt_contract.lower(),
            "topics": [TRANSFER_TOPIC, None, topic_to],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]
        logs = await self._rpc_call("eth_getLogs", params, skip=set(self.urls) - capable)
        return logs if isinstance(logs, list) else []

    async def get_incoming_transfers(self, address: str, since: datetime, limit: int = 50) -> list[IncomingTx]:
        current_block = await self._block_number()
        safe_latest = current_block - self.cfg.min_confirmations
        if safe_latest <= 0:
            return []

        setting_key = f"last_scanned_block:{self.cfg.key}"
        async with async_session() as db:
            raw = await repo.get_setting(db, setting_key)

        try:
            from_block = int(raw) + 1 if raw else 0
        except (TypeError, ValueError):
            from_block = 0

        if from_block <= 0:
            from_block = max(1, safe_latest - self._initial_lookback_blocks())

        # Захист від «нода відкотилась назад» і від задавненого курсора.
        max_backlog = self._initial_lookback_blocks() * 4
        if safe_latest - from_block > max_backlog:
            logging.warning(
                "%s: курсор відстав на %d блоків, стрибаємо вперед до %d",
                self.cfg.key, safe_latest - from_block, safe_latest - max_backlog,
            )
            from_block = safe_latest - max_backlog
        if from_block > safe_latest:
            return []

        txs: list[IncomingTx] = []
        cursor = from_block
        chunks = 0

        while cursor <= safe_latest and chunks < MAX_CHUNKS_PER_CALL:
            to_block = min(cursor + self._chunk_blocks - 1, safe_latest)
            try:
                logs = await self._get_logs(address, cursor, to_block)
            except RpcRangeError as e:
                # Сюди потрапляємо, лише коли ВСІ придатні ноди відмовили на
                # цьому вікні (перебір нод робить _rpc_call).
                if self._chunk_blocks > MIN_CHUNK_BLOCKS:
                    self._chunk_blocks = max(MIN_CHUNK_BLOCKS, self._chunk_blocks // 2)
                    logging.warning("%s: ноди обмежили діапазон (%s), зменшую вікно до %d блоків",
                                    self.cfg.key, e, self._chunk_blocks)
                    continue  # той самий cursor, менше вікно
                # Вікно вже мінімальне, а відмова та сама — значить справа не в
                # розмірі: ці ноди просто не обслуговують eth_getLogs.
                self._mark_logs_unsupported(e)
                return txs
            except LogsUnsupported as e:
                self._warn_logs_unavailable(e)
                return txs

            for log in logs:
                tx = await self._log_to_tx(log, address, current_block, since)
                if tx is not None:
                    txs.append(tx)

            cursor = to_block + 1
            chunks += 1

        # Курсор рухаємо з перекриттям: краще перескати кілька блоків двічі
        # (ProcessedTx дедуплікує), ніж проґавити платіж через падіння процесу.
        new_cursor = max(from_block - 1, cursor - 1 - RESCAN_OVERLAP_BLOCKS)
        async with async_session() as db:
            await repo.set_setting(db, setting_key, str(new_cursor))

        return txs

    async def _log_to_tx(self, log: dict, address: str, current_block: int, since: datetime) -> IncomingTx | None:
        topics = log.get("topics") or []
        if len(topics) < 3:
            return None

        units = int(log.get("data") or "0x0", 16)
        if units == 0:
            return None

        block_number = int(log.get("blockNumber") or "0x0", 16)
        if block_number == 0:
            return None

        tx_time = await self._block_timestamp(block_number)
        if tx_time < since:
            return None

        return IncomingTx(
            tx_hash=log.get("transactionHash", ""),
            network=self.cfg.key,
            from_address="0x" + topics[1][-40:],
            to_address=address,
            amount=from_units(units, self.cfg.decimals),
            units=units,
            confirmations=max(0, current_block - block_number),
            timestamp=tx_time,
            is_success=True,
        )

    def normalize_hash(self, raw: str) -> str:
        raw = raw.lower().strip()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        if not re.match(r"^0x[a-f0-9]{64}$", raw):
            raise ValueError("Invalid EVM hash format")
        return raw
