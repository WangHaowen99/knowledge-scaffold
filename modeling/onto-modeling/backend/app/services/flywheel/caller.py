"""Who is driving the flywheel, and with which credentials.

In a user's request both credentials are that user's login. In a background round the evaluation credential
is the delegation registered there and Hub's is the publication registration (hub_delegation), or None: the
step needing it simply does not start.
"""
from dataclasses import dataclass, field
import time

# 自动步骤需要的最短剩余登录时长：发布要连续调用 Hub 三次
FRESH_SECONDS = 120


@dataclass
class Caller:
    manager: object
    pid: str
    subject: str
    username: str
    token: object
    can_write: bool
    assistant: object
    expires_at: float = 0.0
    hub_token: str = ''
    # 后台轮次发布用的 Hub 登记：{'token_id', 'issuer', 'sub'}，见 hub_packages.publish(delegated=)
    hub_delegation: dict = None
    # 用户这次请求转交的刷新令牌：只用来在评测服务登记或发起评测，不保存
    refresh: str = field(default='', repr=False)

    @property
    def present(self):
        """The user is here with a login, which registering a delegation requires."""
        return isinstance(self.token, str) and bool(self.token)

    @property
    def can_publish(self):
        return bool(self.hub_token or self.hub_delegation)

    def fresh(self):
        """Whether the login lasts long enough for an automatic step; otherwise the next sync starts it."""
        return not self.expires_at or self.expires_at - time.time() >= FRESH_SECONDS
