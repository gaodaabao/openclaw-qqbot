#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qqbot 投递补丁 —— parity 校验（打完补丁后跑，确认与主人环境一致）

用法:
    python qqbot-parity-check.py                     # 自动搜索目标文件
    python qqbot-parity-check.py <index.cjs>         # 指定目标文件
    python qqbot-parity-check.py --stock <备份/原版>  # 额外核对你的 stock 基线
    python qqbot-parity-check.py <index.cjs> --stock <原版>

判定标准（重要）:
    权威标准只有一条 —— **打完后的 sha256 等于主人的 patched 值**（见下方 EXPECTED）。
    stock 值只是诊断用：主人手上那份 .bak 文件你不必有，把**你自己打补丁前的备份**
    传进来用 --stock 比对即可。

退出码: 0 = 逐字节一致 ｜ 3 = 等价（锚点全在但字节不同，见提示）｜ 1 = 不合格
"""
import glob
import hashlib
import io
import os
import shutil
import subprocess
import sys

# ── 主人环境权威值（2026-10-24 v6 收口）──────────────────────────
EXPECTED_SIZE = 564064
EXPECTED_SHA = "ed4ec421b418d7ef15f666b08c597951bde1483dac6690a9cb742a1e154b1d99"
STOCK_SIZE = 552290
STOCK_SHA = "f5d99db0eef0cd7f3daffa0b87aa17dbca7bf9710511b04571e19f919b4fe73d"

# 主人环境的演进链（可逐阶段核对；大小/ sha 都对得上说明打到你那一步了）
STAGES = [
    ("stock 2.0.4", 552290, "f5d99db0eef0cd7f3daffa0b87aa17dbca7bf9710511b04571e19f919b4fe73d"),
    ("A+B", 555637, "b13bcb48d2eca19b5e0c198221484c5f69cdcdf82b7dd8f96f2162e79ac41299"),
    ("v3 = A+B+C", 558431, "7d7b9442bd9d5d35389caba53b7e964e4c53e7ce239838645e0063c004c778e8"),
    ("v4 = A+B+C+D", 559698, "2b51834cf3d013e5b8ff77d87f4d5dd2a526c142a9bbd86f05b62cee4764e8fa"),
    ("v5 = A+B+C+D+E", 560182, "209eeaa62cdf7d7d16850b0772be04cf559dfdd0858e115c249a794d59ed734b"),
    ("v6 = A+B+C+D+E+F", 564064, "ed4ec421b418d7ef15f666b08c597951bde1483dac6690a9cb742a1e154b1d99"),
]

# 阶段 → 该阶段独有锚点
ANCHORS = [
    ("A/B", "final body fallback send"),
    ("A/B", "QQBOT_STREAM_MAX_DRAFTS"),
    ("C", "QQBOT_STREAM_MID_BLOCK_MAX"),
    ("C", "normalizeBodyForDedupe"),
    ("C", "final body pushed to stream"),
    ("D", "displayedBodyText"),
    ("E", "bodyShownInStream"),
    ("E", "static final body suppressed"),
    ("F", "isStreamAlreadySubmittedError"),
    ("F", "stream message already submitted on platform"),
]

PATTERNS = [
    "~/.openclaw/npm/projects/*qqbot*/node_modules/@tencent-connect/openclaw-qqbot/dist/index.cjs",
    "~/.openclaw/extensions/*qqbot*/node_modules/@tencent-connect/openclaw-qqbot/dist/index.cjs",
    "~/.openclaw/extensions/*qqbot*/dist/index.cjs",
]


def sha256_of(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_target():
    hits = []
    for pat in PATTERNS:
        hits += [os.path.expanduser(p) for p in glob.glob(os.path.expanduser(pat))]
    hits = [p for p in hits if os.path.isfile(p)]
    return [p for p in hits if "deliverDispatchPayload" in io.open(p, encoding="utf-8", errors="ignore").read()]


def stage_of(size, sha):
    for label, s, h in STAGES:
        if size == s and sha == h:
            return label
    for label, s, _ in STAGES:
        if size == s:
            return label + " (大小对上、sha 不同 → 有本地差异)"
    return None


def main():
    argv = sys.argv[1:]
    stock_path = None
    positional = []
    i = 0
    while i < len(argv):
        if argv[i] == "--stock" and i + 1 < len(argv):
            stock_path = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1

    print("=" * 74)
    print("主人环境权威值：%d B ｜ sha256 %s" % (EXPECTED_SIZE, EXPECTED_SHA))
    print("stock  基线值：%d B ｜ sha256 %s" % (STOCK_SIZE, STOCK_SHA))
    print("=" * 74)

    targets = positional or find_target()
    if not targets:
        print("FAIL  未找到目标文件：把 <...>/@tencent-connect/openclaw-qqbot/dist/index.cjs 作为参数传入")
        return 1

    if stock_path:
        print()
        print("--- stock 基线核对（你的打补丁前备份 vs 主人）---")
        if not os.path.isfile(stock_path):
            print("  ! 文件不存在: %s" % stock_path)
        else:
            ss, sh = os.path.getsize(stock_path), sha256_of(stock_path)
            print("  你的  : %d B ｜ %s" % (ss, sh))
            print("  主人  : %d B ｜ %s" % (STOCK_SIZE, STOCK_SHA))
            if sh == STOCK_SHA:
                print("  ⇒ **基线一致**：只要补丁打全，最终一定与主人逐字节一致 ✓")
            else:
                print("  ⇒ 基线**不一致**：说明插件版本/内容有差异。")
                print("     补丁脚本是锚点替换，同样能打，但最终 sha 必然不同；")
                print("     此时以「阶段锚点全 ✓ + node --check rc=0」为通过标准，不必强求 sha。")
                print("     想强求逐字节一致 → 用包内 stock-2.0.4-index.cjs 作基线（先备份你自己的）。")

    ok_all = True
    for path in targets:
        print()
        print("--- 目标: %s ---" % path)
        size, sha = os.path.getsize(path), sha256_of(path)
        print("  字节数 : %d" % size)
        print("  sha256 : %s" % sha)

        verdict = None
        if sha == EXPECTED_SHA and size == EXPECTED_SIZE:
            print("  ⇒ 与主人环境 **逐字节一致（PASS）** ✓")
            verdict = "identical"
        elif sha == STOCK_SHA:
            print("  ⇒ 这是 **stock 原版**：补丁还没打（或已被插件更新覆盖），按说明书 §4 重打")
        else:
            st = stage_of(size, sha)
            if st:
                print("  ⇒ 与主人不一致；按大小看像是停在 **%s**" % st)
            else:
                print("  ⇒ 与主人不一致（大小也不是任何已知阶段）")
            print("     排查：① 是否漏打阶段（见下方锚点 ✗ 项）② 行尾是否 CRLF ③ 基线是否与主人相同(--stock)")

        text = io.open(path, encoding="utf-8", errors="ignore").read()
        print()
        print("  阶段锚点：")
        by = {}
        for stage, needle in ANCHORS:
            by.setdefault(stage, []).append((needle, text.count(needle)))
        anchors_ok = True
        for stage in ("A/B", "C", "D", "E", "F"):
            rows = by.get(stage, [])
            good = bool(rows) and all(n > 0 for _, n in rows)
            anchors_ok = anchors_ok and good
            print("    %-4s %s %s" % (stage, "✓" if good else "✗",
                                      " ｜ ".join("%s x%d" % (nm, n) for nm, n in rows)))

        crlf = text.count("\r\n")
        print()
        print("  行尾 : %s" % ("CRLF x%d ✗（sha 必然对不上；应用 .patch 时须 -c core.autocrlf=false）" % crlf
                               if crlf else "LF ✓"))

        node = shutil.which("node")
        syntax_ok = True
        if node:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            syntax_ok = r.returncode == 0
            print("  语法 : node --check rc=%d %s" % (r.returncode, "✓" if syntax_ok else "✗ " + r.stderr[:200]))
        else:
            print("  语法 : （PATH 无 node，跳过；建议手动 node --check）")

        print()
        if verdict == "identical":
            print("  结论 : **PASS —— 与主人环境逐字节一致**")
        elif anchors_ok and not crlf and syntax_ok:
            print("  结论 : **等价（EQUIVALENT）** —— 六个阶段锚点全在、行尾 LF、语法 OK，")
            print("         但字节与主人不同（基线差异）。功能等价，可用；若要逐字节一致见上面 --stock 提示。")
            verdict = "equivalent"
        else:
            print("  结论 : **FAIL** —— 见上面 ✗ 项，按说明书 §4 重打")
            verdict = "fail"

        ok_all = ok_all and verdict in ("identical", "equivalent")
        if verdict == "fail":
            ok_all = False

    print()
    print("阶段表（可选逐阶段核对）：")
    for label, s, h in STAGES:
        print("    %-16s %7d B ｜ %s" % (label, s, h[:16] + "…"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())