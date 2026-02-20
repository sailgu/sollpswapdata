# sollpswapdata

按 **token A / token B** 抓取 Solana 链上逐笔交易：
- 先回补过去一周（默认 7 天）
- 再可选持续实时抓取
- 数据存入本地 SQLite

## 安装依赖
无第三方依赖，使用 Python 标准库即可（3.10+）。

## 使用

```bash
python solana_weekly_pair_data.py \
  --token-a <TOKEN_A_MINT> \
  --token-b <TOKEN_B_MINT> \
  --days 7 \
  --db pair_trades.db \
  --live
```

常用参数：
- `--rpc-url`：Solana RPC 地址（默认 `https://api.mainnet-beta.solana.com`）
- `--pool`：手动指定池子地址（不指定时会自动查找流动性最高池）
- `--poll-seconds`：实时轮询间隔，默认 15 秒

## 表结构

SQLite 文件中的 `trades` 表字段：
- `signature`（主键）
- `block_time`, `ts_utc`, `slot`
- `trader`, `side`
- `amount_in`, `mint_in`
- `amount_out`, `mint_out`
- `source`

## 说明
- 历史与实时数据均通过 Solana RPC 的交易明细解析而来（逐笔）。
- 自动查池使用 GeckoTerminal 仅用于定位池地址，不用于交易数据统计。
- 若外网/RPC 被限制，会出现网络错误。
