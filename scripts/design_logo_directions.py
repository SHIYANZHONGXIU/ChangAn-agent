"""生成长安原创矢量标识初稿；选定前不覆盖应用品牌资产。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "design-demos" / "sidebar-logo"

MARKS = {
    "gate": '<path d="M18 82V29L50 14l32 15v53H65V42L50 35l-15 7v40Z" fill="currentColor"/><path d="M42 63h16v19H42z" fill="currentColor"/><rect x="77" y="11" width="12" height="12" rx="2" fill="#e66736"/>',
    "seal": '<rect x="9" y="9" width="82" height="82" rx="21" fill="currentColor"/><g fill="none" stroke="var(--paper,#fff)" stroke-width="7" stroke-linecap="square" stroke-linejoin="miter"><path d="M50 22v8M28 41V31h44v10M27 48h47M58 43 39 69l30 9M35 77c16-2 29-14 33-27"/></g>',
    "wing": '<path d="m13 72 28-48h18L31 72Z" fill="currentColor"/><path d="m43 72 28-48h18L68 59h18L75 78H49Z" fill="currentColor"/><path d="M36 64h20l-8 14H28Z" fill="#a6b8ff"/>',
    "city": '<path d="M15 80V34h16V22h15v23h10V22h15v12h14v46H67V58H33v22Z" fill="currentColor"/><path d="M44 68h12v12H44Z" fill="#e66736"/>',
    "character": '<path d="M19 30h62v12H19Zm26-16h12v69H45ZM19 56h62v12H19Z" fill="currentColor"/><path d="M20 81 78 14" stroke="var(--paper,#fff)" stroke-width="10"/><path d="M20 81 78 14" stroke="#c54737" stroke-width="6"/>',
    "routes": '<g fill="none" stroke="currentColor" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"><path d="m22 70 27-46 28 46M36 47h27M34 76h34"/></g><circle cx="49" cy="24" r="8" fill="#9caef3"/>',
}


def svg(key, color="currentColor", paper="#fff"):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" role="img" aria-label="长安标识" style="color:{color};--paper:{paper}">{MARKS[key]}</svg>'


DIRECTIONS = [
    dict(key="gate", code="A", name="启阙", tag="长安之门 × 输入光标", color="#202328", paper="#ffffff", mood="gate", idea="从长安的城门轮廓中抽出一个 A，右上角的一点橙色是等待输入的光标。", note="更适合现在的黑白灰工作台；城市感明确，小尺寸也有识别度。"),
    dict(key="seal", code="B", name="安印", tag="安字结构 × 数字印章", color="#b94032", paper="#fffaf6", mood="seal", idea="把「安」拆成屋顶、交叉和回转的笔画，压进一枚圆角朱印。", note="东方气质更强；不是通用机器人头像，而是一枚长安自己的签名。"),
    dict(key="wing", code="C", name="折羽", tag="双折线 × 向前协作", color="#5469d4", paper="#f8f9ff", mood="wing", idea="两束前进的折线交汇成翼，呼应多个 Agent 在同一目标下协作。", note="更年轻、更像开发者工具；线条有速度感，但不靠光效和渐变。"),
]

CSS = """
*{box-sizing:border-box}body{margin:0;color:#252830;background:#f3f4f6;font-family:'Microsoft YaHei UI','Segoe UI',sans-serif;font-size:14px;-webkit-font-smoothing:antialiased}
.page{max-width:1440px;margin:auto;padding:42px 48px}header{display:flex;justify-content:space-between;align-items:end;margin-bottom:30px}h1{margin:0;font-size:31px;letter-spacing:-1px;font-weight:650}header p{margin:10px 0 0;color:#626670;font-size:14px}.status{font-size:12px;color:#6f7480;border:1px solid #d6d9df;border-radius:20px;padding:8px 14px;background:#fff}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px}.card{overflow:hidden;background:white;border:1px solid #dfe2e7;border-radius:18px}.hero{height:335px;display:flex;align-items:center;justify-content:center;position:relative}.hero svg{width:190px;height:190px}.gate .hero{background:#fff}.seal .hero{background:#fff3e9}.wing .hero{background:#202638}.wing .hero svg{color:#b9c5ff!important}.label{position:absolute;left:23px;top:22px;font:12px 'Segoe UI',sans-serif;letter-spacing:1px;color:#747982}.wing .label{color:#c8cfe3}.details{padding:23px 26px 25px;border-top:1px solid #e8eaee}.title{display:flex;align-items:baseline;gap:11px}.code{font:15px 'Segoe UI',sans-serif;color:#656b76}h2{font-size:25px;letter-spacing:1px;margin:0}.tag{font-size:13px;color:#5d6571;margin:11px 0 16px}.details p{font-size:14px;line-height:1.9;margin:0;color:#454c57;min-height:54px}.samples{display:flex;align-items:center;gap:16px;margin-top:23px}.app{width:49px;height:49px;border-radius:12px;display:flex;align-items:center;justify-content:center}.app svg{width:36px;height:36px}.mono{background:#22252a;color:#fff}.mini{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:600}.mini svg{width:23px;height:23px}.footer{margin-top:26px;padding-top:18px;border-top:1px solid #d9dde4;display:flex;justify-content:space-between;color:#656b76;font-size:13px}.note{max-width:820px;line-height:1.8}.standalone .hero{height:440px}.standalone .hero svg{width:250px;height:250px}.standalone .details{padding:24px 35px}.standalone .details p{min-height:0}.standalone .samples{margin-top:18px}.standalone .card{max-width:1104px;margin:auto}.standalone .hero .wordmark{position:absolute;left:45px;bottom:29px;font-size:26px;letter-spacing:6px}.wing .wordmark{color:#e6eaff}.standalone .details .title{float:left;margin-right:30px}.standalone .details .tag{margin-top:3px}.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}.thumb{border:1px solid #dfe2e7;border-radius:16px;background:white;min-height:300px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px}.thumb svg{width:135px;height:135px}.thumb p{font-size:15px;margin:0}.thumb span{font-size:13px;color:#69707c}.thumb.selected{border-color:#9ba4b6}.thumb.selected span{color:#303b50}.responsive{margin:0}
@media(max-width:760px){.page{padding:24px 18px}header{display:block}h1{font-size:25px}.status{display:inline-block;margin-top:16px}.grid,.thumbs{grid-template-columns:1fr}.hero{height:270px}.footer{display:block;line-height:1.8}.standalone .hero{height:340px}}
"""


def document(title, content):
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + title + '</title><style>' + CSS + '</style></head><body>' + content + '</body></html>'


def card(d, standalone=False):
    mark = svg(d["key"], d["color"], d["paper"])
    mono = svg(d["key"], "#fff", "#22252a").replace('fill="#e66736"', 'fill="currentColor"').replace('fill="#a6b8ff"', 'fill="currentColor"')
    return f'''<article class="card {d['mood']}"><div class="hero"><span class="label">CHANGAN / {d['code']}</span>{mark}{'<span class="wordmark">长安</span>' if standalone else ''}</div><div class="details"><div class="title"><span class="code">{d['code']}</span><h2>{d['name']}</h2></div><div class="tag">{d['tag']}</div><p>{d['idea']}</p><div class="samples"><div class="app" style="background:{d['color']}">{svg(d['key'], '#fff', d['color'])}</div><div class="app mono">{mono}</div><div class="mini">{mark}<span>长安</span></div></div></div></article>'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cards = "".join(card(d) for d in DIRECTIONS)
    (OUT / "logo-comparison.html").write_text(document("长安 LOGO · 三个方向", f'<main class="page"><header><div><h1>长安，重新认识一下。</h1><p>三个原创矢量方向 · 都会适配浅色、深色与 Windows 应用图标</p></div><span class="status">设计初稿 · 尚未替换应用</span></header><section class="grid">{cards}</section><footer class="footer"><span class="note">建议 A：保留长安的城市联想，与现有黑白灰侧栏最协调。B 更东方，C 更有开发者工具气质。</span><span>选 A / B / C，也可以混合调整</span></footer></main>'), encoding="utf-8")
    for index, d in enumerate(DIRECTIONS, 1):
        (OUT / f"logo-{d['code'].lower()}.svg").write_text(svg(d["key"], d["color"], d["paper"]), encoding="utf-8")
        (OUT / f"dir-{index}.html").write_text(document("长安 LOGO · " + d["name"], f'<main class="page standalone"><header><div><h1>{d["code"]} / {d["name"]}</h1><p>{d["note"]}</p></div><span class="status">矢量标识 · 待确认</span></header>{card(d, True)}<footer class="footer"><span>主标识 / 彩色应用图标 / 单色反白 / 侧栏缩略</span><span>初稿不代表已应用</span></footer></main>'), encoding="utf-8")
    thumbs = []
    for key, name, caption, color, selected in (
        ("gate", "A · 启阙", "推荐深化：城门与光标", "#202328", True),
        ("seal", "B · 安印", "推荐深化：安字与朱印", "#b94032", True),
        ("wing", "C · 折羽", "推荐深化：双折线协作", "#5469d4", True),
        ("city", "备选 · 城垣", "较具象，城市联想更直接", "#202328", False),
        ("character", "备选 · 长锋", "实验字形，强调笔画张力", "#202328", False),
        ("routes", "备选 · 同路", "圆润连线，强调亲和协作", "#5469d4", False),
    ):
        thumbs.append(f'<article class="thumb {"selected" if selected else ""}">{svg(key, color)}<p>{name}</p><span>{caption}</span></article>')
    (OUT / "thumbs.html").write_text(document("长安 LOGO · 六个候选", '<main class="page"><header><div><h1>先看图形，再定个性。</h1><p>上排是已深化的三个方向，下排保留另外三个候选。</p></div></header><section class="thumbs">' + "".join(thumbs) + '</section></main>'), encoding="utf-8")
    (OUT / "design-spec.md").write_text("""# 长安侧栏与 LOGO 设计约定

用户依据：2026-11-04 用户提供聊天菜单截图，要求聊天侧栏含样式与其保持一致，并重新设计有个性的 LOGO。

- 侧栏：按截图复现浅灰底、白色圆角操作菜单、线性图标、灰色快捷键、分组和子菜单；深色模式沿用相同层级。原聊天/文件安全保护不变。
- LOGO：原创、可编辑 SVG；三版统一 1440×900 比较板与 1200×900 方向板。最终需覆盖侧栏、窗口、ICO 和打包入口，用户选定前不替换。
- 内容来自「长安」城市意象、汉字「安」与多 Agent 协作，不借用 Codex 的标识。
- 假设：保持现有黑白灰工作台，不另做整套 UI 主题；新窗口先提供诚实标明的只读聊天快照，不开启第二个数据写入实例。
- 设计素材是源码原生矢量，不调用生图服务，也不产生模型 API 费用。
""", encoding="utf-8")
    (OUT / "directions.md").write_text("""# 六个候选与三方向

不是设计师背书，也未声称引用或临摹任何商标。来源是设计思维与本项目内容。

- A 启阙：借助佐藤可士和式符号压缩思维（不仿作品），把城门和 A 压成一个厚实几何形，加入一枚橙色光标；白色中心版式。形来自长安的城门与代码输入。
- 备选 城垣：同一城市语义的具象版本，城垛更直接但较像建筑品牌，未深化。
- B 安印：借助田中一光式几何构成思维（不仿作品），安字解构与朱印相接；实色、无肌理、暖色展示。形来自品牌中文名。
- 备选 长锋：汉字实验笔画，参考已查看的 east 样张中的中文实验字体构成语言，不作成现成字体；小尺寸歧义较高，未深化。
- C 折羽：借助设计风格库 east/09 田中式几何构成样张的硬边模块语言，双路径折线向前交汇；冷色反白版式。形来自多个 Agent 同向协作。
- 备选 同路：圆角连线与节点，亲和但与通用软件 A 图形更接近，未深化。

侧栏的现实参照仅为用户提供的截图，未把未经核验的网上案例当证据。初稿确认前不替换图标，也不重新打包独立 EXE。
""", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
