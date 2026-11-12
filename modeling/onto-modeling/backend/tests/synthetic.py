"""测试用的合成凭据：运行时拼出来，代码里不出现口令、令牌字面量。"""


def password(label: str) -> str:
    """满足口令策略（大小写、数字、符号）的合成口令；同一个 label 总是同一个值，例如 password("admin") 是 Synthetic#Admin1。"""
    return "Synthetic#" + label[:1].upper() + label[1:] + "1"


def token(label: str) -> str:
    """合成令牌：只在测试里当不透明字符串用。"""
    return "synthetic-" + label + "-token"


def proxy_url(host: str = "proxy.example.com", port: int = 8080, scheme: str = "http") -> str:
    """带账号的合成代理地址，用来测脱敏。"""
    return f"{scheme}://user:{password('proxy')}@{host}:{port}"
