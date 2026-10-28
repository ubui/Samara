"""认证：PBKDF2 加盐口令文件 + 内存会话。

口令文件格式（每行）:
  user:$pbkdf2$<迭代次数>$<salt>$<hex>     当前格式（PBKDF2-HMAC-SHA256）
  user:$sha256$<salt>$<hex>                1.1 及更早的旧格式，仍可登录，登录后自动升级

用标准库 hashlib，不依赖 crypt(3)（Python 3.13+ 已移除）。
"""

import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE_NAME = "samara_session"
PBKDF2_ROUNDS = 120000          # 约 50ms/次，兼顾暴力破解成本与登录体验


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), PBKDF2_ROUNDS)
    return f"$pbkdf2${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def _read_users(path):
    users = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                user, _, spec = line.partition(":")
                users[user] = spec.strip()
    except OSError:
        pass
    return users


def is_legacy(spec):
    """旧格式（单轮 sha256）需要升级。"""
    return bool(spec) and spec.startswith("$sha256$")


def _check(spec, password):
    parts = (spec or "").split("$")
    if len(parts) == 4 and parts[1] == "sha256":
        salt, digest = parts[2], parts[3]
        calc = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return hmac.compare_digest(calc, digest)
    if len(parts) == 5 and parts[1] == "pbkdf2":
        try:
            rounds = int(parts[2])
        except ValueError:
            return False
        if not (1000 <= rounds <= 5_000_000):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 parts[3].encode("utf-8"), rounds)
        return hmac.compare_digest(dk.hex(), parts[4])
    return False


def _rewrite_spec(path, user, new_spec):
    """原子地把某个用户的口令行换掉，保留注释与其它账号。"""
    lock = _WRITE_LOCK
    with lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f]
        except OSError:
            return False
        out, hit = [], False
        for line in lines:
            if line and not line.startswith("#") and line.split(":", 1)[0] == user:
                out.append(f"{user}:{new_spec}")
                hit = True
            else:
                out.append(line)
        if not hit:
            return False
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True
        except OSError:
            return False


_WRITE_LOCK = threading.Lock()


def verify(path, user, password, upgrade=False):
    """校验口令。upgrade=True 时，旧格式校验通过后自动改写成 PBKDF2。"""
    spec = _read_users(path).get(user)
    if not spec:
        return False
    if not _check(spec, password):
        return False
    if upgrade and is_legacy(spec):
        _rewrite_spec(path, user, hash_password(password))
    return True


class SessionStore:
    def __init__(self, ttl=86400, max_fails=5, lock_seconds=60):
        self.ttl = ttl
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._sessions = {}
        self._fails = {}
        self._lock = threading.Lock()

    def create(self, user):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {"user": user, "exp": time.time() + self.ttl}
        return token

    def get(self, token):
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if s["exp"] < time.time():
                self._sessions.pop(token, None)
                return None
            return s["user"]

    def drop(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def locked(self, ip):
        with self._lock:
            rec = self._fails.get(ip)
            if not rec:
                return 0
            if rec["until"] > time.time():
                return int(rec["until"] - time.time())
            return 0

    def note_fail(self, ip):
        with self._lock:
            rec = self._fails.get(ip) or {"count": 0, "until": 0}
            rec["count"] += 1
            if rec["count"] >= self.max_fails:
                rec["until"] = time.time() + self.lock_seconds
                rec["count"] = 0
            self._fails[ip] = rec

    def note_success(self, ip):
        with self._lock:
            self._fails.pop(ip, None)


def ensure_password_file(path, user="root", password="root"):
    """安装时调用：文件不存在则写入默认账号；已存在时只纠正权限。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        # 老版本写入的文件是 644，同机任何用户都能读到哈希，这里顺手收回来
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write("# samara 登录账号: <user>:$pbkdf2$<迭代>$<salt>$<hash>\n")
        f.write(f"{user}:{hash_password(password)}\n")
    os.chmod(path, 0o600)
    return True


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "/tmp/samara-passwd"
    ensure_password_file(p)
    print("written:", p)
    print("verify root/root:", verify(p, "root", "root"))
    print("verify root/bad:", verify(p, "root", "bad"))


def set_password(path, user, password):
    """修改口令：账号存在则替换这一行，不存在则追加。返回 (ok, msg)。"""
    if not password or len(password) < 1:
        return False, "口令不能为空"
    spec = hash_password(password)
    if _rewrite_spec(path, user, spec):
        return True, "口令已更新"
    # 账号不存在：追加一行
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{user}:{spec}\n")
        os.chmod(path, 0o600)
        return True, "账号已创建"
    except OSError as e:
        return False, str(e)


def clear(self):
    """清空全部登录会话。"""
    with self._lock:
        self._sessions.clear()
        self._fails.clear()
