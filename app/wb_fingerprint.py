"""wb_fingerprint.py —— 设备指纹稳定派生模块 (derive_id)

基于账号 UID 与加盐哈希派生固定的伪设备特征 (machineId, sessionId, reqId)：
同一账号每次都来自同一台设备，多账号之间互相隔离，避免上游风控关联。
"""
import hashlib
import time


def derive_id(uid: str, salt: str) -> str:
    """由 uid 与 salt 稳定派生 32 位十六进制设备/会话标识；同一账号每次结果相同，避免随机机器码触发上游风控。"""
    seed = f"{salt}:{uid or 'anonymous'}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:32]


def generate_request_id(uid: str) -> str:
    """生成带稳定前缀与微秒时间戳的防风控 X-Request-ID。"""
    prefix = derive_id(uid, "req")
    suffix = str(time.time_ns() % 1000000).zfill(6)
    return f"{prefix}-{suffix}"
