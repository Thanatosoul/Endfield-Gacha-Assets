# Endfield Gacha Assets

《明日方舟：终末地》卡池资源仓库。

通过 GitHub Pages 提供结构化卡池数据和图片资源，供抽卡辅助工具消费。

## 资源内容

- `public/data/GachaPoolTable.json` — 全量卡池数据（新池 + 历史池合并）
- `public/data/index.json` — 版本摘要和统计
- `public/images/banner/char/` — 角色卡池横幅
- `public/images/banner/weapon/` — 武器卡池横幅
- `public/images/rotate/` — 角色池轮换图
- `public/images/character/` — 角色肖像
- `public/images/weapon/` — 武器肖像

## 数据来源

- 新池：官方 `ef-webview.hypergryph.com/api/content` 接口
- 旧池（API 已下线）：历史归档数据回填
- 横幅图片：官方 CDN `web.hycdn.cn`
- 肖像资源：收集整理

## 更新周期

通过 GitHub Actions 每日自动同步。