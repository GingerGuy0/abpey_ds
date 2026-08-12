import asyncio
import re
from datetime import datetime, timezone
from app.chains.base import BaseChainClient, IncomingTx, ChainUnavailable, ChainRateLimited, ChainAuthError, TxNotFound
from app.services.amounts import from_units

class EvmChainClient(BaseChainClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = "https://api.etherscan.io/v2/api"
        self._semaphore = asyncio.Semaphore(3)
        self._last_request_time = 0.0

    async def _safe_request(self, params: dict) -> dict:
        async with self._semaphore:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < 0.22:
                await asyncio.sleep(0.22 - elapsed)
            
            from app.config import settings
            params["apikey"] = settings.etherscan_api_key
            
            try:
                try:
                    data = await self._request("GET", self.base_url, params=params)
                except (ChainRateLimited, TxNotFound, ChainAuthError):
                    raise
                except Exception as e:
                    import logging
                    logging.error(f"Etherscan transport error {params.get('action')} chainid={params.get('chainid')} -> {type(e).__name__}: {e}", exc_info=True)
                    raise ChainUnavailable(f"{type(e).__name__}: {e}") from e
                
                module = params.get("module")
                if module == "proxy":
                    if "error" in data:
                        err = data["error"]
                        err_msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                        import logging
                        logging.error("Etherscan V2 %s chainid=%s -> error=%r", params.get("action"), params.get("chainid"), err_msg)
                        if "rate limit" in err_msg.lower():
                            raise ChainRateLimited(err_msg)
                        if "api key" in err_msg.lower():
                            raise ChainAuthError(err_msg)
                        raise ChainUnavailable(err_msg)
                else:
                    if data.get("status") == "0" and data.get("message") != "No transactions found":
                        msg = data.get("message", "")
                        res = data.get("result", "")
                        import logging
                        logging.error("Etherscan V2 %s chainid=%s -> status=%s message=%r result=%r", 
                                      params.get("action"), params.get("chainid"), 
                                      data.get("status"), msg, str(res)[:200])
                        msg_lower = msg.lower()
                        res_lower = str(res).lower() if res else ""
                        if "rate limit" in msg_lower or "rate limit" in res_lower:
                            raise ChainRateLimited(res or msg)
                        
                        auth_keywords = ["api key", "not supported for this chain", "upgrade your api plan"]
                        if any(k in msg_lower or k in res_lower for k in auth_keywords):
                            raise ChainAuthError(res or msg)
                            
                        raise ChainUnavailable(res or msg)
                
                return data
            finally:
                self._last_request_time = asyncio.get_event_loop().time()

    async def get_incoming_transfers(self, address: str, since: datetime, limit: int = 50) -> list[IncomingTx]:
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": self.cfg.usdt_contract,
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "chainid": self.cfg.chainid
        }
        data = await self._safe_request(params)
        
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
            
        txs = []
        for tx in result:
            to_addr = tx.get("to", "")
            if to_addr.lower() != address.lower():
                continue
                
            decimals = int(tx.get("tokenDecimal", self.cfg.decimals))
            units = int(tx.get("value", 0))
            amount = from_units(units, decimals)
            
            ts = int(tx.get("timeStamp", 0))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if dt < since:
                continue
                
            txs.append(IncomingTx(
                tx_hash=tx.get("hash", ""),
                network=self.cfg.key,
                from_address=tx.get("from", ""),
                to_address=to_addr,
                amount=amount,
                units=units,
                confirmations=int(tx.get("confirmations", 0)),
                timestamp=dt,
                is_success=True
            ))
            
        return txs

    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        params = {
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": tx_hash,
            "chainid": self.cfg.chainid
        }
        data = await self._safe_request(params)
        receipt = data.get("result")
        if not receipt:
            raise TxNotFound("Transaction not found")
            
        status = receipt.get("status")
        is_success = status == "0x1"
        
        block_params = {
            "module": "proxy",
            "action": "eth_blockNumber",
            "chainid": self.cfg.chainid
        }
        block_data = await self._safe_request(block_params)
        current_block_hex = block_data.get("result", "0x0")
        current_block = int(current_block_hex, 16)
        
        tx_block_hex = receipt.get("blockNumber", "0x0")
        tx_block = int(tx_block_hex, 16)
        confirmations = max(0, current_block - tx_block) if tx_block > 0 else 0
        
        if tx_block > 0:
            block_info_params = {
                "module": "proxy",
                "action": "eth_getBlockByNumber",
                "tag": tx_block_hex,
                "boolean": "false",
                "chainid": self.cfg.chainid
            }
            block_info_data = await self._safe_request(block_info_params)
            res = block_info_data.get("result")
            ts_hex = res.get("timestamp", "0x0") if res else "0x0"
            tx_time = datetime.fromtimestamp(int(ts_hex, 16), tz=timezone.utc)
        else:
            tx_time = datetime.now(timezone.utc)
        
        usdt_contract = self.cfg.usdt_contract.lower()
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        
        for log in receipt.get("logs", []):
            if log.get("address", "").lower() != usdt_contract:
                continue
            topics = log.get("topics", [])
            if not topics or topics[0] != transfer_topic:
                continue
            if len(topics) >= 3:
                from_addr = "0x" + topics[1][-40:]
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() == address.lower():
                    value_hex = log.get("data", "0x0")
                    units = int(value_hex, 16)
                    amount = from_units(units, self.cfg.decimals)
                    
                    return IncomingTx(
                        tx_hash=tx_hash,
                        network=self.cfg.key,
                        from_address=from_addr,
                        to_address=to_addr,
                        amount=amount,
                        units=units,
                        confirmations=confirmations,
                        timestamp=tx_time,
                        is_success=is_success
                    )
                    
        raise TxNotFound("No matching USDT transfer found in transaction")

    def normalize_hash(self, raw: str) -> str:
        raw = raw.lower()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        if not re.match(r"^0x[a-f0-9]{64}$", raw):
            raise ValueError("Invalid EVM hash format")
        return raw
