# hermes-loop-breaker

给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 写的**本地补丁**：一个挡住
「纯思考死循环」的熔断器，外加一键开关（不重启、不改 config，即时生效）。

> ⚠️ 这是针对特定版本存档的本地补丁，**不是可独立运行的软件**。上游重构后插入点行号会漂移。

## 它解决什么问题

长会话里模型有时会**原地打转**：连续好几轮没有新信息，只是换个说法重复同一段推理 ——
既烧 token，又永远不给你结果。Hermes 自带的 guardrail 主要盯**工具调用**层面的循环，
对「只思考、不调工具」的那种打转盯不住。

本补丁补上这一块：

- **检测**：连续 5 轮推理指纹相似度 ≥ 0.9（4-gram Jaccard）判为打转；
- **纠正**：先 nudge ×2 —— 提示通过「合成工具结果」挂到 tool 结果尾部，
  不动 system prompt、不新增消息角色（不破坏 prompt cache）；
- **兜底**：仍不改就 halt，把已完成的进度交还给用户。

另带一条 **session 级 token 确认点阶梯**：150k → 400k → 550k → 950k → 1.5M …（斐波那契式增长），
长任务里定期提醒，避免烧完额度才发现。

## 一键开关（v3.1）

开关不写进 config，而是走**哨兵文件**（放在 `HERMES_HOME`，与 `config.yaml` 同级）：

| 哨兵文件 | 含义 |
|---|---|
| `loop_breaker.off` | 全关：防死循环与 token 确认点都停 |
| `loop_breaker.token_off` | 只关 token 确认点，保留防死循环 |
| `loop_breaker.on` | 强制开启（覆盖 config 里的 `false`） |
| 都没有 | 跟随 `config.yaml` |

优先级：`off` > `token_off` > `on` > `config`。

**切换对正在运行的会话下一轮即时生效**（每轮实时 `Path.exists()` 检测），无需重启。
只有改 `agent/loop_breaker.py` **代码**时才需要重启一次，之后开关都是热的。

## 用法

```
lb.cmd                 # 无参 = toggle（当前是 off 就往 on 切，否则往 off 切）
lb.cmd on              # 强制开启
lb.cmd off             # 全关
lb.cmd token           # 只关 token 确认点，保留防死循环
lb.cmd clear           # 清除哨兵，回到跟随 config
lb.cmd status          # 只看状态，不改动
lb.cmd off --persist   # 同时把 agent.loop_breaker.enabled 写进 config.yaml
```

`toggle-desktop.cmd` 是双击用的薄壳（内部调 `lb.cmd toggle`）—— 给它建个快捷方式固定到
任务栏，就成了一键开关。

> ⚠️ `off` 是**静默持久**的：忘了复位，配合 `max_turns: 9999` + `max_steps: 0` 就是真能一直跑下去。
> 用完记得 `lb.cmd clear`；切换脚本的面板也会用大字提醒。

## 文件清单

```
agent/loop_breaker.py                        核心模块（纯逻辑、零第三方依赖、可单测）
tests/agent/test_loop_breaker.py             单测 20 例（纯 stdlib 即可运行）
patches/loop-breaker-insertion-points.patch  4 个现有文件的插入点 diff
scripts/lb.cmd                               命令行开关
scripts/toggle-desktop.cmd                   双击开关
```

## 套用到自己的 Hermes

1. 复制 `agent/loop_breaker.py` 到 `<HERMES_ROOT>/agent/`
2. 应用插入点补丁 —— **先 `grep -n` 核对行号**，上游版本不同会漂移：

   ```
   git apply patches/loop-breaker-insertion-points.patch
   ```

3. 在 `config.yaml` 的 `agent:` 段下加：

   ```yaml
   loop_breaker:
     enabled: true
     warnings_enabled: true
     hard_stop_enabled: false
     max_turns: 9999
     max_steps: 0
     token_warn: 0
   ```

   完整 12 个字段的默认值见 `agent/loop_breaker.py` 的 `from_mapping`。
4. 重启 Hermes 一次（**代码**改动需要重启；之后开关本身是热生效的）。

## 已知限制

- 依赖 `agent/conversation_loop.py` 等 4 个文件的插入点，上游重构后需重新定位。
- 每轮多 3 次 `Path.exists()` —— 打破了「纯逻辑模块零副作用」的契约；
  测试可用 `_SENTINEL_DIR_OVERRIDE` 完全旁路。
- 只覆盖主 Agent，subagent 路径未改。
- 未触碰 Hermes 任何安全 / 缓存底线机制。

## License

MIT —— 与上游 Hermes Agent 一致。上游代码版权归 Nous Research，本补丁部分可自由使用。
