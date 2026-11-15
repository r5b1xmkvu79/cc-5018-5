# 园区内部电视广播原型 · RTP/MPEG-TS

在本地网络上用 **RTP 承载 MPEG-TS** 播送园区内部电视，接收端做序号回绕感知的
包重排、不可恢复区间标记、去抖动缓冲，再交给**真实 ffmpeg 解码器**播放，
同时分析 RTP 序号连续性、MPEG-TS 节目表（PAT/PMT）与 PCR 时间参考。

控制页面是一个可操作的流媒体控制台：切换节目、注入乱序/重复/丢包/阻塞，
结果区同时呈现广播画面和包连续性诊断。所有诊断数字都来自接收端**实际收到的包**。

---

## 1. 怎样启动（界面入口）

```bash
cd /workspace/tvcast
./run.sh                 # 默认端口 8080
# 或： python3 -m tvcast.main --port 8080
```

然后浏览器打开 **http://127.0.0.1:8080/** ，点页面左上 **“启动广播”**。
- 节目画面：接收端重排后的 TS 经 ffmpeg 重封装为 fMP4，由页面 `<video>` 播放。
- 数据刷新：每秒拉 `/api/state`，内核事件经 SSE（`/api/events`）即时推送。
- 只有脚本不算完成：本项目的主交付物是这个由电视传输内核驱动的控制页；
  `experiments/` 下的脚本仅用于脱离界面单独验证网络路径。

依赖：Python 3.11 标准库 + 随仓库自带的静态 ffmpeg（`tools/ffmpeg-static/`，
ffmpeg 7.0.2）。无需 pip 安装、无需 root。

---

## 2. 数据流与模块

```
ffmpeg(lavfi→MPEG-TS stdout)
   └─ Sender: RTP打包(PT33, 7×188B/包) → 故障注入(丢包/突发/重复/乱序/阻塞) → UDP
        组播 rtp://239.10.20.30:5004   或   单播 rtp://127.0.0.1:5004
              └─ Receiver:
                   1) RTP 解析，16 位序号映射到 64 位扩展序号空间
                   2) 重排缓冲（回绕感知，区分 正常/乱序/重复/迟到）
                   3) 看门狗：超时或缓冲越界 → 判定不可恢复区间（不无限等待）
                   4) 去抖动播放队列（有界，吸收到达抖动，不改动播放时钟）
                   5) TSAnalyzer(PAT/PMT 代际、PCR、TS 连续性计数器)
                   6) 真实 ffmpeg：常驻解码校验器 + 每连接 fMP4 流
```

| 文件 | 职责 |
|---|---|
| `tvcast/rtp.py` | RTP 打包/解析、16 位循环序号距离、TS 净荷切分 |
| `tvcast/ts.py` | PAT/PMT 分段重组与代际跟踪、PCR、TS 连续性计数器 |
| `tvcast/injector.py` | 受控损伤：均匀丢包、突发、重复、乱序窗口 |
| `tvcast/sender.py` | ffmpeg 源→RTP→注入→UDP；切台只重启源，RTP 序号连续 |
| `tvcast/receiver.py` | 重排/回绕/去抖/缺失标记/解码/落盘 |
| `tvcast/core.py` | 编排内核：生命周期、场景、双端快照 |
| `tvcast/server.py` | REST + SSE + `/video` fMP4 + 样本下载 |
| `tvcast/web/index.html` | 单页流媒体控制界面 |

---

## 3. 序号回绕、乱序、重复如何分别处理

接收端把 16 位 RTP 序号映射到一条 **64 位扩展序号轴**，回绕变成轴上的正常 +1：

- **回绕**：到达序号从 `0xFFxx` 前向跳到 `0x00xx` 记一次 `wraps`，
  扩展序号继续递增，**不会**被误判成 6 万多个丢包。
- **乱序**：序号大于期望值的先进重排缓冲，计 `out_of_order`，
  等空洞补齐后按序释放给解码器。
- **重复**：序号已在缓冲或已投递过，计 `duplicates` 后丢弃，不进解码流。
- **迟到**：缺失区间已判定关闭后才到的包，计 `late_after_gap`，
  不会重新撕开旧区间、不重复计数。
- **不可恢复缺失**：`reorder_timeout`（默认 250ms）内空洞未补齐，
  或重排缓冲超过上限（256），看门狗明确产出一条 gap 区间
  （序号起→止、包数、原因），用 null TS（PID 0x1FFF）填充保持解码字节时钟，
  **绝不无限等待**。

单测覆盖以上分支：`python3 -m unittest tvcast.tests.test_kernel -v`

---

## 4. 网络缺失 与 解码错误 不是同一个数字

两侧各自保留计数，页面分列显示，禁止简单等同：

| 层面 | 计数器 | 含义 |
|---|---|---|
| 发送端 | `injector.lost / sent / dup / reordered` | 网络侧实际丢/发/重复/乱序 |
| 接收端 RTP | `missing_packets / gap_intervals / duplicates` | 序号空间观测到的缺失与重复 |
| MPEG-TS | `cc_errors / tei / discontinuity_markers` | TS 包头连续性计数器错误 |
| 解码器 | `decode_error_count` | ffmpeg 真实报出的解码错误行 |

一个丢失的 RTP 包含 7 个 TS 包，但可能落在同一视频帧、被前/后参考帧掩盖，
也可能同时打断音频和视频；因此受控丢包实验里
`missing_packets ≠ cc_errors ≠ decode_error_count`，三者关系由实验观测而非画等号。

到达抖动按 RFC 3550 A.8 计算（`jitter_rfc3550`），只作用于去抖动缓冲水位，
**不会**直接改写 PCR/播放时钟；PCR 抖动单独由 `pcr_jitter_us` 跟踪。

---

## 5. 节目表变化与 PCR 跟踪

- 切台只重启 ffmpeg 源，**RTP 序号空间保持连续**。
- 编码器重启后 PMT 版本号可能仍从 0 开始；内核在切台时调用
  `note_source_switch()` 让 TS 分析器进入新 **epoch**，
  即使 version 相同也记录为新一代节目表（页面 `PMT 代际/版本`）。
- PCR 从适配域提取，记录最近值与相对墙钟的抖动（µs）。

---

## 6. 编排场景与观测指标

页面“编排场景”按钮（或 `POST /api/scenario`）：

| 场景 | 观测点 |
|---|---|
| 连续播出+受控丢包 | 清洁基线首帧时间，再 5% 丢包看三类计数分离与恢复 |
| 节目参数调整(PMT) | 三次切台，核对 PAT/PMT epoch 代际递增 |
| 接收端晚加入 | 新接收器自身时钟的首包/首帧时刻，不依赖发送端历史 |
| 短时网络阻塞 | 4 秒阻塞合并为 1 个 gap、null 填充、解码器存活、缓冲无溢出 |
| 序号回绕 | 发送序号置 65530 重启源，确认 wraps=1 且无伪大量丢包 |

关键指标：`decoder.first_output_at`（**首次可解码时刻**）、
`playout_high_water`（**缓冲上限实测高水位**）、`playout_overflows`。

---

## 7. 组播配置与单播替代

### 通过界面/内核（主路径）
页面“传输”下拉切换 `multicast` / `unicast`，内核重启收发两端：
- 组播：`239.10.20.30:5004`，发送端置 `IP_MULTICAST_LOOP=1` 并指定 lo 出口。
- 单播：`127.0.0.1:5004`。

### 脱离界面的纯 ffmpeg 实验（备用）
```bash
# 组播（两个终端；同机自测）
PORT=5010 ./experiments/recv_multicast.sh
PORT=5010 ./experiments/send_multicast.sh
# 单播（先收后发）
PORT=5012 ./experiments/recv_unicast.sh
PORT=5012 ./experiments/send_unicast.sh
```
组播在无 IGMP 的交换机/云主机上不可用时，改用单播脚本即可。

---

## 8. 原始样本（可复核）

每次接收落盘到 `captures/`：
- `reordered-*.ts`：重排后、真正喂给解码器的 MPEG-TS（含标记用 null 填充），
  可用 ffprobe/ffmpeg 独立复核。
- `packets-*.csv`：逐包 `wall, seq16, ext_seq, classification`，
  回绕/乱序/重复判定可逐行追溯。

页面“刷新样本列表”经 `/api/captures` 与 `/captures/<file>` 下载。
诊断页所有数字均由这些实际收包统计得到，没有任何模拟数据。

---

## 9. HTTP 接口摘要

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/state` | 发送端/接收端/TS/解码器全量快照 |
| GET | `/api/events` | SSE 内核事件流 |
| GET | `/video` | 实时 fMP4（每连接独立 ffmpeg） |
| POST | `/api/pipeline/start`·`/stop` | 收发生命周期 |
| POST | `/api/mode` | `multicast` / `unicast` |
| POST | `/api/channel` | 切换节目 |
| POST | `/api/inject` | loss_rate / duplicate_rate / reorder_window |
| POST | `/api/burst`·`/api/block` | 突发丢包 / 网络阻塞 |
| POST | `/api/scenario` | 运行编排场景 |
| GET | `/api/captures`·`/captures/<f>` | 样本列表与下载 |

控件失败时接口返回 409，页面顶部“失败反馈”直接展示内核报错，
状态始终回读真实内核，不做前端乐观假设。
