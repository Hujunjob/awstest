# AWS Ubuntu Notes

本文档只覆盖一件事：在 AWS Ubuntu 服务器上，先用 `network_probe.py` 检查 Predict / Polymarket / Polygon 的网络可达性和鉴权链路，再决定是否启动交易进程。

## 1. 适用环境

推荐环境：
- Ubuntu Server 24.04 LTS
- x86_64
- 已安装 `git`
- 已安装 Python 3.11+ 和 `python3-venv`

如果是新机器，先安装基础依赖：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ca-certificates
```

## 2. 拉代码并准备虚拟环境

```bash
cd /opt
sudo git clone <your-repo-url> PolymarketBot
sudo chown -R $USER:$USER /opt/PolymarketBot
cd /opt/PolymarketBot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

如果你平时已经有自己的依赖安装方式，继续沿用即可。`network_probe.py` 至少会用到这些 Python 依赖：
- `websocket-client`
- `web3`
- `eth-account`
- `py-clob-client`
- `py-builder-relayer-client`
- `py-builder-signing-sdk`

## 3. 准备环境变量

建议直接把 AWS 上要使用的钱包配置写进一个独立 env 文件，例如：
- `envs/.env.wallet4`

脚本会优先读取：
- `--env-file` 指定的文件
- 当前 shell 环境变量

默认会同时测试：
- 公开接口
- 鉴权接口

因此 env 里最好至少包含：

### Predict
- `PREDICT_API_KEY`
- `PREDICT_PRIVATE_KEY` 或 `WALLET_PRIVATE_KEY`
- `PREDICT_ACCOUNT_ADDRESS`（如果你使用 predict account 模式）
- `PREDICT_API_BASE_URL`（可选）
- `PREDICT_WS_URL`（可选）
- `PREDICT_CHAIN_RPC_URL` 或 `PREDICT_RPC_URL`（可选）

### Polymarket / Polygon
- `PM_PRIVATE_KEY`
- `PM_FUNDER`
- `PM_CHAIN_ID`（默认 137）
- `PM_CLOB_HOST`（可选）
- `PM_DATA_API_BASE_URL`（可选）
- `PM_WS_URL`（可选）
- `PM_USER_WS_URL`（可选）
- `PM_RELAYER_URL`（可选）
- `PM_CHAIN_RPC_URL` 或 `POLYGON_RPC_URL`

### PM 鉴权 / Relayer
- `PM_BUILDER_API_KEY`
- `PM_BUILDER_SECRET`
- `PM_BUILDER_PASSPHRASE`

如果你已经有现成的 PM API creds，也可以直接提供：
- `PM_API_KEY`
- `PM_API_SECRET`
- `PM_API_PASSPHRASE`

## 4. 运行网络探测脚本

脚本路径：
- `predict_pm_consumer/network_probe.py`

### 4.1 默认运行

默认会同时测：
- Predict REST / WS / BSC RPC
- PM CLOB / Data API / market WS / user WS / relayer
- Polygon RPC HTTP / WS
- Predict / PM 的鉴权链路

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4
```

### 4.2 输出 JSON

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --json
```

### 4.3 多次采样

适合在不同 AWS Region 上对比：

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --repeat 3 \
  --json
```

### 4.4 只测公开接口，不测鉴权

如果你只是想先判断这个地区能不能访问服务：

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --skip-auth \
  --json
```

### 4.5 只测某一类服务

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --only predict,polygon \
  --json
```

支持的 group：
- `predict`
- `pm`
- `polygon`

## 5. 当本机没有 mapping DB 时

脚本默认会尝试从：
- `output/predict/predict_points.db`

读取一个 sample `predict_market_id` 和 `pm_token_id`，用于 orderbook / book / WS 订阅探测。

如果这台 AWS 机器还没有同步过 DB，就手动传：

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --predict-market-id 1585 \
  --pm-token-id 111 \
  --json
```

## 6. 结果怎么读

脚本每一项都会输出：
- `service`
- `target`
- `ok`
- `latency_ms`
- `phase`
- `error`
- `http_status`（如果适用）

总结区会给出：
- `total`
- `ok`
- `failed`
- `slow`

默认规则：
- 任意 probe 失败，脚本退出码为 `1`
- 全部 probe 成功，退出码为 `0`
- `slow` 的阈值默认是 `1500ms`，可用 `--slow-threshold-ms` 调整

## 7. AWS 上常见问题

### 7.1 `missing sample predict market id`
说明这台机器没有可用的 `predict_points.db`，或者 DB 里没有 `MAPPED` 市场。

解决：
- 传 `--predict-market-id`
- 传 `--pm-token-id`
- 或先把 mapping DB 同步到服务器

### 7.2 `missing PREDICT_API_KEY` / `missing PM_PRIVATE_KEY`
说明 env 文件不完整，或者 `--env-file` 路径传错。

先检查：

```bash
ls -l envs/.env.wallet4
```

### 7.3 `Polygon RPC WS` 失败，但 HTTP 正常
这通常说明：
- 当前 Region 对 WebSocket 路径不稳定
- 你配的 RPC 提供商没有开放 WS
- 安全组 / 出口策略对长连接不友好

### 7.4 `PM User WS` 失败，但 `PM Market WS` 正常
这通常是鉴权问题，不一定是网络问题。
优先检查：
- `PM_API_KEY/PM_API_SECRET/PM_API_PASSPHRASE`
- 或 `PM_PRIVATE_KEY + PM_BUILDER_*`

### 7.5 `PM Relayer` 失败
优先检查：
- `PM_BUILDER_API_KEY`
- `PM_BUILDER_SECRET`
- `PM_BUILDER_PASSPHRASE`
- `PM_FUNDER`
- `PM_RELAYER_URL`

## 8. 推荐操作顺序

在 AWS 新机器上，建议按这个顺序走：

1. 跑 `network_probe.py --skip-auth`
2. 确认公开接口都通
3. 再跑完整 probe
4. 把 JSON 结果保存下来，对比不同 AWS Region 的延迟
5. 确认后再启动 producer / v3 / reconcile

## 9. 示例

```bash
cd /opt/PolymarketBot
.venv/bin/python predict_pm_consumer/network_probe.py \
  --env-file envs/.env.wallet4 \
  --repeat 3 \
  --json > /tmp/network_probe_wallet4.json
```

如果你要对比多个 Region，最有用的是保存每台机器的 JSON 输出，然后横向比较：
- Predict REST / WS 是否稳定
- PM market WS / user WS 是否稳定
- Polygon HTTP / WS 哪个慢或不通
- 鉴权链路是否只在某个 Region 失败
