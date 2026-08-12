import re

def extract_hash_from_input(raw: str) -> str:
    raw = raw.strip(' \n\r\t\'"')
    patterns = [
        r"bscscan\.com/tx/([a-zA-Z0-9x]+)",
        r"etherscan\.io/tx/([a-zA-Z0-9x]+)",
        r"tronscan\.org/#/transaction/([a-zA-Z0-9]+)",
        r"tonviewer\.com/transaction/([a-zA-Z0-9_\-]+)",
        r"tonscan\.org/tx/([a-zA-Z0-9_\-]+)",
        r"solscan\.io/tx/([a-zA-Z0-9]+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1)
    
    # If no URL pattern matched, assume it's a bare hash
    # Remove some potential URL prefixes just in case
    raw = raw.split("/")[-1]
    raw = raw.split("?")[0]
    return raw
