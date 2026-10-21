# -*- coding: utf-8 -*-
"""⑰ ReSoBackup 备份（迁移自 Go 版 ReSoBackup，Python 重实现）
Reed-Solomon 纠删码分片 + 可选 AES-256-GCM 加密（scrypt KDF）
分片格式与 Go 版一致：31 字节头（magic RSBK）+ .rsmeta JSON 元数据"""
import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .common import (clean_path, clear_log, make_log_panel, make_num_box,
                     make_tip, run_task)


# ⑰ ReSoBackup 备份（迁移自 Go 版 ReSoBackup，Python 重实现）
# Reed-Solomon 纠删码分片 + 可选 AES-256-GCM 加密（scrypt KDF）
# 分片格式与 Go 版一致：31 字节头（magic RSBK）+ .rsmeta JSON 元数据
# ============================================================

RS_SHARD_MAGIC = b"RSBK"
RS_SHARD_HEADER_SIZE = 31
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


def rs_wrap_shard(batch_id, index, total_shares, threshold, data):
    """31 字节头 + 数据（大端）"""
    header = bytearray(RS_SHARD_HEADER_SIZE)
    header[0:4] = RS_SHARD_MAGIC
    header[4] = 1
    header[5:7] = total_shares.to_bytes(2, "big")
    header[7:9] = threshold.to_bytes(2, "big")
    header[9:11] = index.to_bytes(2, "big")
    header[11:27] = batch_id
    header[27:31] = len(data).to_bytes(4, "big")
    return bytes(header) + data


def rs_unwrap_shard(path):
    """解析分片文件，返回 (index, data, header_dict)；失败抛异常"""
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
        "total_shares": total, "threshold": threshold, "batch_id": batch_id}


def rs_discover_set(input_path):
    """从任意 .rs.NNN 或 .rsmeta 路径定位元数据文件，返回 (meta_path, prefix, dir)"""
    base = os.path.basename(input_path)
    directory = os.path.dirname(os.path.abspath(input_path))
    if base.endswith(".rsmeta"):
        return os.path.join(directory, base), base[:-len(".rsmeta")], directory
    idx = base.rfind(".rs.")
    if idx < 0:
        raise ValueError("input must be a .rsmeta or .rs.NNN file")
    prefix = base[:idx]
    meta_path = os.path.join(directory, prefix + ".rsmeta")
    if not os.path.exists(meta_path):
        raise FileNotFoundError(f"metadata file not found: {meta_path}")
    return meta_path, prefix, directory


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
        "encrypted": bool(encrypt),
        "encrypt_file_name": bool(encrypt and encrypt_filename),
    }
    meta_path = os.path.join(out, prefix + ".rsmeta")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    for i, shard in enumerate(shards):
        share_file = os.path.join(out, f"{prefix}.rs.{i + 1:03d}")
        wrapped = rs_wrap_shard(batch_id, i, shares, threshold, shard)
        with open(share_file, "wb") as f:
            f.write(wrapped)
        if (i + 1) % max(1, shares // 8) == 0 or i + 1 == shares:
            log(f"  已写出分片 {i + 1}/{shares}")

    log(f"备份完成！元数据: {meta_path}")
    log(f"分片文件: {os.path.join(out, prefix + '.rs.###')}")
    return meta_path, prefix


def rs_run_restore(input_path, password, output_dir, log):
    """执行恢复，返回还原出的文件路径。所有异常直接抛出。"""
    if not input_path:
        raise ValueError("--input is required")
    meta_path, prefix, directory = rs_discover_set(input_path)
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    is_encrypted = bool(meta.get("encrypted")) or meta.get("version", 2) <= 1
    is_encrypt_filename = bool(meta.get("encrypt_file_name")) or (
        meta.get("version", 2) <= 1 and meta.get("encrypted_file_name"))

    master_key = None
    if is_encrypted:
        if not password:
            raise ValueError("password is required for encrypted backups")
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
            raise ValueError("invalid password or corrupted metadata")
    else:
        original_name = meta["prefix"]

    shares_n = meta["shares"]
    threshold_n = meta["threshold"]
    shards = [None] * shares_n
    available = 0
    # 扫描同前缀的所有分片
    for name in sorted(os.listdir(directory)):
        if not name.startswith(prefix + ".rs.") or name.endswith(".rsmeta"):
            continue
        if not name[len(prefix) + 4:].isdigit():
            continue
        try:
            idx, data, hdr = rs_unwrap_shard(os.path.join(directory, name))
        except Exception:
            continue
        if hdr["total_shares"] != shares_n or hdr["threshold"] != threshold_n:
            continue
        if 0 <= idx < shares_n and shards[idx] is None:
            shards[idx] = data
            available += 1
    log(f"找到 {available} 份可用分片（需要 {threshold_n} 份）")
    if available < threshold_n:
        raise ValueError(f"not enough shares: have {available}, need {threshold_n}")

    payload_size = meta.get("encrypted_payload_size")
    payload = rs_reconstruct(shards, shares_n, threshold_n, payload_size)

    if is_encrypted:
        try:
            plain = _rs_aes_gcm_decrypt(master_key, payload)
        except Exception:
            raise ValueError("failed to decrypt payload, password may be incorrect or shares corrupted")
    else:
        plain = payload

    orig_size = meta.get("original_file_size")
    if orig_size is not None and len(plain) > orig_size:
        plain = plain[:orig_size]

    out = output_dir or "."
    os.makedirs(out, exist_ok=True)
    out_path = os.path.join(out, os.path.basename(original_name))
    with open(out_path, "wb") as f:
        f.write(plain)
    log(f"恢复完成: {out_path}")
    return out_path


# ============================================================


# ⑰ ReSoBackup 备份标签页 UI
# ============================================================

def build_rsbackup_tab(parent, app=None):
    make_tip(parent, 0, "Reed-Solomon 纠删码备份：把文件切成 N 份分片，任意 K 份即可恢复，可分散存储到多个位置；"
                        "可选 AES-256-GCM 加密（scrypt 派生密钥）。分片格式与 Go 版 ReSoBackup 一致。")

    state = {"busy": False}

    # ================= 备份区 =================
    box_backup = ttk.LabelFrame(parent, text=" 备份 ", padding=8)
    box_backup.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
    box_backup.grid_columnconfigure(1, weight=1)

    ttk.Label(box_backup, text="源文件：").grid(row=0, column=0, sticky="w", padx=6, pady=5)
    bk_input = tk.StringVar()
    ttk.Entry(box_backup, textvariable=bk_input, width=52).grid(row=0, column=1, sticky="w", padx=6)
    ttk.Button(box_backup, text="浏览...",
               command=lambda: _pick_file(bk_input, "选择要备份的文件")
               ).grid(row=0, column=2, sticky="w")

    # 分片参数（子框架隔离列宽，避免与其他行列宽互相干扰）
    frame_params = ttk.Frame(box_backup)
    frame_params.grid(row=1, column=0, columnspan=3, sticky="w", padx=2, pady=4)
    ttk.Label(frame_params, text="分片数：").grid(row=0, column=0, sticky="w", padx=6)
    box_shares, var_shares = make_num_box(frame_params, RS_DEFAULT_SHARES, RS_MIN_SHARES, RS_MAX_SHARES)
    box_shares.grid(row=0, column=1, sticky="w")
    ttk.Label(frame_params, text="恢复阈值：").grid(row=0, column=2, sticky="w", padx=(16, 0))
    box_thr, var_threshold = make_num_box(frame_params, RS_DEFAULT_THRESHOLD, 1, RS_MAX_SHARES)
    box_thr.grid(row=0, column=3, sticky="w")
    ttk.Label(frame_params, text="(3~128 / 1~分片数；冗余 = 分片数 − 阈值)",
              foreground="#666666").grid(row=0, column=4, sticky="w", padx=8)

    frame_crypto = ttk.Frame(box_backup)
    frame_crypto.grid(row=2, column=0, columnspan=3, sticky="w", padx=2, pady=4)
    var_encrypt = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame_crypto, text="加密 (AES-256-GCM)", variable=var_encrypt
                    ).grid(row=0, column=0, sticky="w", padx=6)
    ttk.Label(frame_crypto, text="密码：").grid(row=0, column=1, sticky="w", padx=(12, 0))
    var_password = tk.StringVar()
    ttk.Entry(frame_crypto, textvariable=var_password, width=20, show="*"
              ).grid(row=0, column=2, sticky="w")
    var_enc_name = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame_crypto, text="同时加密文件名", variable=var_enc_name
                    ).grid(row=0, column=3, sticky="w", padx=(16, 0))

    ttk.Label(box_backup, text="输出目录：").grid(row=3, column=0, sticky="w", padx=6, pady=5)
    bk_outdir = tk.StringVar()
    ttk.Entry(box_backup, textvariable=bk_outdir, width=52).grid(row=3, column=1, sticky="w", padx=6)
    ttk.Button(box_backup, text="浏览...",
               command=lambda: _pick_dir(bk_outdir, "选择分片输出目录")
               ).grid(row=3, column=2, sticky="w")
    ttk.Label(box_backup, text="留空 = 源文件所在目录", foreground="#666666"
              ).grid(row=3, column=3, sticky="w", padx=6)

    label_warn = ttk.Label(box_backup, text="", foreground="#b26a00", wraplength=1050)
    label_warn.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=2)

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
    box_restore = ttk.LabelFrame(parent, text=" 恢复 ", padding=8)
    box_restore.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
    box_restore.grid_columnconfigure(1, weight=1)

    ttk.Label(box_restore, text="分片/元数据：").grid(row=0, column=0, sticky="w", padx=6, pady=5)
    rs_input = tk.StringVar()
    ttk.Entry(box_restore, textvariable=rs_input, width=52).grid(row=0, column=1, sticky="w", padx=6)
    ttk.Button(box_restore, text="浏览...",
               command=lambda: _pick_file(rs_input, "选择任意 .rs.NNN 或 .rsmeta 文件")
               ).grid(row=0, column=2, sticky="w")
    ttk.Label(box_restore, text="任选一个分片或 .rsmeta，自动寻找同组其他分片",
              foreground="#666666").grid(row=0, column=3, sticky="w", padx=6)

    ttk.Label(box_restore, text="密码：").grid(row=1, column=0, sticky="w", padx=6, pady=5)
    rs_password = tk.StringVar()
    ttk.Entry(box_restore, textvariable=rs_password, width=24, show="*"
              ).grid(row=1, column=1, sticky="w", padx=6)
    ttk.Label(box_restore, text="加密备份必填；未加密留空", foreground="#666666"
              ).grid(row=1, column=2, sticky="w", padx=6)

    ttk.Label(box_restore, text="输出目录：").grid(row=2, column=0, sticky="w", padx=6, pady=5)
    rs_outdir = tk.StringVar()
    ttk.Entry(box_restore, textvariable=rs_outdir, width=52).grid(row=2, column=1, sticky="w", padx=6)
    ttk.Button(box_restore, text="浏览...",
               command=lambda: _pick_dir(rs_outdir, "选择恢复输出目录")
               ).grid(row=2, column=2, sticky="w")
    ttk.Label(box_restore, text="留空 = 当前目录", foreground="#666666"
              ).grid(row=2, column=3, sticky="w", padx=6)

    text_log, log = make_log_panel(parent, 3, title="运行日志")

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
            messagebox.showerror("错误", "文件不存在！")
            return
        state["busy"] = True
        _toggle_buttons(False)
        clear_log(text_log)
        log(f"开始恢复：{src}")

        def work():
            out_path = rs_run_restore(src, password, outdir, log)
            log(f"输出：{out_path}")

        def done(err):
            state["busy"] = False
            _toggle_buttons(True)  # 两个按钮都恢复，避免另一侧永久变灰
            if err:
                messagebox.showerror("失败", "恢复失败，详情见日志。")
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

    frame_btn = ttk.Frame(parent)
    frame_btn.grid(row=4, column=0, columnspan=3, sticky="w", padx=6, pady=2)
    btn_backup = tk.Button(frame_btn, text="开始备份", command=lambda: do_backup(),
                           bg="#2f6bff", fg="white", activebackground="#2455cc",
                           activeforeground="white", font=("Microsoft YaHei", 10, "bold"),
                           padx=14, pady=3, cursor="hand2")
    btn_backup.pack(side="left", padx=6)
    btn_restore = tk.Button(frame_btn, text="开始恢复", command=lambda: do_restore(),
                            bg="#1a7f37", fg="white", activebackground="#15662b",
                            activeforeground="white", font=("Microsoft YaHei", 10, "bold"),
                            padx=14, pady=3, cursor="hand2")
    btn_restore.pack(side="left", padx=6)

    parent.grid_rowconfigure(3, weight=1)

    if app is not None:
        app.register_drop_handler(parent, on_drop)


# ============================================================
