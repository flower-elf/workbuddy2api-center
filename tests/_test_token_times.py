"""令牌的签发与到期时刻：jwt_iat/jwt_exp 解析、Account.issued_at 与 public() 的 issuedAt。

面板的有效期进度按「到期 - 签发」取满格，两个时刻来源不同：到期优先读凭证文件
里刷新令牌时写回的响应值，签发时刻只能从令牌自身读。解析口径在这里锁住：
声明是秒级整数、毫秒会换算成秒、令牌不是 JWT 时留 0 而不是抛错。

不联网。
"""

import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-token-times-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A


def jwt(claims):
    """把声明打包成三段式令牌，签名段随意填。"""

    def segment(payload):
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return "%s.%s.%s" % (segment({"alg": "none", "typ": "JWT"}), segment(claims), "sig")


def account(**fields):
    data = {"uid": "uid-token", "realm": "intl"}
    data.update(fields)
    return A.Account(data)


class JwtClaimTests(unittest.TestCase):
    def test_seconds_pass_through(self):
        token = jwt({"exp": 1800000000, "iat": 1797400000})
        self.assertEqual(A.jwt_exp(token), 1800000000)
        self.assertEqual(A.jwt_iat(token), 1797400000)

    def test_broken_or_partial_tokens_read_zero(self):
        self.assertEqual(A.jwt_iat("probe-token"), 0)
        self.assertEqual(A.jwt_exp("a.b.c"), 0)
        self.assertEqual(A.jwt_exp(""), 0)
        self.assertEqual(A.jwt_iat(jwt({"exp": 1800000000})), 0)


class AccountIssuedAtTests(unittest.TestCase):
    def test_token_iat_feeds_the_view(self):
        acc = account(accessToken=jwt({"sub": "uid-token", "exp": 1800000000, "iat": 1797400000}))
        self.assertEqual(acc.issued_at, 1797400000)
        self.assertEqual(acc.expires_at, 1800000000)
        self.assertEqual(acc.public()["issuedAt"], 1797400000)

    def test_stored_value_wins_over_the_token(self):
        acc = account(accessToken=jwt({"exp": 1800000000, "iat": 1797400000}), issuedAt=1797500000)
        self.assertEqual(acc.issued_at, 1797500000)

    def test_millisecond_claims_are_converted(self):
        acc = account(accessToken=jwt({"exp": 1800000000 * 1000, "iat": 1797400000 * 1000}))
        self.assertEqual(acc.issued_at, 1797400000)
        self.assertEqual(acc.public()["issuedAt"], 1797400000)

    def test_a_plain_token_leaves_zero(self):
        acc = account(accessToken="probe-token")
        self.assertEqual(acc.issued_at, 0)
        self.assertEqual(acc.public()["issuedAt"], 0)


if __name__ == "__main__":
    unittest.main()
