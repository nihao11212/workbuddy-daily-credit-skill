"""WorkBuddy 每日签到自动领取脚本。

作者：88lin
仓库：https://github.com/88lin/workbuddy-auto-signin
协议：MIT

签到接口系从桌面端逆向所得，服务端改一版就可能失效——修复都会推到上面这个仓库。
顺手点个 ⭐ Star，等哪天连签莫名其妙断了，你能一秒把它找回来。

读取本机 WorkBuddy 桌面端的登录会话，调用其签到接口自动领取每日积分：
  POST {endpoint}/v2/billing/meter/checkin-activity-status  查询签到状态
  POST {endpoint}/v2/billing/meter/daily-checkin            领取今日积分

响应契约：
  - 领取成功 : 含 credit 字段，如 {"credit": 100}
  - 今日已签 : null 或 HTTP 400 + {"code":10001,"msg":"今天已签到，请明天再来"}
               幂等，两种形态都按"已签"处理，不计失败
  - 业务错误 : {"code": ..., "msg": ...}
  - 认证拒绝 : HTTP 401；HTTP 403 单独作为权限/业务拒绝处理

凭据文件由桌面端登录后自动写入；脚本按平台自动探测，或用环境变量
WORKBUDDY_AUTH_FILE 指定。任何模式下都不会打印令牌，可安全分享。

用法：
  python signin.py auto           # 每日自动化：签到 + 成长中心（礼物/任务/补登/兑换/抽奖/Buddy）
  python signin.py silent         # 同 auto，但结果写日志文件而非 stdout（配合 pythonw.exe 静默运行）
  python signin.py growth         # 仅成长中心（不签到）
  python signin.py silent-poll    # 轮询：补签（未签才签）+ 成长中心，空跑不写日志
  python signin.py silent-growth  # silent-poll 的旧名，行为完全相同（老计划任务仍可用）
  python signin.py doctor         # 离线检查凭据格式与运行时能力，不解密、不联网
  python signin.py status         # 仅查签到状态（调试）
  python signin.py claim          # 仅领取签到（调试，幂等）
  python signin.py all            # 查签到状态 + 领取（调试）
"""

import base64
import json
import math
import ntpath
import os
import plistlib
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from contextlib import closing
from datetime import date, datetime

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
# Linux 没有桌面端，入口是 CodeBuddy CLI；它写出的凭据文件名不同、放在 XDG 数据目录
# （issue #4 实测：~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info，
#  JSON 结构与桌面端一致，签到/成长中心接口全部照常工作）
CLI_AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth",
                                 "Tencent-Cloud.coding-copilot.info")

# 伪 HTTP 码：区分"没拿到响应"的两种原因
CODE_NO_NETWORK = -1   # 连不上/超时
CODE_BUDGET_OUT = -2   # 本次运行的时间预算已耗尽，主动放弃后续请求

# 计划任务跑满 ExecutionTimeLimit 会被系统直接杀掉，届时 emit 还没执行，当天日志整条
# 丢失。这里自设更小的预算，确保总能走到写日志那一步。两个任务的时限不同，上限也必须
# 分开算：签到任务 PT10M、轮询任务 PT5M，各留 60s 给解释器启动和收尾。
# 这四个常量与 install-windows.ps1 里的 ExecutionTimeLimit 一一对应，改一处就要改另一处。
DEFAULT_BUDGET_SECONDS = 420.0
MAX_BUDGET_SECONDS = 540.0          # 签到任务 PT10M = 600s - 60s
# 轮询任务如今也负责补签（见 run_daily），预算比"只跑成长中心"时期宽一些，好让
# 冷启动重试跑得完；但仍远小于 PT5M，跑不完就早收尾、四小时后再来。
POLL_BUDGET_SECONDS = 180.0
POLL_MAX_BUDGET_SECONDS = 240.0     # 轮询任务 PT5M = 300s - 60s
# 每轮最多用掉一张补登卡；仅依据服务端活跃日历中明确的当月断登日期操作。
MAKEUP_MAX_PER_RUN = 1
REQUEST_TIMEOUT = 30
# 网络类失败的退避节奏（秒）。定时任务最容易撞上的就是"刚开机/刚唤醒"：WiFi 重连、
# DHCP 续租、VPN 拨通往往要几十秒，而原来的策略是"5 秒后再试一次"——两次都撞在同
# 一堵墙上，420 秒预算只花掉 5 秒就判了当天死刑。退避到分钟级才真正跨得过这个窗口：
# 最多 6 次尝试摊开约 3.5 分钟，仍在签到任务的预算内。实际跑几轮由剩余预算决定
# （见 _request_with_retry 的守卫），轮询任务预算短，会自动少跑几轮。
NETWORK_RETRY_DELAYS = (5, 15, 30, 60, 90)
# 5xx 是服务端抖动，不是本机网络没就绪，短促重试即可——干等几分钟既救不了它，
# 还会把预算耗光，让后面的成长中心一个都跑不成。
SERVER_RETRY_DELAYS = (3, 10)
_started_at = None
_budget_seconds = DEFAULT_BUDGET_SECONDS
_config_warning = None   # 配置非法时的告警，由 emit 统一带进输出

# 轮询类命令：预算更短，且空跑不落盘。silent-growth 是 silent-poll 的旧名，
# 已安装的计划任务还在用它，必须继续认。
POLL_ACTIONS = ("silent-poll", "silent-growth")

AUTH_HELPER_TIMEOUT = 10.0
AUTH_INPUT_LIMIT = 65536
AUTH_OUTPUT_LIMIT = 65536
TOKEN_LIMIT = 32768
_sensitive_values = set()
AUTH_REASONS = {
    "INVALID_FORMAT": "登录凭据格式无效，请检查凭据来源和客户端版本",
    "UNSUPPORTED_ENVELOPE": "尚不支持此加密凭据格式，请更新脚本；重新登录不会改变加密格式",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，请用 WORKBUDDY_EXE 指定其可执行文件",
    "INVALID_RUNTIME_PATH": "WORKBUDDY_EXE 不是可用的可执行文件，请核对路径",
    "RUNTIME_UNAVAILABLE": "客户端运行时不支持所需的原生存储接口，请检查客户端和脚本版本",
    "KEY_MISMATCH": "凭据与所选客户端的密钥不匹配，请用 WORKBUDDY_EXE 指定对应客户端",
    "DECRYPT_FAILED": "加密凭据认证失败，请检查客户端版本及凭据是否完整",
    "HELPER_TIMEOUT": "凭据处理超时，已停止子进程，请稍后重试",
    "HELPER_PROTOCOL": "客户端凭据助手返回无效结果，请检查客户端和脚本版本",
}

# Fixed code only in argv; the encrypted field travels over stdin. Build material
# never leaves this process. Only sym-v1 / field / suite 1 is supported.
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


class AuthError(Exception):
    """Only fixed, non-sensitive messages may cross the credential boundary."""

    def __init__(self, reason, result="AUTH_ERROR"):
        self.reason = reason
        self.result = result
        super().__init__(AUTH_REASONS.get(reason, "本地未找到有效登录会话，请先登录客户端"))

    def output(self):
        return {"result": self.result, "reason": self.reason,
                "report": str(self), "needs_attention": True}


def _valid_token(token):
    return (isinstance(token, str) and 0 < len(token) <= TOKEN_LIMIT
            and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is not None)


def _validate_session(session):
    if not isinstance(session, dict):
        raise AuthError("INVALID_FORMAT")
    for field in ("auth", "account"):
        if session.get(field) is None:
            raise AuthError("MISSING_SESSION", "NO_SESSION")
        if not isinstance(session[field], dict):
            raise AuthError("INVALID_FORMAT")
    if session["auth"].get("accessToken") in (None, "") or session["account"].get("uid") in (None, ""):
        raise AuthError("MISSING_SESSION", "NO_SESSION")
    for source, fields in ((session["account"], ("uid", "enterpriseId")),
                           (session["auth"], ("domain",))):
        for field in fields:
            value = source.get(field)
            if value not in (None, "") and (not isinstance(value, str) or
                                      re.fullmatch(r"[\x21-\x7e]{1,2048}", value) is None):
                raise AuthError("INVALID_FORMAT")
    endpoint = session["auth"].get("endpoint")
    if endpoint in (None, ""):
        endpoint = DEFAULT_ENDPOINT
    if not isinstance(endpoint, str) or re.search(r"[\s\x00-\x1f\x7f]", endpoint):
        raise AuthError("INVALID_FORMAT")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        parsed.port  # Validate an explicitly supplied port without logging the URL.
    except ValueError:
        raise AuthError("INVALID_FORMAT") from None


def _token_format(value):
    if isinstance(value, str):
        if not _valid_token(value):
            raise AuthError("INVALID_FORMAT")
        return "plaintext"
    if not isinstance(value, dict):
        raise AuthError("INVALID_FORMAT")
    if (set(value) != {"$wbEncrypted", "envelope"} or
            type(value.get("$wbEncrypted")) is not int or value["$wbEncrypted"] != 1):
        raise AuthError("UNSUPPORTED_ENVELOPE")
    try:
        encoded = value["envelope"]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= AUTH_INPUT_LIMIT - 1024:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError()
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or type(envelope.get("suite")) is not int:
            raise ValueError()
        if envelope["suite"] != 1:
            raise AuthError("UNSUPPORTED_ENVELOPE")
        if set(envelope) != {"suite", "keyId", "nonce", "authTag", "ciphertext"}:
            raise ValueError()
        if not isinstance(envelope["keyId"], str) or not re.fullmatch(r"[0-9a-f]{16}", envelope["keyId"]):
            raise ValueError()
        for field, length in (("nonce", 12), ("authTag", 16), ("ciphertext", None)):
            data = envelope[field]
            if not isinstance(data, str):
                raise ValueError()
            decoded = base64.b64decode(data, validate=True)
            if base64.b64encode(decoded).decode("ascii") != data or (length is not None and len(decoded) != length):
                raise ValueError()
        return "sym-v1"
    except (ValueError, TypeError, KeyError):
        raise AuthError("INVALID_FORMAT") from None


def _mac_runtime(bundle):
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _registry_text(registry, key, field):
    """Read only string registry values; expand REG_EXPAND_SZ using Windows."""
    try:
        value, kind = registry.QueryValueEx(key, field)
        if not isinstance(value, str) or kind not in (registry.REG_SZ, registry.REG_EXPAND_SZ):
            return None
        if kind == registry.REG_EXPAND_SZ:
            value = registry.ExpandEnvironmentStrings(value)
        return value
    except OSError:
        return None


def _registry_runtime_path(value, icon=False):
    """Parse a directory or DisplayIcon; never interpret an uninstall command."""
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    value = value.strip()
    if value.startswith('"'):
        suffix = r"(?:\s*,\s*-?\d+)?" if icon else ""
        match = re.fullmatch(r'"([^"]+)"' + suffix, value)
        if not match:
            return None
        value = match[1]
    elif '"' in value:
        return None
    elif icon:
        # Only remove the trailing resource index; commas can belong to folders.
        value = re.sub(r",\s*-?\d+$", "", value).rstrip()
    if not ntpath.splitdrive(value)[0] or not ntpath.isabs(value):
        return None
    if not icon:
        value = ntpath.join(value, "WorkBuddy.exe")
    value = ntpath.normpath(value)
    if ntpath.basename(value).casefold() != "workbuddy.exe":
        return None
    return value


def _windows_registry_runtimes():
    """Yield existing clients from user/machine uninstall entries, lazily.

    Keep the import and all platform-specific constants inside this Windows-only
    fallback so importing the script on macOS/Linux needs no winreg module.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    seen = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for index in range(count):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index),
                                                0, winreg.KEY_READ | view) as entry:
                                name = _registry_text(winreg, entry, "DisplayName")
                                if not name or not re.fullmatch(
                                        r"WorkBuddy(?:\s+\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)?",
                                        name.strip(), re.IGNORECASE):
                                    continue
                                # A stale InstallLocation must not mask a valid DisplayIcon.
                                for field in ("InstallLocation", "DisplayIcon"):
                                    path = _registry_runtime_path(
                                        _registry_text(winreg, entry, field), icon=field == "DisplayIcon")
                                    if path and ntpath.normcase(path) not in seen:
                                        seen.add(ntpath.normcase(path))
                                        if os.path.isfile(path):
                                            yield path
                        except OSError:
                            # Deleted entries and access-denied subkeys are normal.
                            continue
            except OSError:
                continue


def find_workbuddy_runtime():
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or (os.name != "nt" and not os.access(path, os.X_OK)):
            raise AuthError("INVALID_RUNTIME_PATH")
        return path
    home = os.path.expanduser("~")
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    elif sys.platform == "darwin":
        candidates = [_mac_runtime(os.path.join(root, "WorkBuddy.app"))
                      for root in ("/Applications", os.path.join(home, "Applications"))]
    for path in candidates:
        if path and os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK)):
            return os.path.abspath(path)
    if sys.platform == "win32":
        with closing(_windows_registry_runtimes()) as entries:
            registered = next(entries, None)
        if registered:
            return registered
    raise AuthError("RUNTIME_NOT_FOUND")


def _run_auth_helper(exe, request):
    """Bound every pipe and deadline; never include captured data in exceptions."""
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthError("INVALID_FORMAT")
    timeout = min(AUTH_HELPER_TIMEOUT, _budget_left())
    if timeout <= 0:
        raise AuthError("HELPER_TIMEOUT")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.Popen([exe, "-e", AUTH_HELPER_JS], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise AuthError("RUNTIME_UNAVAILABLE") from None
    output = {}
    failed = threading.Event()

    def read_pipe(name, pipe, limit):
        try:
            captured = pipe.read(limit + 1)
            if len(captured) > limit:
                failed.set()
                process.kill()
            else:
                output[name] = captured
        except OSError:
            failed.set()

    def write_pipe():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            failed.set()

    workers = [threading.Thread(target=read_pipe, args=("stdout", process.stdout, AUTH_OUTPUT_LIMIT), daemon=True),
               threading.Thread(target=read_pipe, args=("stderr", process.stderr, 8192), daemon=True),
               threading.Thread(target=write_pipe, daemon=True)]
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AuthError("HELPER_TIMEOUT") from None
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise AuthError("HELPER_TIMEOUT")
        if failed.is_set():
            raise AuthError("HELPER_PROTOCOL")
        try:
            reply = json.loads(output.get("stdout", b"").decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AuthError("HELPER_PROTOCOL") from None
        if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != 1:
            raise AuthError("HELPER_PROTOCOL")
        if reply.get("ok") is False and process.returncode == 1:
            reason = reply.get("reason")
            raise AuthError(reason if isinstance(reason, str) and reason in AUTH_REASONS else "HELPER_PROTOCOL")
        if reply.get("ok") is not True or process.returncode != 0:
            raise AuthError("HELPER_PROTOCOL")
        if request["operation"] == "decrypt":
            if set(reply) != {"version", "ok", "accessToken"} or not _valid_token(reply.get("accessToken")):
                raise AuthError("HELPER_PROTOCOL")
        elif (set(reply) != {"version", "ok", "electron"} or not isinstance(reply.get("electron"), str)
              or re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", reply["electron"]) is None):
            raise AuthError("HELPER_PROTOCOL")
        return reply
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
        # Avoid blocking on a pipe held by an incompatible runtime's descendant.
        if not any(worker.is_alive() for worker in workers):
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def resolve_session(session):
    _validate_session(session)
    token = session["auth"]["accessToken"]
    if _token_format(token) != "plaintext":
        token = _run_auth_helper(find_workbuddy_runtime(), {"operation": "decrypt", "value": token})["accessToken"]
    _sensitive_values.add(token)
    return dict(session, auth=dict(session["auth"], accessToken=token))



def _budget_left():
    return 10.0

