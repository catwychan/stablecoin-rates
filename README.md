# 稳定币借贷利率

Aave、Morpho、Spark、Sky、Venus、Kamino、Fluid 和 Jupiter Lend 上稳定币的借款利率、借出体量和 90 天走势。

- `fetch_rates.py` 从各平台的公开 API 抓取数据，生成 `rates.json` 和 `history.json`。
- `.github/workflows/refresh.yml` 每 4 小时运行一次脚本，并把页面发布到 GitHub Pages。
- `index.html` 是页面本身。
