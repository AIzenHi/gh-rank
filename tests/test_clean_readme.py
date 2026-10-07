"""清洗器压力测试：把各种脏 README 形态都塞进去，看清洗结果干不干净。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.readme import clean_readme, extract_front_summary  # noqa: E402

NASTY = """---
title: Awesome Project
author: someone-nobody
license: MIT
tags:
  - cli
  - queue
---

<!-- markdownlint-disable -->
# Awesome Project
[![Build](https://img.shields.io/badge/build-passing-brightgreen)](https://img.shields.io/badge/build-passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)
[![npm](https://img.shields.io/npm/v/pkg)](https://www.npmjs.com/pkg)
<a href="https://travis-ci.org/u/p"><img src="https://travis-ci.org/u/p.svg?branch=main" alt="Build"></a>
<p align="center">
  <img src="assets/banner.svg" alt="Project Banner" width="600">
</p>

- [Contents](#contents)
- [Features](#features)

## Contents
- [Features](#features)
- [Install](#install)

## Features
Real prose starts here. It solves a genuine problem for developers who need
to ship reliable batch jobs without writing distributed systems code.

| Feature | Status | Notes |
|---------|--------|-------|
| Streaming | done  | ok |
| Replay   | wip   | soon |

---

## Install
```bash
npm install -g pkg
```

See [the docs](https://example.com/docs) for details. ![screenshot](./docs/shot.png)

## License
MIT
"""

print("=" * 60)
print("清洗结果")
print("=" * 60)
out = clean_readme(NASTY, 12000)
print(out)
print()
print("=" * 60)
print("断言")
print("=" * 60)

checks = {
    "shields.io 已清除": "shields.io" not in out,
    "travis-ci 已清除": "travis-ci" not in out,
    "banner.svg 已清除": "banner.svg" not in out,
    "HTML 注释已清除": "<!--" not in out,
    "HTML 标签已清除": "<img" not in out and "<p " not in out and "</p>" not in out,
    "目录条目已清除": "- [Contents](#contents)" not in out,
    "纯符号碎屑已清除": "\n---\n" not in out and "\n===" not in out,
    # front matter：GitHub 上很常见。不剥掉的话首行 --- 被当碎屑删掉，
    # 键值行却留下来当正文，extract_front_summary 再把它当第一段摘要 ——
    # 用户看到的降级兜底说明就变成一串 YAML（实测复现过）。
    "front matter 已整块剥掉": not out.lstrip().startswith("---"),
    "front matter 键值行不残留": "author:" not in out and "title: Awesome Project" not in out,
    "front matter 值不残留": "someone-nobody" not in out,
    "表格内容保留": "Streaming" in out,
    "正文保留": "distributed systems code" in out,
    "代码块保留": "npm install -g pkg" in out,
    "普通链接保留": "the docs" in out,
    "空标签链接已清除": "[](http" not in out,
    "降级摘要取正文而非 YAML/链接": "distributed systems" in extract_front_summary(out),
    "降级摘要不含 front matter 残留": "someone-nobody" not in extract_front_summary(out),
}

failed = 0
for name, passed in checks.items():
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    failed += 0 if passed else 1

summary = extract_front_summary(out)
print()
print(f"  降级摘要 ({len(summary)} 字): {summary[:120]}")
print()
print("原始长度 %d → 清洗后 %d（减少 %.0f%%）" % (len(NASTY), len(out), (1 - len(out) / len(NASTY)) * 100))
print()
print("失败项：%d" % failed)
sys.exit(1 if failed else 0)
