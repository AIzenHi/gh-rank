"""主题切换 + 字号档位的回归测试。

为什么要有这个：2026-10-02 加主题功能时踩了个隐蔽的坑 ——
`.rank-item` 有 `transition: background .15s`，实测证明颜色过渡在
tab 不在前台时会**永久停在起始值**：强制把 --bg-soft 改成 #ff00ff，
绘制出来的背景仍是旧色。表现是「点了切主题，页面变了卡片没变」。

所以必须守住两条：
  1. 颜色一律瞬时（规则里不能有 background/color/border-color 的 transition）
  2. 两套主题的变量必须真的不同（不然「切了但没变化」也是坏）

用静态检查 + 变量比对来断言，不依赖浏览器焦点状态。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  FAIL  {name}  {detail}")


css = (WEB / "style.css").read_text(encoding="utf-8")
js = (WEB / "app.js").read_text(encoding="utf-8")
html = (WEB / "index.html").read_text(encoding="utf-8")

# 先剥掉 CSS 注释再分析 —— 否则本文件顶部那段说明里出现的
# 「transition: background .15s」示例文字会被当成真声明，造成假阳性。
css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

print("=" * 64)
print("主题 / 字号 回归测试")
print("=" * 64)

# ---------------------------------------------------------------- 结构
print("\n[1] 控件与持久化结构")

check("HTML 有字号档位控件", 'class="font-ctl"' in html and 'data-fs="1.4"' in html)
check("HTML 有主题切换按钮", 'id="btnTheme"' in html)
check("HTML 首屏内联脚本（防闪烁）",
      "ghrank.theme" in html and html.index("ghrank.theme") < html.index("</head>"))
check("JS 定义了 applyTheme", "function applyTheme" in js)
check("JS 定义了 applyFontScale", "function applyFontScale" in js)
check("JS 初始化时调用了 initViewControls",
      "function initViewControls" in js and "initViewControls();" in js)
check("主题持久化到 localStorage", "ghrank.theme" in js and "lsSet(LS_THEME" in js)
check("字号持久化到 localStorage", "ghrank.fontScale" in js and "lsSet(LS_FONT" in js)

# ---------------------------------------------------------------- 字号可缩放
print("\n[2] 字号必须真的能全局缩放")

check("根字号由 --font-scale 驱动",
      re.search(r"html\s*\{[^}]*font-size:\s*calc\([^)]*var\(--font-scale\)", css) is not None)

# 除了 :root 定义处，正文里不能有裸 px 字号（否则那部分不随档位缩放）
body_css = css.split("}", 1)[1] if "}" in css else css
px_font = [
    m.group(0).strip()
    for m in re.finditer(r"font-size:\s*[\d.]+px", body_css)
]
check("规则体里没有裸 px 字号（全部走 rem）", not px_font, str(px_font[:4]))

check("默认档位是「大」(1.2)",
      "--font-scale: 1.2" in css and "FONT_DEFAULT = '1.2'" in js,
      "小屏用户默认就该偏大")
check("三档齐全",
      all(f'data-fs="{v}"' in html for v in ("1", "1.2", "1.4")))

# ---------------------------------------------------------------- 颜色瞬时切换
print("\n[3] 颜色必须瞬时切换（这是本测试存在的核心理由）")

color_props = ("background", "color", "border-color", "border")
bad_transitions = []
for m in re.finditer(r"transition:\s*([^;]+);", css):
    value = m.group(1)
    for prop in color_props:
        # 精确匹配属性名，避免 transition-property 里的 transform 被误判
        if re.search(rf"(^|[\s,]){re.escape(prop)}\b", value):
            bad_transitions.append(value.strip())
            break

check("没有任何颜色类 transition",
      not bad_transitions,
      f"发现 {len(bad_transitions)} 处: {bad_transitions[:3]}")

# ---------------------------------------------------------------- 两套主题真的不同
print("\n[4] 两套主题的变量必须不同")

root_block = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
light_block = re.search(r'\[data-theme="light"\]\s*\{(.*?)\n\}', css, re.S)
check("存在 :root 深色变量块", root_block is not None)
check("存在 [data-theme=\"light\"] 浅色变量块", light_block is not None)

if root_block and light_block:
    def parse(block: str) -> dict:
        return {
            m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+):\s*([^;]+);", block)
        }

    dark = parse(root_block.group(1))
    light = parse(light_block.group(1))

    shared = set(dark) & set(light)
    same = [k for k in shared if dark[k] == light[k]]
    check("深浅两套主题有实质差异（不是没改）",
          len(same) < len(shared) * 0.25,
          f"{len(shared)} 个共享变量里 {len(same)} 个值相同: {sorted(same)[:4]}")

    for var in ("--bg", "--bg-soft", "--text", "--accent", "--border"):
        check(f"{var} 两套主题不同",
              var in dark and var in light and dark[var] != light[var],
              f"dark={dark.get(var)} light={light.get(var)}")

    # 浅色主题的文字必须够深 —— 浅底浅字等于看不清
    def hex_to_lum(c: str) -> float:
        c = c.lstrip("#")
        if len(c) != 6:
            return 1.0
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    check("浅色主题 文字比背景更暗（对比度方向正确）",
          hex_to_lum(light.get("--text", "#fff")) < hex_to_lum(light.get("--bg", "#fff")),
          f"text亮度={hex_to_lum(light.get('--text', '#fff')):.0f} "
          f"bg亮度={hex_to_lum(light.get('--bg', '#fff')):.0f}")
    check("深色主题 文字比背景更亮（对比度方向正确）",
          hex_to_lum(dark.get("--text", "#000")) > hex_to_lum(dark.get("--bg", "#000")),
          f"text亮度={hex_to_lum(dark.get('--text', '#000')):.0f} "
          f"bg亮度={hex_to_lum(dark.get('--bg', '#000')):.0f}")

# ---------------------------------------------------------------- 快捷键
print("\n[5] 快捷键")

check("T 键切主题", "applyTheme(currentTheme() === 'dark' ? 'light' : 'dark')" in js)
check("快捷键在弹窗打开时不误触", "if (!$('#modal').hidden) return;" in js)
check("快捷键避开输入框", "contenteditable" in js)

print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
for f in failed:
    print(f"  - {f}")
print("=" * 64)
sys.exit(1 if failed else 0)
