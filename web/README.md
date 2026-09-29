# web —— Web 仪表盘

ESP32-S3 温湿度监控系统的前端（设计文档 §24-§25、§41 Step 7）。

```
SQLite → FastAPI → Vue
```

## 技术栈

| 包 | 版本 | 说明 |
|---|---|---|
| Vue | 3.5.43 | 文档 §24 指定；`<script setup>` + Composition API |
| TypeScript | 5.9.3 | **不是** latest（7.x）。TS 7 是 Go 重写版，vue-tsc 的 Volar 集成未必跟上了 |
| ECharts | 6.1.0 | 文档 §24 指定；**按需引入**（见 `src/echarts.ts`） |
| Vite | 8.3.1 | 构建工具 |
| vue-tsc | 3.3.11 | 类型检查进构建 |

版本全部**钉死**（`package.json` 里没有 `^`，`.npmrc` 里 `save-exact=true`），
理由与后端 `requirements.txt` 用 `==` 相同。

**没有引入 vue-router**：本轮是单页 Dashboard，加了只是多一层空转。

## 怎么跑

### 生产（日常用这个）

前端由**后端容器**托管，和 API 同一个端口、同源：

```bash
cd ..                      # 回到 backend/
sudo docker compose build  # 构建（含前端）
sudo docker compose up -d
```

然后打开 <http://localhost:8005> —— 就是仪表盘。接口文档仍在 <http://localhost:8005/docs>。

**改了前端代码要重新 build**（`docker compose up -d` 不会自动重建）。

### 开发（改前端时用，可选）

本机 **WSL 里没有 Node**，所以前端命令都跑在容器里。用 `run.sh` 包装好了：

```bash
cd web

./run.sh check     # 类型检查（vue-tsc --noEmit）
./run.sh build     # 类型检查 + 打包
./run.sh dev       # Vite 开发服务器（热重载）
./run.sh install   # 只装依赖
```

`./run.sh dev` 之后打开 <http://localhost:5173>，改代码即时生效。

`/api` 请求由 Vite 的开发服务器**在服务端**转发到 8005，所以浏览器看来
全程同源——**开发和生产都不需要 CORS**。

**两个容易踩的地方**（`run.sh` 里已经处理了，改脚本时别删掉）：

- `node_modules` 挂在**具名卷**（`iot-web-modules`）上，不落在 `/mnt/c`。
  三万多个小文件在 DrvFs 上会慢到无法接受——实测 venv 在 `/mnt/c` 上
  要 72 秒、ext4 上只要 1.3 秒，差 55 倍。
- dev 模式必须 `--network host`。不加的话容器里的 `127.0.0.1:8005`
  是**容器自己**，proxy 连不上后端——症状是页面能打开但数据全空。

## 目录

```
src/
├── main.ts                    入口
├── App.vue                    页面装配（4 个独立轮询器）
├── style.css                  设计变量（所有颜色都走 CSS 变量）
├── format.ts                  展示层格式化（时长/字节/相对时间）
├── echarts.ts                 ECharts 按需引入（不用 import * as echarts）
├── api/
│   ├── types.ts               ★ 后端 app/api/schemas.py 的镜像
│   └── client.ts              fetch 封装（超时 + 错误详情）
├── composables/
│   └── usePolling.ts          轮询（防重叠 / 暂停隐藏页 / 失败不清空）
└── components/
    ├── MetricCard.vue         四张指标卡
    ├── TimeSeriesChart.vue    折线图（含 6 个"每 5 秒刷新"相关的细节）
    ├── DataQualityPanel.vue   ★ 数据质量（这一页最有价值的一块）
    └── StatusPill.vue         状态胶囊
```

## ★ 数据质量面板为什么是主角

这个项目实测踩过一类故障：**设备连续发了 41 分钟后彻底停发，持续 2.5 小时，
而它本地一切正常**（MQTT 连接在、队列零丢弃、串口照常打印温湿度）。

从后端看，那段时间只是"没有新数据"——和"设备没采到"长得一模一样。
而一条温度曲线在这种情况下**只是停止延伸**，和"页面刚打开还没加载完"
看起来差不多。**光看曲线发现不了。**

所以面板把"数据还在不在流"变成一个明确的判断，并回答四个递进的问题：

| 问题 | 数据来源 |
|---|---|
| 数据还在进来吗？ | `data.age_ms` / `data.stale` |
| 不在的话断了多久？ | `data.recent_gaps` |
| 是哪一段链路的问题？ | `mqtt.connected` / `ingest.running` / `db.journal_mode` |
| 丢了多少？ | seq 跳号 / `queue_dropped` / 估算漏的条数 |

**"请求失败"和"请求成功但没数据"必须分开显示**——前者是后端不可达，
后者是设备侧问题，混在一起会把人引向错误的排查方向。

## 已知限制（都是有意的，不是遗漏）

### 1. 轮询 5 秒，不是 WebSocket

文档 §25 的第一版方案。够用，而且实现简单、没有连接状态要维护。
代价是最多 5 秒延迟、每 5 秒一次完整请求。

升级路径（文档 §25 后续）：`MQTT → FastAPI → WebSocket → Browser`。
后端已经有 MQTT 消费者，加一个 WebSocket 端点即可。

### 2. 曲线窗口最大 30 分钟

因为后端的 `limit` 上限是 1000，而窗口 `limit = 窗口秒数 ÷ 2`（2 秒采样）。
30 分钟 = 900 点、约 80KB/次。

**窗口再大就该在服务端降采样**，而不是把 1800 个点原样传给浏览器：
给 `/api/telemetry` 加一个 `bucket` 参数，用 SQL 的 `AVG` 按时间桶分组。
那是个明确的后续改进，不是"以后再说"。

### 3. `SAMPLE_INTERVAL_MS` 是隐含耦合

`App.vue` 里的 `SAMPLE_INTERVAL_MS = 2000` 对应固件的
`CONFIG_BOARD_SAMPLE_INTERVAL_MS`。固件改了采样周期，前端不会自动跟着变——
后果只是"窗口内点数估算不准"（取不满或取多），不会出错。
要做对得让后端把采样周期暴露出来。

### 4. 类型是手写的，不是从 OpenAPI 生成的

`src/api/types.ts` 是 `app/api/schemas.py` 的手写镜像。改后端响应模型时
**必须同步改它**，否则前端会在运行期拿到 `undefined`——而 TypeScript
不会报错（它相信你手写的类型）。

不用 `openapi-typescript` 的原因：它需要在**构建时**访问运行中的后端，
而前端是在 Docker 构建阶段编译的，那时后端容器还没起。

后续改进：加一个 `npm run gen:api`（手动跑、对着运行中的后端生成），
然后在 CI 里 diff 检查有没有漂移。

### 5. 静态文件不做 SPA 回退

`StaticFiles(html=True)` 只把 `/` 映射到 `index.html`，未知路径返回 404。
本轮是单页、没有客户端路由，所以不影响。
将来加 vue-router 的 history 模式后，直接刷新 `/devices` 会 404，
那时需要一个 catch-all 路由把未知路径都返回 `index.html`。

## 怎么验证"页面真的能跑"

**`curl http://localhost:8005/` 返回 200 是不够的**——一个白屏的 SPA
对 curl 来说也是 200。Vue 有没有挂载、ECharts 有没有画出来、
API 字段名有没有对错，curl 一概看不出来。

用系统自带的 Chrome 无头模式就能验（**零安装**，不用装
`agent-browser` 那套 500MB 的 Chromium）：

```bash
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"

# 1. 抓渲染后的 DOM，检查有没有关键内容
"$CHROME" --headless=new --disable-gpu --no-sandbox \
  --user-data-dir=/tmp/chrome-profile \
  --virtual-time-budget=15000 \
  --dump-dom http://localhost:8005 > dom.html

# 2. 截图，肉眼看版面
"$CHROME" --headless=new --disable-gpu --no-sandbox \
  --user-data-dir=/tmp/chrome-profile \
  --virtual-time-budget=15000 \
  --window-size=1400,2200 \
  --screenshot=dashboard.png http://localhost:8005
```

**两个关键参数**：

- `--virtual-time-budget=15000` —— 让页面在"虚拟时间"里快进 15 秒。
  SPA 首次渲染要等几个 API 请求回来；不加这个参数的话 `--dump-dom`
  会在 Vue 挂载之前抓取，拿到的是空的 `<div id="app">`，
  于是你会误判成"页面是坏的"。
- `--user-data-dir=<临时目录>` —— 不碰你真实的 Chrome 配置。
  不加的话可能干扰正在运行的 Chrome，或者弹出一堆"恢复上次会话"。

**这个方法抓到过一个真 bug**：温度曲线的 y 轴画成了 15~55，
而实际数据恒为 34.0 —— `scale: true` 在"数据完全平坦"时给出的范围过宽，
曲线被压成一条线，看起来像"数据没加载出来"。
只看 HTTP 状态码永远发现不了这个。

## 相关技能

前端这块的可复用流程见技能 `web-frontend-containerized`
（多阶段构建、无头验证、DrvFs 上的 node_modules 处理）。
