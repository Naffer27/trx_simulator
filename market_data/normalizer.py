TF_MAP = {"1":"1m","1m":"1m","5":"5m","5m":"5m","60":"1h","1h":"1h","D":"1d","1d":"1d"}

def norm_symbol(s: str) -> str:
    return s.replace("/", "").upper()

def norm_tf(tf: str) -> str:
    return TF_MAP.get(str(tf), "1m")