# 长安 · 启阙

用户于 2026-11-04 从三方向对比中明确回复「A」，选择启阙。确认记录见 `design-demos/sidebar-logo/direction-approved.md`。

- 图形：城门/A 形结构，中央门洞，右上角橙色输入光标。几何比例遵循已展示的 A 版，不重新设计。
- 主色：深灰 `#202328`，深色界面反白 `#f0f1f4`；点缀橙 `#e66736`。
- `changan-logo.svg`：可编辑主标识，透明背景。
- `changan-logo-light.png` / `changan-logo-dark.png`：512×512 透明背景，分别用于浅色和深色界面。
- `changan-app.svg` / `changan-app.png`：圆角深灰应用底板与反白图形，PNG 512×512。
- `changan.ico`：16 / 20 / 24 / 32 / 40 / 48 / 64 / 128 / 256 像素九档，用于 Windows 窗口、程序与快捷方式。

复跑 `python scripts/create_brand_assets.py` 从同一组几何坐标生成矢量与抗锯齿位图，不需要联网或新增依赖。打包脚本直接复制同一枚 ICO，不再创建旧的三横线图标。

本图形为源码绘制的设计交付；未做商标近似检索，不构成商标注册或独占使用承诺。该品牌选择仅用于长安，不自动推广到用户其他项目。
