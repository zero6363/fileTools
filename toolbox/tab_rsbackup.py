# -*- coding: utf-8 -*-
"""⑰ ReSoBackup 备份（迁移自 Go 版 ReSoBackup，Python 重实现）
Reed-Solomon 纠删码分片 + 可选 AES-256-GCM 加密（scrypt KDF）
分片格式：31 字节头（magic RSBK）+ 数据；新备份在数据尾部附带 .rsmeta 副本"""
import hashlib
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .common import (clean_path, clear_log, form_label, form_note,
                     get_unique_path, make_form, make_log_panel, make_num_row,
                     make_tip, run_task)
from .theme import COLOR


# ⑰ ReSoBackup 备份（迁移自 Go 版 ReSoBackup，Python 重实现）
# Reed-Solomon 纠删码分片 + 可选 AES-256-GCM 加密（scrypt KDF）
# 分片格式：31 字节头（magic RSBK）+ 数据；新备份在数据尾部附带 .rsmeta 副本
# ============================================================

RS_SHARD_MAGIC = b"RSBK"
RS_SHARD_HEADER_SIZE = 31
# 分片尾部附加的元数据副本（31 字节固定头没有空间，故放在数据之后）：
# [rsmeta JSON][sha256(JSON) 32B][JSON 长度 4B][magic "RSMR" 4B]
RS_META_TRAILER_MAGIC = b"RSMR"
RS_META_TRAILER_OVERHEAD = 4 + 4 + 32
RS_MIN_SHARES, RS_MAX_SHARES = 3, 128
RS_DEFAULT_SHARES, RS_DEFAULT_THRESHOLD = 8, 5
RS_KDF_N, RS_KDF_R, RS_KDF_P, RS_KDF_KEYLEN = 32768, 8, 1, 32

# ---- GF(2^8) 运算（本原多项式 0x11d，与 klauspost/reedsolomon 一致） ----
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _gf_div(a, b):
    if b == 0:
        raise ZeroDivisionError("GF division by zero")
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] - _GF_LOG[b]) % 255]


def _gf_pow(a, n):
    if n == 0:
        return 1
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] * n) % 255]


def _gf_inv(a):
    if a == 0:
        raise ZeroDivisionError("GF inverse of zero")
    return _GF_EXP[(255 - _GF_LOG[a]) % 255]


def _vandermonde(rows, cols):
    """result[r][c] = r^c in GF(2^8)"""
    return [[_gf_pow(r, c) for c in range(cols)] for r in range(rows)]


def _mat_mul(a, b):
    n, m, p = len(a), len(b), len(b[0])
    out = [[0] * p for _ in range(n)]
    for i in range(n):
        ai = a[i]
        oi = out[i]
        for k in range(m):
            v = ai[k]
            if v == 0:
                continue
            bk = b[k]
            for j in range(p):
                v2 = bk[j]
                if v2:
                    oi[j] ^= _gf_mul(v, v2)
    return out


def _mat_inv(m):
    """高斯消元求逆（GF(2^8)）"""
    n = len(m)
    aug = [row[:] + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        piv = None
        for r in range(col, n):
            if aug[r][col] != 0:
                piv = r
                break
        if piv is None:
            raise ValueError("matrix is singular")
        aug[col], aug[piv] = aug[piv], aug[col]
        inv = _gf_inv(aug[col][col])
        aug[col] = [_gf_mul(v, inv) for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                f = aug[r][col]
                ar = aug[r]
                ac = aug[col]
                for c in range(2 * n):
                    if ac[c]:
                        ar[c] ^= _gf_mul(f, ac[c])
    return [row[n:] for row in aug]


def _rs_build_matrix(total_shares, data_shares):
    """编码矩阵：顶部 k 行单位矩阵，下部为校验行（klauspost buildMatrix 等价）"""
    vm = _vandermonde(total_shares, data_shares)
    top = [row[:] for row in vm[:data_shares]]
    top_inv = _mat_inv(top)
    return _mat_mul(vm, top_inv)


def _xor_bytes(a, b):
    n = max(len(a), len(b))
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")


def _gf_scale_vec(coef, vec):
    """coef * vec（GF 字节向量数乘）"""
    if coef == 0:
        return bytes(len(vec))
    if coef == 1:
        return vec
    table = bytes(_gf_mul(coef, x) for x in range(256))
    return vec.translate(table)


def rs_encode(data, total_shares, threshold):
    """把 data 切成 total_shares 份，任意 threshold 份可恢复。
    返回 [shard0..shardN-1]（前 threshold 份为数据片，其余为校验片）。"""
    if not 1 <= threshold <= total_shares:
        raise ValueError("threshold must be in [1, total_shares]")
    parity = total_shares - threshold
    if len(data) == 0:
        return [b""] * total_shares
    shard_size = (len(data) + threshold - 1) // threshold
    padded = data + bytes(shard_size * threshold - len(data))
    data_shards = [padded[i * shard_size:(i + 1) * shard_size] for i in range(threshold)]
    parity_shards = [bytes(shard_size) for _ in range(parity)]
    if parity:
        matrix = _rs_build_matrix(total_shares, threshold)
        for i in range(parity):
            row = matrix[threshold + i]
            acc = bytes(shard_size)
            for j in range(threshold):
                if row[j]:
                    acc = _xor_bytes(acc, _gf_scale_vec(row[j], data_shards[j]))
            parity_shards[i] = acc
    return data_shards + parity_shards


def rs_reconstruct(shards, total_shares, threshold, payload_size):
    """擦除恢复：shards 为长度 total_shares 的列表，缺失位置为 None。
    凑够 threshold 份即可还原，返回原始 payload。"""
    parity = total_shares - threshold
    matrix = _rs_build_matrix(total_shares, threshold)
    present = [i for i in range(total_shares) if shards[i] is not None]
    if len(present) < threshold:
        raise ValueError(f"not enough shares: have {len(present)}, need {threshold}")
    present = present[:threshold]
    sub = [matrix[i] for i in present]           # k x k 子矩阵
    sub_inv = _mat_inv(sub)
    shard_size = len(shards[present[0]])
    data_shards = []
    for i in range(threshold):
        acc = bytes(shard_size)
        row = sub_inv[i]
        for j, idx in enumerate(present):
            if row[j]:
                acc = _xor_bytes(acc, _gf_scale_vec(row[j], shards[idx]))
        data_shards.append(acc)
    payload = b"".join(data_shards)
    if payload_size is not None and len(payload) > payload_size:
        payload = payload[:payload_size]
    return payload


def _rs_scrypt(password, salt, n=RS_KDF_N, r=RS_KDF_R, p=RS_KDF_P, keylen=RS_KDF_KEYLEN):
    import hashlib
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=256 * 1024 * 1024, dklen=keylen)


def _rs_aes_gcm_encrypt(key, plaintext):
    from Crypto.Cipher import AES
    nonce = os.urandom(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + ct + tag


def _rs_aes_gcm_decrypt(key, blob):
    from Crypto.Cipher import AES
    if len(blob) < 12 + 16:
        raise ValueError("ciphertext too short")
    nonce, body = blob[:12], blob[12:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(body[:-16], body[-16:])


def _b64_raw_url_encode(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64_raw_url_decode(s):
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def rs_sanitize_filename(name):
    replacer = {ord(c): "_" for c in '/\\:*?"<>| '}
    return name.translate(replacer)


def rs_safe_name(s):
    return s.rstrip("=").replace("/", "_").replace("+", "-")


def rs_meta_blob(meta):
    """把 rsmeta 压成紧凑 JSON 字节，用于内嵌进分片"""
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def rs_wrap_shard(batch_id, index, total_shares, threshold, data, meta_blob=None):
    """31 字节头 + 数据（大端）；meta_blob 存在时在数据后附加元数据副本"""
    header = bytearray(RS_SHARD_HEADER_SIZE)
    header[0:4] = RS_SHARD_MAGIC
    header[4] = 1
    header[5:7] = total_shares.to_bytes(2, "big")
    header[7:9] = threshold.to_bytes(2, "big")
    header[9:11] = index.to_bytes(2, "big")
    header[11:27] = batch_id
    header[27:31] = len(data).to_bytes(4, "big")
    out = bytes(header) + data
    if meta_blob:
        out += meta_blob + hashlib.sha256(meta_blob).digest() \
            + len(meta_blob).to_bytes(4, "big") + RS_META_TRAILER_MAGIC
    return out


def _rs_read_embedded_meta(b, data_size):
    """从分片文件字节尾部解析内嵌元数据；没有或校验失败返回 None"""
    end = len(b)
    if end < RS_SHARD_HEADER_SIZE + data_size + RS_META_TRAILER_OVERHEAD + 1:
        return None
    if b[end - 4:end] != RS_META_TRAILER_MAGIC:
        return None
    meta_len = int.from_bytes(b[end - 8:end - 4], "big")
    start = end - RS_META_TRAILER_OVERHEAD - meta_len
    if meta_len <= 0 or start < RS_SHARD_HEADER_SIZE + data_size:
        return None
    blob = b[start:start + meta_len]
    if hashlib.sha256(blob).digest() != b[start + meta_len:start + meta_len + 32]:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        return None


def rs_unwrap_shard(path):
    """解析分片文件，返回 (index, data, header_dict)；失败抛异常

    header_dict["embedded_meta"] 为分片尾部内嵌的 rsmeta（无则 None）。"""
    with open(path, "rb") as f:
        b = f.read()
    if len(b) < RS_SHARD_HEADER_SIZE:
        raise ValueError("unexpected EOF")
    if b[:4] != RS_SHARD_MAGIC:
        raise ValueError("invalid shard magic")
    total = int.from_bytes(b[5:7], "big")
    threshold = int.from_bytes(b[7:9], "big")
    index = int.from_bytes(b[9:11], "big")
    batch_id = b[11:27]
    data_size = int.from_bytes(b[27:31], "big")
    if len(b) < RS_SHARD_HEADER_SIZE + data_size:
        raise ValueError("unexpected EOF")
    return index, b[RS_SHARD_HEADER_SIZE:RS_SHARD_HEADER_SIZE + data_size], {
        "total_shares": total, "threshold": threshold, "batch_id": batch_id,
        "embedded_meta": _rs_read_embedded_meta(b, data_size)}


def rs_discover_set(input_path):
    """从任意 .rs.NNN 或 .rsmeta 路径定位同目录的元数据，返回 (meta_path|None, prefix, dir)

    只扫描所选文件所在的目录，不递归子目录，也不去上级目录查找。
    .rsmeta 丢失时 meta_path 返回 None；新备份的分片尾部自带元数据副本，
    仍可完整恢复（含加密备份）。"""
    p = os.path.abspath(str(input_path or "").strip().strip('"'))
    if not os.path.isfile(p):
        raise FileNotFoundError(f"文件不存在: {p}")
    directory = os.path.dirname(p)
    base = os.path.basename(p)
    if base.endswith(".rsmeta"):
        return p, base[:-len(".rsmeta")], directory
    idx = base.rfind(".rs.")
    if idx < 0 or not base[idx + 4:].isdigit():
        raise ValueError("请选择 .rs.NNN 分片或 .rsmeta 文件")
    prefix = base[:idx]
    meta_path = os.path.join(directory, prefix + ".rsmeta")
    return (meta_path if os.path.exists(meta_path) else None), prefix, directory


def rs_collect_shard_groups(prefix, directory):
    """只扫描指定目录里的同前缀分片，按 batch_id 分组（不递归子目录）。

    返回 {batch_id: {"total", "threshold", "shards", "count", "embedded_meta"}}"""
    groups = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return groups
    for name in names:
        if not name.startswith(prefix + ".rs.") or not name[len(prefix) + 4:].isdigit():
            continue
        path = os.path.join(directory, name)
        try:
            idx, data, hdr = rs_unwrap_shard(path)
        except Exception:
            continue
        total, threshold, batch_id = hdr["total_shares"], hdr["threshold"], hdr["batch_id"]
        if not 1 <= threshold <= total:
            continue
        group = groups.get(batch_id)
        if group is None:
            group = groups[batch_id] = {"total": total, "threshold": threshold,
                                        "shards": [None] * total, "count": 0,
                                        "embedded_meta": None}
        if group["total"] != total or group["threshold"] != threshold:
            continue
        if 0 <= idx < total and group["shards"][idx] is None:
            group["shards"][idx] = data
            group["count"] += 1
        # 内嵌元数据必须与分片头属于同一批次，避免把别的备份的元数据当成本组
        em = hdr.get("embedded_meta")
        if em and group["embedded_meta"] is None:
            if (em.get("shares") == total and em.get("threshold") == threshold
                    and em.get("batch_id")):
                try:
                    em_batch = _b64_raw_url_decode(em["batch_id"])
                except Exception:
                    em_batch = None
                if em_batch == batch_id:
                    group["embedded_meta"] = em
    return groups


def rs_backup_warnings(shares, threshold):
    """与 Go 版 BackupWarnings 一致的风险提示"""
    warnings = []
    parity = shares - threshold
    if parity < 2 or parity * 100 < shares * 15:
        warnings.append(
            f"low redundancy: only {parity} redundant share(s), so losing more than "
            f"{parity} share(s) will make recovery impossible")
    expansion = shares / threshold
    if expansion > 2.0:
        warnings.append(
            "high redundancy: total stored share data will be about "
            f"{expansion:.2f}x the encrypted payload size")
    return warnings


def rs_run_backup(input_path, shares, threshold, password, output_dir,
                  encrypt, encrypt_filename, log):
    """执行备份，返回 (meta_path, prefix)。所有异常直接抛出。"""
    if not os.path.isfile(input_path):
        raise ValueError(f"input file not found: {input_path}")
    if not RS_MIN_SHARES <= shares <= RS_MAX_SHARES:
        raise ValueError(f"--shares must be in range [{RS_MIN_SHARES}, {RS_MAX_SHARES}]")
    if not 1 <= threshold <= shares:
        raise ValueError("--threshold must be >= 1 and <= shares")
    if encrypt and not password:
        raise ValueError("password is required when encryption is enabled")
    if encrypt_filename and not encrypt:
        raise ValueError("encrypt-filename requires encrypt")

    with open(input_path, "rb") as f:
        plain = f.read()

    out = output_dir or os.path.dirname(os.path.abspath(input_path))
    os.makedirs(out, exist_ok=True)
    original_name = os.path.basename(input_path)

    salt = b""
    master_key = None
    encrypted_name = ""
    if encrypt:
        salt = os.urandom(16)
        log("派生密钥（scrypt N=32768，约 0.1-0.5 秒）...")
        master_key = _rs_scrypt(password, salt)
        if encrypt_filename:
            encrypted_name = _b64_raw_url_encode(_rs_aes_gcm_encrypt(master_key, original_name.encode("utf-8")))
            prefix = rs_safe_name(encrypted_name)
        else:
            prefix = rs_sanitize_filename(original_name)
    else:
        prefix = rs_sanitize_filename(original_name)

    batch_id = os.urandom(16)
    payload = _rs_aes_gcm_encrypt(master_key, plain) if encrypt else plain

    log(f"Reed-Solomon 编码：{len(payload)} 字节 -> {shares} 份（任意 {threshold} 份可恢复）...")
    shards = rs_encode(payload, shares, threshold)

    meta = {
        "version": 2,
        "batch_id": _b64_raw_url_encode(batch_id),
        "salt": _b64_raw_url_encode(salt),
        "kdf": {"name": "scrypt", "n": RS_KDF_N, "r": RS_KDF_R, "p": RS_KDF_P,
                "key_len": RS_KDF_KEYLEN},
        "shares": shares,
        "threshold": threshold,
        "original_file_size": len(plain),
        "encrypted_payload_size": len(payload),
        "encrypted_file_name": encrypted_name,
        "prefix": prefix,
        # 加密文件名时不能明文存原名；其余情况存精确文件名（prefix 会被消毒）
        "original_file_name": "" if (encrypt and encrypt_filename) else original_name,
        "encrypted": bool(encrypt),
        "encrypt_file_name": bool(encrypt and encrypt_filename),
    }
    meta_path = os.path.join(out, prefix + ".rsmeta")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    meta_blob = rs_meta_blob(meta)  # 同一份元数据副本写进每个分片尾部

    for i, shard in enumerate(shards):
        share_file = os.path.join(out, f"{prefix}.rs.{i + 1:03d}")
        wrapped = rs_wrap_shard(batch_id, i, shares, threshold, shard, meta_blob)
        with open(share_file, "wb") as f:
            f.write(wrapped)
        if (i + 1) % max(1, shares // 8) == 0 or i + 1 == shares:
            log(f"  已写出分片 {i + 1}/{shares}")

    log(f"备份完成！元数据: {meta_path}")
    log(f"分片文件: {os.path.join(out, prefix + '.rs.###')}")
    return meta_path, prefix


def rs_run_restore(input_path, password, output_dir, log):
    """执行恢复，返回还原出的文件路径。所有异常直接抛出。

    元数据来源优先级：
    1. 外部 .rsmeta 文件；
    2. 分片尾部内嵌的元数据副本（新备份才有，可脱离 .rsmeta 恢复，含加密备份）；
    3. 仅分片头（旧分片 + 无 rsmeta，只能恢复未加密备份）。"""
    if not input_path:
        raise ValueError("请填写分片或 .rsmeta 文件路径。")
    meta_path, prefix, directory = rs_discover_set(input_path)

    external = None
    if meta_path:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                external = json.load(f)
        except Exception:
            log("rsmeta 读取失败，尝试使用分片内嵌的元数据")

    # 只在所选分片/元数据所在目录查找，不递归、不去其它文件夹
    groups = rs_collect_shard_groups(prefix, directory)
    if not groups:
        raise ValueError(
            "没有在所选目录找到可用分片。\n"
            f"搜索目录：{directory}\n"
            f"请确认分片文件名前缀一致（{prefix}.rs.NNN），且与所选文件在同一目录。")

    def _embedded_groups():
        return sorted((g for g in groups.values() if g["embedded_meta"] is not None),
                      key=lambda g: (g["count"] >= g["threshold"], g["count"], g["total"]),
                      reverse=True)

    meta = external
    group = None
    if external is not None:
        # 优先选与 rsmeta 同批次的分片，避免同名旧备份混入
        batch_id = None
        if external.get("batch_id"):
            try:
                batch_id = _b64_raw_url_decode(external["batch_id"])
            except Exception:
                batch_id = None
        if batch_id is not None:
            group = groups.get(batch_id)
            if group is not None and (
                    group["total"] != external["shares"]
                    or group["threshold"] != external["threshold"]):
                group = None
        if group is not None and group["count"] < group["threshold"]:
            # 该批次分片不足：若另一组带内嵌元数据且够数，改用那一组
            alt = [g for g in _embedded_groups() if g["count"] >= g["threshold"]]
            if alt:
                group = alt[0]
                meta = group["embedded_meta"]
                log("rsmeta 对应的分片不足，改用另一组带内嵌元数据的分片")
        if group is None:
            embedded = _embedded_groups()
            if embedded:
                group = embedded[0]
                meta = group["embedded_meta"]
                log("rsmeta 与分片不匹配，改用分片内嵌的元数据")
            else:
                # 旧版分片（无内嵌元数据）：按 rsmeta 的分片数/阈值匹配
                candidates = [g for g in groups.values()
                              if g["total"] == external["shares"]
                              and g["threshold"] == external["threshold"]]
                if not candidates:
                    raise ValueError(
                        f"所选目录的分片与 .rsmeta 不匹配：元数据为 "
                        f"{external['shares']} 片 / 阈值 {external['threshold']}。\n"
                        "请确认 .rsmeta 与分片属于同一次备份。")
                group = max(candidates, key=lambda g: g["count"])
    else:
        # 没有 rsmeta：优先用分片尾部内嵌的元数据副本
        embedded_all = _embedded_groups()
        embedded = [g for g in embedded_all if g["count"] >= g["threshold"]]
        if embedded:
            group = embedded[0]
            meta = group["embedded_meta"]
            log(f"未找到 .rsmeta，使用分片内嵌元数据"
                f"（{group['total']} 片 / 阈值 {group['threshold']}）")
        elif embedded_all:
            # 内嵌元数据存在但分片不够：仍用它判断加密方式/原始文件名，并给出准确报错
            group = embedded_all[0]
            meta = group["embedded_meta"]
            log(f"分片内嵌元数据存在，但可用分片不足"
                f"（{group['count']}/{group['threshold']}）")
        else:
            meta = None

    if group is None:
        # 无任何可用元数据：取分片最多的一组，按分片头恢复（不足时由下面统一报错）
        group = max(groups.values(), key=lambda g: (g["count"], g["total"]))

    # 分片数量与解密无关，先判断，避免加密备份先报“缺少密码”
    shares_n, threshold_n = group["total"], group["threshold"]
    shards, available = group["shards"], group["count"]

    def _fmt_idx(nums):
        nums = list(nums)
        if len(nums) > 20:
            return ", ".join(map(str, nums[:20])) + f" ...（共 {len(nums)} 个）"
        return ", ".join(map(str, nums)) or "无"

    log(f"找到 {available} 份可用分片（需要 {threshold_n} 份）")
    if available < threshold_n:
        present = [i + 1 for i, s in enumerate(shards) if s is not None]
        missing = [i + 1 for i, s in enumerate(shards) if s is None]
        raise ValueError(
            f"分片不足：需要 {threshold_n} 份，只找到 {available} 份\n"
            f"已有分片：{_fmt_idx(present)}\n"
            f"缺少分片：{_fmt_idx(missing)}\n"
            f"搜索目录：{directory}\n"
            "只在所选分片所在目录查找；请把缺少的分片放到该目录后重试。")

    if meta is not None:
        is_encrypted = bool(meta.get("encrypted")) or meta.get("version", 2) <= 1
        is_encrypt_filename = bool(meta.get("encrypt_file_name")) or (
            meta.get("version", 2) <= 1 and meta.get("encrypted_file_name"))
        if is_encrypted:
            log("该备份已加密" + ("，文件名也已加密" if is_encrypt_filename else "")
                + "，需要密码。")

        master_key = None
        if is_encrypted:
            if not password:
                raise ValueError(
                    "该备份已加密，请先输入密码再恢复。\n"
                    "请在上方“密码”框填写备份时设置的密码（区分大小写）。\n"
                    "如果忘记密码，加密备份将无法恢复。")
            salt = _b64_raw_url_decode(meta["salt"])
            kdf = meta.get("kdf", {})
            log("派生密钥（scrypt）...")
            master_key = _rs_scrypt(password, salt,
                                    n=kdf.get("n", RS_KDF_N), r=kdf.get("r", RS_KDF_R),
                                    p=kdf.get("p", RS_KDF_P), keylen=kdf.get("key_len", RS_KDF_KEYLEN))

        if is_encrypt_filename and meta.get("encrypted_file_name"):
            try:
                original_name = _rs_aes_gcm_decrypt(
                    master_key, _b64_raw_url_decode(meta["encrypted_file_name"])).decode("utf-8")
            except Exception:
                raise ValueError(
                    "密码不正确，无法解密备份的文件名。\n"
                    "请确认“密码”框中的密码与备份时完全一致（区分大小写）。\n"
                    "若密码确认无误，可能是 .rsmeta / 分片内嵌元数据损坏，"
                    "或分片来自不同的备份批次。")
        else:
            original_name = meta.get("original_file_name") or meta["prefix"]

        payload_size = meta.get("encrypted_payload_size")
        if group["total"] != meta["shares"] or group["threshold"] != meta["threshold"]:
            raise ValueError(
                f"分片与元数据不匹配：元数据为 {meta['shares']} 片 / 阈值 {meta['threshold']}，"
                f"当前分片组为 {group['total']} 片 / 阈值 {group['threshold']}。\n"
                "请确认所选分片与 .rsmeta 属于同一次备份。")
    else:
        payload_size = None
        is_encrypted = False
        original_name = prefix
        log(f"未找到 .rsmeta，分片也未内嵌元数据，按分片头信息恢复"
            f"（{shares_n} 片 / 阈值 {threshold_n}）")
        log("注意：仅凭分片头无法判断原始大小、原始文件名与是否加密；"
            "若备份时启用了加密，需要含内嵌元数据的新分片或找回 rsmeta 才能解密。")

    payload = rs_reconstruct(shards, shares_n, threshold_n, payload_size)

    if is_encrypted:
        try:
            plain = _rs_aes_gcm_decrypt(master_key, payload)
        except Exception:
            hint = ""
            if available <= threshold_n:
                hint = ("\n当前可用分片刚好达到恢复阈值，参与恢复的任意一份分片损坏都会失败；"
                        "如可能，请把更多同组分片放到同一目录后重试。")
            raise ValueError(
                "密码不正确，或分片数据已损坏，无法解密文件内容。\n"
                f"可用分片：{available}/{shares_n} 份（恢复阈值 {threshold_n}）。\n"
                "请先确认密码（区分大小写）；若密码无误，请换用其它分片或重新拷贝分片。"
                + hint)
    else:
        plain = payload

    orig_size = meta.get("original_file_size") if meta is not None else None
    if orig_size is not None:
        if len(plain) > orig_size:
            plain = plain[:orig_size]
    elif meta is None:
        # 无 rsmeta：分片只保留了补齐到整数块的 0 填充，去掉尾部填充
        trimmed = plain.rstrip(b"\x00")
        removed = len(plain) - len(trimmed)
        if removed:
            log(f"已去掉尾部填充的 0 字节 {removed} 个（原文件若本身以 0 字节结尾，请核对大小）")
        plain = trimmed

    out = output_dir or os.path.join(directory, "rebuild")
    os.makedirs(out, exist_ok=True)
    target = Path(out) / os.path.basename(original_name)
    out_path = get_unique_path(target)  # 已存在则加 _1/_2，绝不覆盖旧文件
    if str(out_path) != str(target):
        log(f"目标文件已存在，自动改名: {out_path.name}")
    with open(out_path, "wb") as f:
        f.write(plain)
    log(f"恢复完成: {out_path}")
    return str(out_path)


# ============================================================


# ⑰ ReSoBackup 备份标签页 UI
# ============================================================

def build_rsbackup_tab(parent, app=None):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(4, weight=1)
    make_tip(parent, 0, "Reed-Solomon 纠删码备份：文件切成 N 份分片，任意 K 份即可恢复；"
                        "可选 AES-256-GCM 加密。分片尾部附带元数据副本，丢失 .rsmeta 也能恢复。",
             colspan=1, autowrap=True)

    state = {"busy": False}

    # ================= 备份区 =================
    box_backup = ttk.LabelFrame(parent, text=" 备份 ", padding=(10, 8))
    box_backup.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
    box_backup.grid_columnconfigure(0, weight=1)
    form_b = make_form(box_backup)
    form_b.grid(row=0, column=0, sticky="ew")

    form_label(form_b, 0, "源文件：")
    bk_input = tk.StringVar()
    ttk.Entry(form_b, textvariable=bk_input, width=52).grid(
        row=0, column=1, sticky="w", padx=(0, 6), pady=5)
    ttk.Button(form_b, text="浏览...",
               command=lambda: _pick_file(bk_input, "选择要备份的文件")).grid(
        row=0, column=2, sticky="w", padx=(0, 4), pady=5)

    var_shares = make_num_row(form_b, 1, "分片数：", RS_DEFAULT_SHARES,
                              RS_MIN_SHARES, RS_MAX_SHARES,
                              note=f"{RS_MIN_SHARES}~{RS_MAX_SHARES} 份")
    var_threshold = make_num_row(form_b, 2, "恢复阈值：", RS_DEFAULT_THRESHOLD,
                                 1, RS_MAX_SHARES, note="任意这么多份即可恢复")

    # 加密选项（放在输入列，与上方输入框左对齐）
    crypto = ttk.Frame(form_b)
    crypto.grid(row=3, column=1, columnspan=3, sticky="w", padx=(0, 6), pady=3)
    var_encrypt = tk.BooleanVar(value=False)
    ttk.Checkbutton(crypto, text="加密 (AES-256-GCM)", variable=var_encrypt).pack(side="left")
    ttk.Label(crypto, text="密码：").pack(side="left", padx=(12, 4))
    var_password = tk.StringVar()
    ttk.Entry(crypto, textvariable=var_password, width=20, show="*").pack(side="left")
    var_enc_name = tk.BooleanVar(value=False)
    ttk.Checkbutton(crypto, text="同时加密文件名", variable=var_enc_name).pack(
        side="left", padx=(16, 0))

    form_label(form_b, 4, "输出目录：")
    bk_outdir = tk.StringVar()
    ttk.Entry(form_b, textvariable=bk_outdir, width=52).grid(
        row=4, column=1, sticky="w", padx=(0, 6), pady=5)
    ttk.Button(form_b, text="浏览...",
               command=lambda: _pick_dir(bk_outdir, "选择分片输出目录")).grid(
        row=4, column=2, sticky="w", padx=(0, 4), pady=5)
    form_note(form_b, 4, "留空 = 源文件所在目录")

    label_warn = ttk.Label(form_b, text="", foreground=COLOR["warning"], justify="left")
    label_warn.grid(row=5, column=1, columnspan=3, sticky="w", padx=(0, 6), pady=(2, 4))

    def _warn_wrap(event):
        try:
            label_warn.configure(wraplength=max(300, event.width - 160))
        except Exception:
            pass

    box_backup.bind("<Configure>", _warn_wrap, add="+")

    def refresh_warn(*_args):
        try:
            s = int(var_shares.get())
            t = int(var_threshold.get())
        except ValueError:
            label_warn.config(text="")
            return
        if not (RS_MIN_SHARES <= s <= RS_MAX_SHARES) or not (1 <= t <= s):
            label_warn.config(text="")
            return
        warnings = rs_backup_warnings(s, t)
        parity = s - t
        text = f"冗余 {parity} 份（可丢失 {parity} 份），存储膨胀约 {s / t:.2f}x"
        if warnings:
            text += "  |  " + "  |  ".join(warnings)
        label_warn.config(text=text)

    var_shares.trace_add("write", refresh_warn)
    var_threshold.trace_add("write", refresh_warn)
    refresh_warn()

    # ================= 恢复区 =================
    box_restore = ttk.LabelFrame(parent, text=" 恢复 ", padding=(10, 8))
    box_restore.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))
    box_restore.grid_columnconfigure(0, weight=1)
    form_r = make_form(box_restore)
    form_r.grid(row=0, column=0, sticky="ew")

    form_label(form_r, 0, "分片/元数据：")
    rs_input = tk.StringVar()
    ttk.Entry(form_r, textvariable=rs_input, width=52).grid(
        row=0, column=1, sticky="w", padx=(0, 6), pady=5)
    ttk.Button(form_r, text="浏览...",
               command=lambda: _pick_file(rs_input, "选择任意 .rs.NNN 或 .rsmeta 文件")).grid(
        row=0, column=2, sticky="w", padx=(0, 4), pady=5)
    form_note(form_r, 0, "选一个分片或 .rsmeta（只查同目录）")

    form_label(form_r, 1, "密码：")
    rs_password = tk.StringVar()
    ttk.Entry(form_r, textvariable=rs_password, width=24, show="*").grid(
        row=1, column=1, sticky="w", padx=(0, 6), pady=5)
    form_note(form_r, 1, "加密备份必填，未加密留空")

    form_label(form_r, 2, "输出目录：")
    rs_outdir = tk.StringVar()
    ttk.Entry(form_r, textvariable=rs_outdir, width=52).grid(
        row=2, column=1, sticky="w", padx=(0, 6), pady=5)
    ttk.Button(form_r, text="浏览...",
               command=lambda: _pick_dir(rs_outdir, "选择恢复输出目录")).grid(
        row=2, column=2, sticky="w", padx=(0, 4), pady=5)
    form_note(form_r, 2, "留空 = 分片目录\\rebuild，不覆盖")

    text_log, log = make_log_panel(parent, 4, colspan=1, title="运行日志")

    # ================= 功能函数 =================
    def _pick_file(var, title):
        p = filedialog.askopenfilename(title=title)
        if p:
            var.set(p)

    def _pick_dir(var, title):
        p = filedialog.askdirectory(title=title)
        if p:
            var.set(p)

    def _toggle_buttons(enabled):
        st = "normal" if enabled else "disabled"
        for b in (btn_backup, btn_restore):
            b.config(state=st)

    def do_backup():
        if state["busy"]:
            return
        src = clean_path(bk_input.get())
        outdir = clean_path(bk_outdir.get())
        try:
            shares = int(var_shares.get())
            threshold = int(var_threshold.get())
        except ValueError:
            messagebox.showerror("错误", "分片数与恢复阈值必须是数字！")
            return
        encrypt = var_encrypt.get()
        password = var_password.get()
        enc_name = var_enc_name.get()
        if not src:
            messagebox.showerror("错误", "请填写源文件！")
            return
        if not os.path.isfile(src):
            messagebox.showerror("错误", "源文件不存在或不是文件！")
            return
        if not RS_MIN_SHARES <= shares <= RS_MAX_SHARES:
            messagebox.showerror("错误", f"分片数需在 {RS_MIN_SHARES}~{RS_MAX_SHARES} 之间！")
            return
        if not 1 <= threshold <= shares:
            messagebox.showerror("错误", "恢复阈值需在 1 ~ 分片数 之间！")
            return
        if encrypt and not password:
            messagebox.showerror("错误", "已勾选加密，请填写密码！")
            return
        if enc_name and not encrypt:
            messagebox.showerror("错误", "加密文件名需要先勾选加密！")
            return
        warnings = rs_backup_warnings(shares, threshold)
        if warnings:
            if not messagebox.askyesno("参数风险确认", "\n".join(warnings) + "\n\n确认继续备份？"):
                return
        state["busy"] = True
        _toggle_buttons(False)
        clear_log(text_log)
        log(f"开始备份：{src}")
        log(f"参数：{shares} 片 / 阈值 {threshold} / "
            f"{'加密' if encrypt else '不加密'}{' + 加密文件名' if enc_name else ''}")

        def work():
            meta_path, prefix = rs_run_backup(src, shares, threshold, password,
                                              outdir, encrypt, enc_name, log)
            log(f"前缀：{prefix}")

        def done(err):
            state["busy"] = False
            _toggle_buttons(True)  # 两个按钮都恢复，避免另一侧永久变灰
            if err:
                messagebox.showerror("失败", "备份失败，详情见日志。")
            else:
                messagebox.showinfo("完成", "备份完成！分片与元数据已生成，详情见日志。")

        run_task(btn_backup, log, work, on_done=done)

    def do_restore():
        if state["busy"]:
            return
        src = clean_path(rs_input.get())
        outdir = clean_path(rs_outdir.get())
        password = rs_password.get()
        if not src:
            messagebox.showerror("错误", "请填写分片或元数据文件路径！")
            return
        if not os.path.isfile(src):
            messagebox.showerror("错误", "文件不存在或不是文件！")
            return
        state["busy"] = True
        _toggle_buttons(False)
        clear_log(text_log)
        log(f"开始恢复：{src}")
        fail = {}

        def work():
            try:
                out_path = rs_run_restore(src, password, outdir, log)
                log(f"输出：{out_path}")
            except (ValueError, OSError) as e:
                # 密码错误、分片不足等可预期问题：日志保持简洁，不打印整段 traceback
                fail["msg"] = str(e) or type(e).__name__
                log(f"恢复失败：{fail['msg']}")
            except Exception as e:
                fail["msg"] = f"恢复时发生意外错误：{e}"
                raise

        def done(err):
            state["busy"] = False
            _toggle_buttons(True)  # 两个按钮都恢复，避免另一侧永久变灰
            if fail:
                messagebox.showerror("恢复失败", fail["msg"])
            elif err:
                messagebox.showerror("恢复失败", "详情见日志。")
            else:
                messagebox.showinfo("完成", "恢复完成！详情见日志。")

        run_task(btn_restore, log, work, on_done=done)

    def on_drop(paths):
        """拖放：.rsmeta/.rs.NNN 填入恢复框，其他文件填入备份框"""
        for p in paths:
            base = os.path.basename(p).lower()
            if base.endswith(".rsmeta") or ".rs." in base:
                rs_input.set(p)
            elif os.path.isfile(p):
                bk_input.set(p)

    form_btn = make_form(parent)
    form_btn.grid(row=3, column=0, sticky="w", padx=6, pady=(0, 2))
    frame_btn = ttk.Frame(form_btn)
    frame_btn.grid(row=0, column=1, columnspan=2, sticky="w", pady=(4, 2))
    btn_backup = tk.Button(frame_btn, text="开始备份", command=lambda: do_backup(),
                           bg=COLOR["accent"], fg=COLOR["text_on_accent"],
                           disabledforeground=COLOR["text_on_accent"],
                           activebackground=COLOR["accent_active"], activeforeground=COLOR["text_on_accent"],
                           font=("Microsoft YaHei", 10, "bold"), padx=16, pady=4, cursor="hand2")
    btn_backup.pack(side="left", padx=(0, 8))
    btn_restore = tk.Button(frame_btn, text="开始恢复", command=lambda: do_restore(),
                            bg=COLOR["success"], fg=COLOR["text_on_accent"],
                            disabledforeground=COLOR["text_on_accent"],
                            activebackground=COLOR["success_active"], activeforeground=COLOR["text_on_accent"],
                            font=("Microsoft YaHei", 10, "bold"), padx=16, pady=4, cursor="hand2")
    btn_restore.pack(side="left")

    if app is not None:
        app.register_drop_handler(parent, on_drop)


# ============================================================
