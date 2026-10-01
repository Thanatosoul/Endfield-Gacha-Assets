# 图片来源与致谢 / Image Sources

本仓库中的卡池封面图来自以下来源：

## 官方

- 游戏数据与卡池封面：Hypergryph 官方 content 接口
  （`https://ef-webview.hypergryph.com/api/content`），由 `scripts/sync-daily.py` 同步。
- 部分卡池（如联合寻访「辉光庆典」`joint_1_2_2`）官方接口不返回任何图片字段，
  因此同步不到封面，需第三方补充。

## 第三方补充

- `public/images/banner/char/joint_1_2_2.webp`（辉光庆典）
  来自 [`cmyyx/cep`](https://github.com/cmyyx/cep) 的
  `public/images/banners/huiguagnqingdian.webp`。

应用端在本地封面缺失时还会回退到以下第三方图源（见主项目
`src/modules/pool-management/imageSources.ts`）：

- [`ivaqis/arknights-tracker`](https://github.com/ivaqis/arknights-tracker)：
  `arknights-tracker/static/images/banners/icon/<poolId>.webp`（按 poolId 命名）。
- [`cmyyx/cep`](https://github.com/cmyyx/cep)：`public/images/banners/<slug>.webp`（按名称命名）。

这些素材版权归原权利方所有，此处仅用于本地存档与展示，不作商业用途。
