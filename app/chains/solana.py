import re
from decimal import Decimal
from datetime import datetime, timezone
from app.chains.base import BaseChainClient, IncomingTx, TxNotFound, ChainUnavailable
from app.services.amounts import from_units

class SolanaChainClient(BaseChainClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from app.config import settings
        self.rpc_url = settings.solana_rpc_url

    async def get_incoming_transfers(self, address: str, since: datetime, limit: int = 50) -> list[IncomingTx]:
        payload_sigs = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                address,
                {"limit": limit, "commitment": "finalized"}
            ]
        }
        
        try:
            data_sigs = await self._request("POST", self.rpc_url, json=payload_sigs)
        except Exception as e:
            raise ChainUnavailable(str(e))
            
        sigs = data_sigs.get("result", [])
        if not isinstance(sigs, list):
            raise ChainUnavailable("Invalid response from Solana RPC")
            
        if not sigs:
            return []
            
        batch_payload = []
        for i, sig_info in enumerate(sigs):
            if sig_info.get("err"):
                continue
            sig = sig_info.get("signature")
            if not sig:
                continue
            batch_payload.append({
                "jsonrpc": "2.0",
                "id": i,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}
                ]
            })
            if len(batch_payload) >= 20:
                break
                
        if not batch_payload:
            return []
            
        try:
            data_txs = await self._request("POST", self.rpc_url, json=batch_payload)
        except Exception as e:
            raise ChainUnavailable(str(e))
            
        if not isinstance(data_txs, list):
            raise ChainUnavailable("Invalid batch response from Solana RPC")
            
        tx_results = {r.get("id"): r.get("result") for r in data_txs if isinstance(r, dict)}
        
        txs = []
        for i, sig_info in enumerate(sigs):
            if i not in tx_results:
                continue
            tx_data = tx_results[i]
            if not tx_data:
                continue
                
            sig = sig_info["signature"]
            meta = tx_data.get("meta", {})
            if meta.get("err") is not None:
                continue
                
            block_time = tx_data.get("blockTime")
            if not block_time:
                continue
            dt = datetime.fromtimestamp(block_time, tz=timezone.utc)
            if dt < since:
                continue
                
            pre_balances = meta.get("preTokenBalances", [])
            post_balances = meta.get("postTokenBalances", [])
            
            amount_diff = Decimal("0")
            units_diff = 0
            from_addr = ""
            
            # Find our receiving token account balance change
            for post_bal in post_balances:
                if post_bal.get("mint") != self.cfg.usdt_contract:
                    continue
                if post_bal.get("owner") != address:
                    continue
                    
                acct_idx = post_bal.get("accountIndex")
                post_amount_str = post_bal.get("uiTokenAmount", {}).get("amount", "0")
                post_amount = int(post_amount_str)
                
                pre_amount = 0
                for pre_bal in pre_balances:
                    if pre_bal.get("accountIndex") == acct_idx:
                        pre_amount = int(pre_bal.get("uiTokenAmount", {}).get("amount", "0"))
                        break
                        
                units = post_amount - pre_amount
                if units > 0:
                    units_diff = units
                    decimals = int(post_bal.get("uiTokenAmount", {}).get("decimals", self.cfg.decimals))
                    amount_diff = from_units(units, decimals)
                    break
                    
            if units_diff <= 0:
                continue
                
            # Try to find the sender
            for pre_bal in pre_balances:
                if pre_bal.get("mint") != self.cfg.usdt_contract:
                    continue
                acct_idx = pre_bal.get("accountIndex")
                pre_amt = int(pre_bal.get("uiTokenAmount", {}).get("amount", "0"))
                
                post_amt = 0
                for post_bal in post_balances:
                    if post_bal.get("accountIndex") == acct_idx:
                        post_amt = int(post_bal.get("uiTokenAmount", {}).get("amount", "0"))
                        break
                        
                if pre_amt - post_amt >= units_diff:
                    from_addr = pre_bal.get("owner", "")
                    break
                    
            if not from_addr:
                # Fallback to fee payer
                keys = tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])
                if keys:
                    from_addr = keys[0].get("pubkey", "")
                    
            txs.append(IncomingTx(
                tx_hash=sig,
                network=self.cfg.key,
                from_address=from_addr,
                to_address=address,
                amount=amount_diff,
                units=units_diff,
                confirmations=self.cfg.min_confirmations,
                timestamp=dt,
                is_success=True
            ))
            
        return txs

    async def get_tx_by_hash(self, tx_hash: str, address: str) -> IncomingTx:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                tx_hash,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}
            ]
        }
        
        try:
            data = await self._request("POST", self.rpc_url, json=payload)
        except Exception as e:
            raise ChainUnavailable(str(e))
            
        result = data.get("result")
        if not result:
            # Maybe not finalized yet, check if exists at all
            payload_conf = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "getTransaction",
                "params": [
                    tx_hash,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}
                ]
            }
            try:
                data_conf = await self._request("POST", self.rpc_url, json=payload_conf)
                if data_conf.get("result"):
                    # Exists but not finalized
                    result = data_conf.get("result")
                    is_finalized = False
                else:
                    raise TxNotFound("Transaction not found")
            except Exception:
                raise TxNotFound("Transaction not found")
        else:
            is_finalized = True
            
        meta = result.get("meta", {})
        is_success = meta.get("err") is None
        
        block_time = result.get("blockTime")
        if not block_time:
            dt = datetime.now(timezone.utc)
        else:
            dt = datetime.fromtimestamp(block_time, tz=timezone.utc)
            
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
        
        amount_diff = Decimal("0")
        units_diff = 0
        from_addr = ""
        
        for post_bal in post_balances:
            if post_bal.get("mint") != self.cfg.usdt_contract:
                continue
            if post_bal.get("owner") != address:
                continue
                
            acct_idx = post_bal.get("accountIndex")
            post_amount_str = post_bal.get("uiTokenAmount", {}).get("amount", "0")
            post_amount = int(post_amount_str)
            
            pre_amount = 0
            for pre_bal in pre_balances:
                if pre_bal.get("accountIndex") == acct_idx:
                    pre_amount = int(pre_bal.get("uiTokenAmount", {}).get("amount", "0"))
                    break
                    
            units = post_amount - pre_amount
            if units > 0:
                units_diff = units
                decimals = int(post_bal.get("uiTokenAmount", {}).get("decimals", self.cfg.decimals))
                amount_diff = from_units(units, decimals)
                break
                
        if units_diff <= 0:
            raise TxNotFound("No matching USDT transfer found in transaction")
            
        for pre_bal in pre_balances:
            if pre_bal.get("mint") != self.cfg.usdt_contract:
                continue
            acct_idx = pre_bal.get("accountIndex")
            pre_amt = int(pre_bal.get("uiTokenAmount", {}).get("amount", "0"))
            
            post_amt = 0
            for post_bal in post_balances:
                if post_bal.get("accountIndex") == acct_idx:
                    post_amt = int(post_bal.get("uiTokenAmount", {}).get("amount", "0"))
                    break
                    
            if pre_amt - post_amt >= units_diff:
                from_addr = pre_bal.get("owner", "")
                break
                
        if not from_addr:
            keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])
            if keys:
                from_addr = keys[0].get("pubkey", "")
                
        return IncomingTx(
            tx_hash=tx_hash,
            network=self.cfg.key,
            from_address=from_addr,
            to_address=address,
            amount=amount_diff,
            units=units_diff,
            confirmations=self.cfg.min_confirmations if is_finalized else 0,
            timestamp=dt,
            is_success=is_success
        )

    def normalize_hash(self, raw: str) -> str:
        raw = raw.strip()
        if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{86,90}$", raw):
            raise ValueError("Invalid Solana hash format")
        return raw
